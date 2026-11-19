"""
Tự động giải captcha kéo-thả (slider) của Shopee bằng OMOCaptcha ShopeeSliderWebTask.

Luồng chạy:
  1. Quét DOM, tìm 2 phần tử ảnh của captcha: ảnh nền (lớn) + mảnh ghép (nhỏ, hình vuông).
  2. Chụp từng phần tử bằng CDP Page.captureScreenshot. Để ảnh chụp sạch, lớp còn lại
     được set visibility:hidden trước khi chụp rồi khôi phục ngay sau đó.
     (Dùng screenshot thay vì canvas.toDataURL vì ảnh captcha Shopee nằm trên CDN
      khác origin -> canvas bị taint, toDataURL ném SecurityError.)
  3. Gửi [mảnh ghép, ảnh nền] lên OMOCaptcha, nhận toạ độ đích.
  4. Quy đổi toạ độ ảnh -> pixel CSS trên trang rồi kéo bằng CDP Input.dispatchMouseEvent
     với quỹ đạo giảm tốc + overshoot nhẹ cho giống người thật.

Không nhận diện được captcha -> trả False kèm lý do, luồng gọi fallback về giải tay.
"""
import base64
import json
import os
import random
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

import config
from modules import omocaptcha_api
from utils.error_handler import (
    BrowserClosedError,
    check_browser_alive,
    is_browser_closed_exception,
)
from utils.logger import get_logger

logger = get_logger()

# Giới hạn cứng, tránh treo khi DOM lạ
_MAX_DRAG_STEPS = 40
_MIN_BG_WIDTH = 150
_MIN_BG_HEIGHT = 60
_MIN_PIECE_SIDE = 24
_MAX_PIECE_SIDE = 170
_MIN_PIECE_AREA = 900
_MIN_PIECE_ASPECT = 0.55
_MAX_PIECE_ASPECT = 1.8
_MIN_CAPTURE_SIDE = 20

# ----------------------------------------------------------------------
# JavaScript: quét phần tử ảnh của captcha
# ----------------------------------------------------------------------
_JS_COLLECT = r"""
const out = [];
const refs = [];
const vw = window.innerWidth, vh = window.innerHeight;
const hasImage = (el) => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'canvas' || tag === 'img') return true;
  const bg = getComputedStyle(el).backgroundImage || '';
  return bg.indexOf('url(') !== -1;
};
const rectOk = (r) => {
  if (r.width < 12 || r.height < 12) return false;
  if (r.bottom < 0 || r.right < 0 || r.top > vh + 2 || r.left > vw + 2) return false;
  return true;
};
const nodes = document.querySelectorAll('canvas, img, div, span, section, figure, article, li');
for (const el of nodes) {
  if (refs.length >= 60) break;
  if (!hasImage(el)) continue;
  const st = getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden') continue;
  if (parseFloat(st.opacity || '1') < 0.05) continue;
  const r = el.getBoundingClientRect();
  if (!rectOk(r)) continue;
  refs.push(el);
  out.push({
    i: refs.length - 1,
    tag: el.tagName.toLowerCase(),
    id: el.id || '',
    cls: (el.className && typeof el.className === 'string') ? el.className.slice(0, 160) : '',
    x: r.left, y: r.top, w: r.width, h: r.height,
    src: (el.tagName.toLowerCase() === 'img') ? String(el.currentSrc || el.src || '').slice(0, 160) : ''
  });
}
window.__omoRefs = refs;
window.__omoSaved = {};
return out;
"""

_JS_HIDE = r"""
const refs = window.__omoRefs || [];
window.__omoSaved = window.__omoSaved || {};
for (const i of arguments[0]) {
  const el = refs[i];
  if (!el) continue;
  if (!(i in window.__omoSaved)) window.__omoSaved[i] = el.style.visibility || '';
  el.style.visibility = 'hidden';
}
return true;
"""

_JS_RESTORE = r"""
const refs = window.__omoRefs || [];
const saved = window.__omoSaved || {};
for (const k in saved) {
  const el = refs[k];
  if (el) el.style.visibility = saved[k];
}
window.__omoSaved = {};
return true;
"""

_JS_SCROLL_TO = r"""
const el = (window.__omoRefs || [])[arguments[0]];
if (!el) return false;
try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) { return false; }
return true;
"""

_JS_HANDLE = r"""
const bg = (window.__omoRefs || [])[arguments[0]];
if (!bg) return null;
const br = bg.getBoundingClientRect();
let best = null;
const nodes = document.querySelectorAll('div, span, button, i, a, em');
for (const el of nodes) {
  const r = el.getBoundingClientRect();
  if (r.width < 18 || r.width > 120) continue;
  if (r.height < 18 || r.height > 120) continue;
  if (r.top < br.bottom - 12) continue;
  if (r.top > br.bottom + 170) continue;
  if (r.left < br.left - 70) continue;
  if (r.left > br.left + 150) continue;
  const st = getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden') continue;
  const cur = String(st.cursor || '').toLowerCase();
  const cls = (el.className && typeof el.className === 'string') ? el.className.toLowerCase() : '';
  let score = 0;
  if (cur.indexOf('grab') !== -1 || cur.indexOf('move') !== -1) score += 4;
  else if (cur.indexOf('pointer') !== -1) score += 2;
  if (/slider|handle|thumb|drag/.test(cls)) score += 4;
  else if (/btn|button/.test(cls)) score += 2;
  if (el.tagName.toLowerCase() === 'button') score += 1;
  const cx = r.left + r.width / 2;
  if (cx < br.left + br.width * 0.35) score += 1.5;
  if (!best || score > best.score) {
    best = {
      score: score,
      x: cx,
      y: r.top + r.height / 2,
      w: r.width,
      h: r.height,
      cls: cls.slice(0, 100)
    };
  }
}
return best;
"""


# ----------------------------------------------------------------------
# Tiện ích ảnh
# ----------------------------------------------------------------------
def _png_size(b64: str) -> Tuple[int, int]:
    """Đọc kích thước thật của ảnh PNG từ base64 (chỉ cần header IHDR)."""
    try:
        head = b64[:2048]
        head += "=" * ((-len(head)) % 4)
        raw = base64.b64decode(head, validate=False)
    except Exception:
        return 0, 0
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    width, height = struct.unpack(">II", raw[16:24])
    return int(width), int(height)


def _overlap_area(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    left = max(a["x"], b["x"])
    top = max(a["y"], b["y"])
    right = min(a["x"] + a["w"], b["x"] + b["w"])
    bottom = min(a["y"] + a["h"], b["y"] + b["h"])
    if right <= left or bottom <= top:
        return 0.0
    return (right - left) * (bottom - top)


def _hint_text(cand: Dict[str, Any]) -> str:
    return f"{cand.get('id', '')} {cand.get('cls', '')}".lower()


# ----------------------------------------------------------------------
# Nhận diện vùng captcha
# ----------------------------------------------------------------------
def _collect_candidates(driver) -> List[Dict[str, Any]]:
    """Quét DOM, trả danh sách phần tử có ảnh kèm toạ độ viewport."""
    try:
        raw = driver.execute_script(_JS_COLLECT) or []
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.debug(f"OMOCaptcha: lỗi quét DOM captcha - {e}")
        return []

    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            cand = {
                "i": int(item.get("i", -1)),
                "tag": str(item.get("tag", "")),
                "id": str(item.get("id", "")),
                "cls": str(item.get("cls", "")),
                "x": float(item.get("x", 0) or 0),
                "y": float(item.get("y", 0) or 0),
                "w": float(item.get("w", 0) or 0),
                "h": float(item.get("h", 0) or 0),
                "src": str(item.get("src", "")),
            }
        except (TypeError, ValueError):
            continue
        if cand["i"] < 0 or cand["w"] < 12 or cand["h"] < 12:
            continue
        out.append(cand)
    return out


def _mask_score(cand: Dict[str, Any], bg: Dict[str, Any]) -> float:
    """Điểm càng cao càng giống mảnh ghép."""
    score = 0.0
    hint = _hint_text(cand)

    if any(k in hint for k in ("mask", "puzzle", "piece", "jigsaw", "slice", "cut", "block")):
        score += 6.0
    if any(k in hint for k in ("bg", "background", "back")):
        score -= 4.0

    if _overlap_area(cand, bg) > 0:
        score += 3.0

    side_max = max(cand["w"], cand["h"])
    if abs(cand["w"] - cand["h"]) <= max(6.0, 0.35 * side_max):
        score += 1.5

    bg_area = max(1.0, bg["w"] * bg["h"])
    cand_area = cand["w"] * cand["h"]
    score += 2.0 * (1.0 - min(1.0, cand_area / bg_area))
    return score


def _pick_pair(candidates: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    Chọn cặp (mảnh ghép, ảnh nền) từ danh sách ứng viên.

    Nguyên tắc: ảnh nền phải là phần tử có mảnh ghép nằm trong/đè lên nó.
    Nhờ vậy một div nền trang rộng cả màn hình (cũng "có ảnh") không bị chọn nhầm.
    Trong các ứng viên thoả điều kiện, ưu tiên canvas/img rồi tới vùng gọn nhất.
    """
    if not candidates:
        return None

    bg_pool = [
        c for c in candidates
        if c["w"] >= _MIN_BG_WIDTH and c["h"] >= _MIN_BG_HEIGHT
    ]
    if not bg_pool:
        return None

    def _piece_candidates_for(bg: Dict[str, Any], require_overlap: bool) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        bg_area = max(1.0, bg["w"] * bg["h"])
        for cand in candidates:
            if cand["i"] == bg["i"]:
                continue
            if not (_MIN_PIECE_SIDE <= cand["w"] <= _MAX_PIECE_SIDE):
                continue
            if not (_MIN_PIECE_SIDE <= cand["h"] <= _MAX_PIECE_SIDE):
                continue
            if (cand["w"] * cand["h"]) < _MIN_PIECE_AREA:
                continue
            aspect = cand["w"] / max(1.0, cand["h"])
            if aspect < _MIN_PIECE_ASPECT or aspect > _MAX_PIECE_ASPECT:
                continue
            if (cand["w"] * cand["h"]) > (bg_area * 0.5):
                continue
            if require_overlap and _overlap_area(cand, bg) <= 0:
                continue
            out.append(cand)
        return out

    def _best_piece(bg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Ưu tiên mảnh ghép nằm đè lên nền; không có mới nới lỏng điều kiện
        for require_overlap in (True, False):
            pool = _piece_candidates_for(bg, require_overlap)
            if pool:
                return max(pool, key=lambda c: _mask_score(c, bg))
        return None

    def _bg_rank(item: Tuple[Dict[str, Any], Dict[str, Any]]) -> Tuple[int, float]:
        bg, _piece = item
        is_real_image = 1 if bg["tag"] in ("canvas", "img") else 0
        # Vùng gọn nhất (diện tích nhỏ) thường chính là khung captcha
        return is_real_image, -(bg["w"] * bg["h"])

    evaluated: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for bg in bg_pool:
        piece = _best_piece(bg)
        if piece is not None:
            evaluated.append((bg, piece))

    if not evaluated:
        return None

    bg, mask = max(evaluated, key=_bg_rank)
    return mask, bg


# ----------------------------------------------------------------------
# Chụp ảnh phần tử qua CDP
# ----------------------------------------------------------------------
def _viewport(driver) -> Tuple[int, int]:
    try:
        size = driver.execute_script("return {w: window.innerWidth, h: window.innerHeight};") or {}
        return int(size.get("w", 0) or 0), int(size.get("h", 0) or 0)
    except Exception:
        return 0, 0


def _capture_rect(driver, rect: Dict[str, Any]) -> str:
    """Chụp vùng rect (toạ độ viewport, pixel CSS) -> base64 PNG. '' nếu thất bại."""
    vp_w, vp_h = _viewport(driver)
    if vp_w <= 0 or vp_h <= 0:
        return ""

    x = max(0, int(round(rect["x"])))
    y = max(0, int(round(rect["y"])))
    w = int(round(rect["w"]))
    h = int(round(rect["h"]))

    # Cắt theo viewport để Chrome không từ chối clip
    w = min(w, vp_w - x)
    h = min(h, vp_h - y)
    if w < _MIN_CAPTURE_SIDE or h < _MIN_CAPTURE_SIDE:
        return ""

    try:
        result = driver.execute_cdp_cmd(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "clip": {
                    "x": float(x),
                    "y": float(y),
                    "width": float(w),
                    "height": float(h),
                    "scale": 1,
                },
            },
        )
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.debug(f"OMOCaptcha: chụp captcha thất bại - {e}")
        return ""

    data = str((result or {}).get("data", "") or "")
    return data if len(data) >= 200 else ""


def _capture_pair(driver, mask: Dict[str, Any], bg: Dict[str, Any]) -> Tuple[str, str]:
    """
    Chụp mảnh ghép (đã ẩn nền) và ảnh nền (đã ẩn mảnh ghép).

    Ẩn lớp còn lại bằng visibility:hidden để ảnh chụp chỉ còn đúng phần tử cần.
    """
    mask_b64 = ""
    bg_b64 = ""

    try:
        driver.execute_script(_JS_HIDE, [bg["i"]])
        time.sleep(0.12)
        mask_b64 = _capture_rect(driver, mask)
    finally:
        try:
            driver.execute_script(_JS_RESTORE)
        except Exception:
            pass

    time.sleep(0.08)
    try:
        driver.execute_script(_JS_HIDE, [mask["i"]])
        time.sleep(0.12)
        bg_b64 = _capture_rect(driver, bg)
    finally:
        try:
            driver.execute_script(_JS_RESTORE)
        except Exception:
            pass

    return mask_b64, bg_b64


# ----------------------------------------------------------------------
# Kéo slider bằng CDP
# ----------------------------------------------------------------------
def _dispatch_mouse(
    driver, event_type: str, x: float, y: float, button: str = "none", buttons: int = 0
) -> None:
    params: Dict[str, Any] = {
        "type": event_type,
        "x": round(float(x), 2),
        "y": round(float(y), 2),
        "button": button,
        "buttons": buttons,
    }
    if event_type in ("mousePressed", "mouseReleased"):
        params["clickCount"] = 1
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", params)


def _build_points(dx: float, dy: float) -> List[Tuple[float, float]]:
    """
    Quỹ đạo kéo giống người: giảm tốc dần, overshoot nhẹ rồi chỉnh lại.

    Số bước tỉ lệ quãng đường nhưng luôn <= _MAX_DRAG_STEPS.
    """
    distance = max(abs(dx), abs(dy))
    steps = int(min(_MAX_DRAG_STEPS, max(10, distance / 9.0)))
    overshoot = min(4.0, max(1.0, distance * 0.012))
    sign = 1.0 if dx >= 0 else -1.0

    points: List[Tuple[float, float]] = []
    for idx in range(1, steps + 1):
        t = idx / steps
        eased = 1.0 - (1.0 - t) ** 3
        x = (dx + overshoot * sign) * eased
        y = dy * eased
        if idx < steps:
            y += random.uniform(-1.0, 1.0)
        points.append((x, y))

    points.append((dx + overshoot * sign, dy))
    points.append((dx, dy))
    return points


def _human_drag(driver, x0: float, y0: float, dx: float, dy: float) -> None:
    """Nhấn giữ tại (x0, y0), kéo theo quỹ đạo, thả chuột."""
    _dispatch_mouse(driver, "mouseMoved", x0, y0)
    time.sleep(random.uniform(0.05, 0.12))

    _dispatch_mouse(driver, "mousePressed", x0, y0, button="left", buttons=1)
    time.sleep(random.uniform(0.06, 0.14))

    # Nhích nhẹ trước khi kéo để trang nhận dragstart
    _dispatch_mouse(driver, "mouseMoved", x0 + 2, y0 + random.uniform(-1, 1), buttons=1)
    time.sleep(random.uniform(0.03, 0.07))

    for offset_x, offset_y in _build_points(dx, dy):
        _dispatch_mouse(driver, "mouseMoved", x0 + offset_x, y0 + offset_y, buttons=1)
        time.sleep(random.uniform(0.006, 0.018))

    time.sleep(random.uniform(0.15, 0.35))
    _dispatch_mouse(driver, "mouseReleased", x0 + dx, y0 + dy, button="left", buttons=0)


def _find_handle(driver, bg: Dict[str, Any], mask: Dict[str, Any]) -> Tuple[float, float, str]:
    """Tìm tay kéo của slider; không thấy thì kéo trực tiếp mảnh ghép."""
    try:
        handle = driver.execute_script(_JS_HANDLE, bg["i"])
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        handle = None

    if isinstance(handle, dict):
        try:
            score = float(handle.get("score", 0) or 0)
            hx = float(handle.get("x", 0) or 0)
            hy = float(handle.get("y", 0) or 0)
        except (TypeError, ValueError):
            score, hx, hy = 0.0, 0.0, 0.0
        if score >= 2.0 and hx > 0 and hy > 0:
            return hx, hy, f"handle(score={score:.1f},cls={str(handle.get('cls', ''))[:40]})"

    return (
        mask["x"] + mask["w"] / 2.0,
        mask["y"] + mask["h"] / 2.0,
        "mask-piece",
    )


# ----------------------------------------------------------------------
# Debug
# ----------------------------------------------------------------------
def _debug_dir() -> str:
    return os.path.join(
        getattr(config, "APP_DIR", os.getcwd()),
        getattr(config, "LOG_DIR", "logs"),
        "captcha_debug",
    )


def _save_debug_artifact(name: str, payload: bytes) -> None:
    try:
        folder = _debug_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as fh:
            fh.write(payload)
        logger.debug(f"OMOCaptcha: đã lưu debug {path}")
    except Exception as e:
        logger.debug(f"OMOCaptcha: không lưu được debug - {e}")


def _dump_debug(
    driver,
    tag: str,
    candidates: List[Dict[str, Any]],
    mask_b64: str = "",
    bg_b64: str = "",
) -> None:
    """Lưu DOM + ảnh captcha để chỉnh selector khi Shopee đổi giao diện."""
    if not getattr(config, "OMO_DEBUG_IMAGES", False):
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = f"{stamp}_{tag or 'shopee'}"

    try:
        url = ""
        try:
            url = (driver.current_url or "")[:300]
        except Exception:
            url = ""
        info = {"url": url, "candidates": candidates}
        _save_debug_artifact(
            f"{prefix}_dom.json",
            json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    except Exception:
        pass

    for suffix, data in (("mask", mask_b64), ("background", bg_b64)):
        if not data:
            continue
        try:
            _save_debug_artifact(f"{prefix}_{suffix}.png", base64.b64decode(data))
        except Exception:
            pass


# ----------------------------------------------------------------------
# API chính
# ----------------------------------------------------------------------
def solve_shopee_captcha(
    driver,
    client: Optional["omocaptcha_api.OmocaptchaClient"] = None,
    rotate: Optional[bool] = None,
    tag: str = "",
) -> Tuple[bool, str]:
    """
    Giải 1 lượt captcha Shopee đang hiển thị.

    Trả (True, mô tả) nếu đã kéo xong (luồng gọi tự kiểm tra captcha đã qua chưa),
    (False, lý do) nếu không giải được để rơi về chế độ giải tay.
    """
    check_browser_alive(driver)

    if client is None:
        client = omocaptcha_api.get_client()
    if client is None:
        return False, "chưa cấu hình/bật OMOCaptcha"

    cfg = omocaptcha_api.get_config()
    solve_timeout = float(cfg.get("solve_timeout", 120) or 120)
    if rotate is None:
        rotate = bool(cfg.get("shopee_rotate", False))

    candidates = _collect_candidates(driver)
    pair = _pick_pair(candidates)
    if pair is None:
        _dump_debug(driver, tag, candidates)
        return False, f"không nhận diện được vùng captcha ({len(candidates)} ứng viên)"

    mask, bg = pair

    # Captcha có thể nằm ngoài khung nhìn -> cuộn vào giữa rồi quét lại toạ độ
    vp_w, vp_h = _viewport(driver)
    if bg["y"] < 0 or bg["y"] + bg["h"] > vp_h or bg["x"] < 0 or bg["x"] + bg["w"] > vp_w:
        try:
            driver.execute_script(_JS_SCROLL_TO, bg["i"])
            time.sleep(0.35)
            candidates = _collect_candidates(driver)
            pair = _pick_pair(candidates)
            if pair is None:
                _dump_debug(driver, tag, candidates)
                return False, "captcha ngoài khung nhìn và không quét lại được"
            mask, bg = pair
        except Exception as e:
            if is_browser_closed_exception(e):
                raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e

    mask_b64, bg_b64 = _capture_pair(driver, mask, bg)
    if not mask_b64 or not bg_b64:
        _dump_debug(driver, tag, candidates, mask_b64, bg_b64)
        return False, "chụp ảnh captcha thất bại (không lấy được mask/nền)"

    _dump_debug(driver, tag, candidates, mask_b64, bg_b64)

    logger.info(
        f"🔎 OMOCaptcha: mảnh ghép {int(mask['w'])}x{int(mask['h'])} "
        f"@({int(mask['x'])},{int(mask['y'])}), nền {int(bg['w'])}x{int(bg['h'])} "
        f"@({int(bg['x'])},{int(bg['y'])})"
    )

    try:
        solution = client.solve_shopee_slider(
            mask_b64, bg_b64, rotate=rotate, timeout=solve_timeout
        )
    except omocaptcha_api.OmocaptchaError as e:
        return False, f"OMOCaptcha lỗi: {e}"

    # --- Quy đổi toạ độ ảnh nền (pixel ảnh) sang pixel CSS trên trang ---
    png_w, _png_h = _png_size(bg_b64)
    scale = (png_w / bg["w"]) if (png_w > 0 and bg["w"] > 0) else 1.0
    if scale <= 0:
        scale = 1.0

    piece_offset_x = (mask["x"] - bg["x"]) * scale
    piece_offset_y = (mask["y"] - bg["y"]) * scale

    target_x = float(solution.get("x", 0) or 0)
    target_y = float(solution.get("y", 0) or 0)
    mode = str(solution.get("mode", "x"))

    dx_img = target_x - piece_offset_x
    dy_img = (target_y - piece_offset_y) if mode == "point" else 0.0

    dx_css = dx_img / scale
    dy_css = dy_img / scale

    if dx_css < 3.0:
        return False, f"toạ độ đích không hợp lệ (dx={dx_css:.1f}px, mode={mode})"
    if dx_css > bg["w"]:
        logger.warning(
            f"⚠️ OMOCaptcha: quãng kéo {dx_css:.1f}px vượt bề rộng ảnh nền {bg['w']:.0f}px, vẫn thử."
        )

    handle_x, handle_y, handle_src = _find_handle(driver, bg, mask)
    logger.info(
        f"🖱️ OMOCaptcha: kéo {dx_css:.1f}px (mode={mode}, scale={scale:.2f}, "
        f"điểm bắt đầu={handle_src})"
    )

    try:
        _human_drag(driver, handle_x, handle_y, dx_css, dy_css)
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        return False, f"lỗi khi kéo slider: {e}"

    return True, f"đã kéo {dx_css:.0f}px (mode={mode}, {handle_src})"


def dump_captcha_debug(driver, tag: str = "manual") -> str:
    """
    Lưu ảnh + DOM captcha hiện tại ra logs/captcha_debug/ để soi selector.

    Trả đường dẫn thư mục debug.
    """
    candidates = _collect_candidates(driver)
    pair = _pick_pair(candidates)
    mask_b64 = ""
    bg_b64 = ""
    if pair is not None:
        mask, bg = pair
        mask_b64, bg_b64 = _capture_pair(driver, mask, bg)
    _dump_debug(driver, tag, candidates, mask_b64, bg_b64)
    return _debug_dir()
