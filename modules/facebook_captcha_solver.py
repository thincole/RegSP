"""
Tự động giải FunCaptcha (Arkose Labs) của Facebook bằng OMOCaptcha `FuncaptchaImageTask`.

Dạng challenge xử lý được (theo tài liệu OMOCaptcha):
  - "Use the arrows to pick the image where all the darts add up to the number in the left image"
  - "Use the arrows to rotate the object to face in the direction of the hand"
  - "Use the arrows to match the icon on the left to the cup with the most liquid"
  Tất cả đều là dạng: ảnh mẫu bên trái + dãy ảnh bên phải + 2 mũi tên + Submit.

Luồng chạy (mỗi vòng challenge):
  1. Tìm iframe Arkose của Facebook, đọc câu hỏi + khung challenge bên trong.
  2. Chụp đúng khung challenge (không chụp cả màn hình) ở toạ độ tuyệt đối của trang.
  3. Gửi `FuncaptchaImageTask` kèm nguyên văn câu hỏi -> nhận `index` (1-based).
  4. Bấm mũi tên phải `index - 1` lần (ảnh đang đứng ở vị trí thứ nhất) rồi bấm Submit.
  5. Lặp lại cho tới khi hết vòng ("x of N") hoặc challenge biến mất.

Không tìm được khung challenge -> trả False để luồng gọi rơi về chế độ giải tay.
"""
import base64
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

import config
from modules import omocaptcha_api
from utils.error_handler import (
    BrowserClosedError,
    check_browser_alive,
    is_browser_closed_exception,
)
from utils.logger import get_logger

logger = get_logger()

_FRAME_HINTS = ("arkose", "funcaptcha", "fc-", "fc_", "enforcement", "captcha", "challenge")

_INSTRUCTION_RE = re.compile(r"(use the arrows[^\n\r]{0,240})", re.IGNORECASE)
_ROUND_RE = re.compile(r"\((\d+)\s*(?:of|/)\s*(\d+)\)", re.IGNORECASE)

# Câu hỏi mặc định nếu không đọc được text (đúng dạng phổ biến nhất của Facebook)
_DEFAULT_INSTRUCTION = "Use the arrows to match the icon on the left to the cup with the most liquid"

# ----------------------------------------------------------------------
# JavaScript chạy BÊN TRONG iframe challenge
# ----------------------------------------------------------------------
_JS_READ_STATE = r"""
const body = document.body;
const txt = body ? (body.innerText || '') : '';
const low = txt.toLowerCase();
const has = low.indexOf('arrow') !== -1 || low.indexOf('submit') !== -1 ||
            low.indexOf('select all') !== -1 || low.indexOf('match the') !== -1;
let best = null;
if (body) {
  const nodes = document.querySelectorAll('div, section, main, form');
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width < 180 || r.height < 180) continue;
    if (r.width > 1100 || r.height > 1100) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const t = (el.innerText || '').toLowerCase();
    if (t.indexOf('arrow') === -1 && t.indexOf('submit') === -1) continue;
    if (el.querySelectorAll('img, canvas, picture').length < 2) continue;
    const area = r.width * r.height;
    if (!best || area < best.area) {
      best = {x: r.left, y: r.top, w: r.width, h: r.height, area: area};
    }
  }
  if (!best) {
    const r = body.getBoundingClientRect();
    if (r.width >= 150 && r.height >= 150) {
      best = {x: r.left, y: r.top, w: r.width, h: r.height, area: r.width * r.height};
    }
  }
}
return {has: has, text: txt.slice(0, 600), rect: best,
        vw: window.innerWidth, vh: window.innerHeight};
"""

# Biến thể chặt hơn cho document chính của Facebook (tránh nhận nhầm nội dung trang)
_JS_READ_STATE_MAIN = r"""
const body = document.body;
const txt = body ? (body.innerText || '') : '';
const low = txt.toLowerCase();
const has = low.indexOf('use the arrows') !== -1;
let best = null;
if (body) {
  for (const el of document.querySelectorAll('div, section, main, form')) {
    const r = el.getBoundingClientRect();
    if (r.width < 180 || r.height < 180) continue;
    if (r.width > 1100 || r.height > 1100) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const t = (el.innerText || '').toLowerCase();
    if (t.indexOf('use the arrows') === -1) continue;
    if (el.querySelectorAll('img, canvas, picture').length < 2) continue;
    const area = r.width * r.height;
    if (!best || area < best.area) {
      best = {x: r.left, y: r.top, w: r.width, h: r.height, area: area};
    }
  }
}
return {has: has, text: txt.slice(0, 600), rect: best,
        vw: window.innerWidth, vh: window.innerHeight};
"""

_JS_FIND_ARROW = r"""
const dir = String(arguments[0] || 'right');
const nodes = document.querySelectorAll('button, div, span, a, svg, i');
const cands = [];
const vw = window.innerWidth;
for (const el of nodes) {
  const r = el.getBoundingClientRect();
  if (r.width < 16 || r.width > 96) continue;
  if (r.height < 16 || r.height > 96) continue;
  const ratio = r.width / Math.max(1, r.height);
  if (ratio < 0.5 || ratio > 2.0) continue;
  const st = getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden') continue;
  if (parseFloat(st.opacity || '1') < 0.15) continue;
  if (r.bottom < 0 || r.top > window.innerHeight) continue;
  const cls = ((el.className && typeof el.className === 'string') ? el.className : '') + ' ' + (el.id || '');
  const label = (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '');
  const hint = (cls + ' ' + label).toLowerCase();
  let score = 0;
  if (/arrow|next|prev|left|right|carousel|nav/.test(hint)) score += 4;
  if (st.cursor === 'pointer') score += 2;
  if (el.tagName.toLowerCase() === 'button') score += 1;
  if (score <= 0) continue;
  cands.push({el: el, cx: r.left + r.width / 2, score: score});
}
if (cands.length < 2) return null;
cands.sort(function (a, b) { return a.cx - b.cx; });
let pick = null;
if (dir === 'left') {
  pick = cands[0];
} else {
  pick = cands[cands.length - 1];
}
if (!pick || pick.cx === vw / 2) return null;
return pick.el;
"""

_JS_FIND_SUBMIT = r"""
const nodes = document.querySelectorAll('button, div, span, a, input');
for (const el of nodes) {
  const txt = (el.innerText || el.value || '').trim();
  if (txt.toLowerCase() !== 'submit') continue;
  const r = el.getBoundingClientRect();
  if (r.width < 40 || r.height < 16) continue;
  const st = getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden') continue;
  return el;
}
return null;
"""

# Selector dự phòng (dùng khi heuristic JS không tìm ra)
_ARROW_SELECTORS = {
    "left": [
        (By.CSS_SELECTOR, "[aria-label*='left' i]"),
        (By.CSS_SELECTOR, ".funcaptcha__arrow--left"),
        (By.CSS_SELECTOR, "[class*='arrow'][class*='left']"),
        (By.CSS_SELECTOR, "[id*='left']"),
        (By.CSS_SELECTOR, "[class*='prev']"),
    ],
    "right": [
        (By.CSS_SELECTOR, "[aria-label*='right' i]"),
        (By.CSS_SELECTOR, ".funcaptcha__arrow--right"),
        (By.CSS_SELECTOR, "[class*='arrow'][class*='right']"),
        (By.CSS_SELECTOR, "[id*='right']"),
        (By.CSS_SELECTOR, "[class*='next']"),
    ],
}

_SUBMIT_SELECTORS = [
    (By.CSS_SELECTOR, "#submit"),
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.CSS_SELECTOR, ".funcaptcha__submit"),
    (By.CSS_SELECTOR, "[class*='submit' i]"),
]

# Cầu dao bảo vệ: khi endpoint FuncaptchaImageTask trả ERROR_JOB_STATUS liên tiếp
# (kể cả với ảnh mẫu chính chủ của OMOCaptcha) thì tắt hẳn nhánh Facebook trong phiên này,
# tránh mỗi captcha lại chờ vô ích trước khi rơi về giải tay.
_JOB_FAILURE_LIMIT = 3
_job_failure_count = 0
_api_broken = False


def _note_job_failure() -> None:
    global _job_failure_count, _api_broken
    _job_failure_count += 1
    if _job_failure_count >= _JOB_FAILURE_LIMIT and not _api_broken:
        _api_broken = True
        logger.warning(
            "⛔ OMOCaptcha(FB): FuncaptchaImageTask lỗi liên tiếp "
            f"({_job_failure_count} lần) -> tắt giải FunCaptcha tự động trong phiên này, "
            "dùng giải tay. Kiểm tra dịch vụ/liên hệ OMOCaptcha nếu cần bật lại."
        )


def _note_job_success() -> None:
    """Job chạy được -> xoá đếm lỗi liên tiếp."""
    global _job_failure_count
    _job_failure_count = 0


def reset_breaker() -> None:
    """Reset cầu dao (dùng khi muốn thử lại nhánh Facebook trong cùng tiến trình)."""
    global _job_failure_count, _api_broken
    _job_failure_count = 0
    _api_broken = False


# ----------------------------------------------------------------------
# Debug
# ----------------------------------------------------------------------
def _debug_dir() -> str:
    return os.path.join(
        getattr(config, "APP_DIR", os.getcwd()),
        getattr(config, "LOG_DIR", "logs"),
        "captcha_debug",
    )


def _save_debug(name: str, payload: bytes) -> None:
    if not getattr(config, "OMO_DEBUG_IMAGES", False):
        return
    try:
        folder = _debug_dir()
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(payload)
        logger.debug(f"OMOCaptcha(FB): đã lưu debug {os.path.join(folder, name)}")
    except Exception as e:
        logger.debug(f"OMOCaptcha(FB): không lưu được debug - {e}")


# ----------------------------------------------------------------------
# Tìm iframe challenge
# ----------------------------------------------------------------------
def _candidate_frames(driver) -> List[Any]:
    """Danh sách iframe khả nghi, ưu tiên iframe có src/id chứa 'arkose'/'funcaptcha'."""
    driver.switch_to.default_content()
    scored: List[Tuple[int, Any]] = []
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        return []

    for frame in frames:
        try:
            if not frame.is_displayed():
                continue
            rect = frame.rect or {}
            if float(rect.get("width", 0) or 0) < 150 or float(rect.get("height", 0) or 0) < 150:
                continue
            meta = " ".join(
                str(frame.get_attribute(attr) or "")
                for attr in ("src", "id", "name", "title")
            ).lower()
            priority = 0 if any(hint in meta for hint in _FRAME_HINTS) else 1
            scored.append((priority, frame))
        except Exception:
            continue

    scored.sort(key=lambda item: item[0])
    return [frame for _priority, frame in scored]


def _read_state(driver, script: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Đọc trạng thái challenge trong context hiện tại (iframe hoặc document chính)."""
    try:
        state = driver.execute_script(script or _JS_READ_STATE)
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        return None
    return state if isinstance(state, dict) else None


def _extract_instruction(text: str) -> str:
    """Rút câu hỏi từ text của challenge."""
    raw = (text or "").strip()
    if not raw:
        return ""
    found = _INSTRUCTION_RE.search(raw)
    if found:
        return re.sub(r"\s+", " ", found.group(1)).strip()
    for line in raw.splitlines():
        line = line.strip()
        if len(line) >= 15 and not _ROUND_RE.fullmatch(line):
            return re.sub(r"\s+", " ", line)[:300]
    return ""


# ----------------------------------------------------------------------
# Chụp ảnh challenge (toạ độ tuyệt đối của trang)
# ----------------------------------------------------------------------
def _viewport(driver) -> Tuple[int, int]:
    try:
        size = driver.execute_script("return {w: window.innerWidth, h: window.innerHeight};") or {}
        return int(size.get("w", 0) or 0), int(size.get("h", 0) or 0)
    except Exception:
        return 0, 0


def _capture_clip(driver, clip: Dict[str, float]) -> str:
    """Chụp vùng clip (toạ độ viewport của trang) -> base64 PNG."""
    vp_w, vp_h = _viewport(driver)
    if vp_w <= 0 or vp_h <= 0:
        return ""

    x = max(0, int(round(clip["x"])))
    y = max(0, int(round(clip["y"])))
    w = int(round(clip["w"]))
    h = int(round(clip["h"]))
    w = min(w, vp_w - x)
    h = min(h, vp_h - y)
    if w < 80 or h < 80:
        return ""

    try:
        result = driver.execute_cdp_cmd(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "clip": {"x": float(x), "y": float(y), "width": float(w), "height": float(h), "scale": 1},
            },
        )
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.debug(f"OMOCaptcha(FB): chụp challenge thất bại - {e}")
        return ""

    data = str((result or {}).get("data", "") or "")
    return data if len(data) >= 300 else ""


# ----------------------------------------------------------------------
# Bấm mũi tên / Submit
# ----------------------------------------------------------------------
def _click_element(driver, element) -> bool:
    """Click thật (ActionChains) trước, fallback JS click."""
    try:
        ActionChains(driver).move_to_element(element).pause(0.05).click().perform()
        return True
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].click();", element)
        return True
    except Exception:
        return False


def _find_arrow(driver, direction: str):
    """Tìm nút mũi tên trong iframe hiện tại."""
    try:
        element = driver.execute_script(_JS_FIND_ARROW, direction)
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        element = None
    if element is not None:
        return element

    for by, value in _ARROW_SELECTORS.get(direction, []):
        try:
            for cand in driver.find_elements(by, value):
                if cand.is_displayed():
                    return cand
        except Exception:
            continue
    return None


def _find_submit(driver):
    """Tìm nút Submit trong iframe hiện tại."""
    try:
        element = driver.execute_script(_JS_FIND_SUBMIT)
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        element = None
    if element is not None:
        return element

    for by, value in _SUBMIT_SELECTORS:
        try:
            for cand in driver.find_elements(by, value):
                if cand.is_displayed():
                    return cand
        except Exception:
            continue
    return None


def _press_right_arrow(driver, times: int) -> bool:
    """Bấm mũi tên phải `times` lần. True nếu bấm được ít nhất 1 lần."""
    pressed = 0
    for _ in range(max(0, int(times))):
        arrow = _find_arrow(driver, "right")
        if arrow is None:
            break
        if not _click_element(driver, arrow):
            break
        pressed += 1
        time.sleep(0.35)
    return pressed > 0


# ----------------------------------------------------------------------
# Định vị challenge + chụp
# ----------------------------------------------------------------------
def _locate_challenge(driver) -> Tuple[Optional[Any], Optional[Dict[str, Any]], str]:
    """
    Tìm context chứa challenge thật và switch vào trong.

    Trả (context, state, lý do). context = WebElement nếu nằm trong iframe,
    = "main" nếu Arkose render thẳng trong document chính, None nếu không thấy.
    """
    for frame in _candidate_frames(driver):
        try:
            driver.switch_to.frame(frame)
        except Exception:
            continue
        state = _read_state(driver)
        if state and state.get("has") and isinstance(state.get("rect"), dict):
            return frame, state, ""
        driver.switch_to.default_content()

    # Không có iframe challenge -> thử document chính (một số bản Facebook render Arkose inline)
    driver.switch_to.default_content()
    state = _read_state(driver, _JS_READ_STATE_MAIN)
    if state and state.get("has") and isinstance(state.get("rect"), dict):
        return "main", state, ""

    return None, None, "không thấy khung challenge Arkose (iframe lẫn document chính)"


def _capture_challenge(driver, frame, state: Dict[str, Any], tag: str) -> str:
    """Chụp khung challenge ở toạ độ tuyệt đối rồi trả base64."""
    inner = state.get("rect") or {}
    try:
        inner_rect = {
            "x": float(inner.get("x", 0) or 0),
            "y": float(inner.get("y", 0) or 0),
            "w": float(inner.get("w", 0) or 0),
            "h": float(inner.get("h", 0) or 0),
        }
    except (TypeError, ValueError):
        inner_rect = {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}

    driver.switch_to.default_content()

    if frame == "main":
        # Arkose render thẳng trong document chính: rect đã là toạ độ viewport của trang
        clip = dict(inner_rect)
    else:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", frame)
            time.sleep(0.35)
            frame_rect = frame.rect or {}
            fx = float(frame_rect.get("x", 0) or 0)
            fy = float(frame_rect.get("y", 0) or 0)
        except Exception as e:
            if is_browser_closed_exception(e):
                raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
            return ""

        if inner_rect["w"] <= 0 or inner_rect["h"] <= 0:
            inner_rect = {
                "x": 0.0,
                "y": 0.0,
                "w": float(frame_rect.get("width", 0) or 0),
                "h": float(frame_rect.get("height", 0) or 0),
            }
        clip = {
            "x": fx + inner_rect["x"],
            "y": fy + inner_rect["y"],
            "w": inner_rect["w"],
            "h": inner_rect["h"],
        }

    shot = _capture_clip(driver, clip)

    if shot and getattr(config, "OMO_DEBUG_IMAGES", False):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            _save_debug(f"{stamp}_{tag or 'fb'}_challenge.png", base64.b64decode(shot))
            _save_debug(
                f"{stamp}_{tag or 'fb'}_state.json",
                json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        except Exception:
            pass

    return shot


# ----------------------------------------------------------------------
# API chính
# ----------------------------------------------------------------------
def solve_facebook_funcaptcha(
    driver,
    client: Optional["omocaptcha_api.OmocaptchaClient"] = None,
    max_rounds: Optional[int] = None,
    tag: str = "fb",
) -> Tuple[bool, str]:
    """
    Giải FunCaptcha của Facebook theo từng vòng cho tới khi xong.

    Trả (True, mô tả) nếu đã xử lý ít nhất 1 vòng challenge,
    (False, lý do) nếu không tìm thấy challenge hoặc API lỗi (để fallback giải tay).
    """
    check_browser_alive(driver)

    if client is None:
        client = omocaptcha_api.get_client()
    if client is None:
        return False, "chưa cấu hình/bật OMOCaptcha"
    if _api_broken:
        return False, "FuncaptchaImageTask đang lỗi liên tiếp, đã tắt trong phiên này"

    cfg = omocaptcha_api.get_config()
    solve_timeout = float(cfg.get("solve_timeout", 120) or 120)
    rounds_limit = int(max_rounds or getattr(config, "OMO_FB_MAX_ROUNDS", 6) or 6)

    handled = 0
    for round_no in range(1, rounds_limit + 1):
        check_browser_alive(driver)

        frame, state, reason = _locate_challenge(driver)
        if frame is None:
            driver.switch_to.default_content()
            if handled > 0:
                return True, f"đã xử lý {handled} vòng challenge"
            return False, reason

        text = str(state.get("text", "") or "")
        instruction = _extract_instruction(text) or _DEFAULT_INSTRUCTION
        round_match = _ROUND_RE.search(text)
        round_label = round_match.group(0) if round_match else ""

        image_b64 = _capture_challenge(driver, frame, state, tag=f"{tag}_{round_no}")
        driver.switch_to.default_content()

        if not image_b64:
            return False, "chụp ảnh challenge thất bại"

        logger.info(
            f"🤖 OMOCaptcha(FB): vòng {round_no}{(' ' + round_label) if round_label else ''} "
            f"-> gửi challenge: \"{instruction[:70]}\""
        )

        try:
            index = client.solve_funcaptcha_image(
                image_b64, instruction, timeout=solve_timeout
            )
        except omocaptcha_api.OmocaptchaError as e:
            driver.switch_to.default_content()
            if e.code in ("ERROR_JOB_STATUS", "INVALID_SOLUTION"):
                _note_job_failure()
            return False, f"OMOCaptcha lỗi: {e}"

        clicks = max(0, index - 1)
        _note_job_success()
        logger.info(f"🖱️ OMOCaptcha(FB): index={index} -> bấm mũi tên phải {clicks} lần + Submit")

        # Quay lại đúng iframe để bấm (frame có thể đã đổi sau mỗi vòng -> tìm lại)
        frame2, state2, reason2 = _locate_challenge(driver)
        if frame2 is None:
            driver.switch_to.default_content()
            if handled > 0:
                return True, f"đã xử lý {handled} vòng challenge"
            return False, reason2

        if clicks > 0 and not _press_right_arrow(driver, clicks):
            driver.switch_to.default_content()
            return False, "không bấm được mũi tên phải"

        submit = _find_submit(driver)
        if submit is None:
            driver.switch_to.default_content()
            return False, "không tìm thấy nút Submit"
        if not _click_element(driver, submit):
            driver.switch_to.default_content()
            return False, "không bấm được Submit"

        driver.switch_to.default_content()
        handled += 1

        # Chờ vòng tiếp theo (Arkose chuyển ảnh) hoặc challenge đóng hẳn
        time.sleep(2.2)
        check_browser_alive(driver)

    driver.switch_to.default_content()
    logger.warning(f"⚠️ OMOCaptcha(FB): đã chạy {handled} vòng, chưa hết challenge -> nhường giải tay.")
    return handled > 0, f"đã chạy {handled} vòng"


def is_funcaptcha_present(driver) -> bool:
    """True nếu đang có iframe Arkose chứa challenge hiển thị."""
    driver.switch_to.default_content()
    frame, state, _reason = _locate_challenge(driver)
    driver.switch_to.default_content()
    return frame is not None and state is not None


def dump_facebook_captcha_debug(driver, tag: str = "fb_manual") -> str:
    """Lưu ảnh + state challenge ra logs/captcha_debug/ để soi."""
    driver.switch_to.default_content()
    frame, state, _reason = _locate_challenge(driver)
    if frame is not None and state is not None:
        _capture_challenge(driver, frame, state, tag=tag)
    driver.switch_to.default_content()
    return _debug_dir()
