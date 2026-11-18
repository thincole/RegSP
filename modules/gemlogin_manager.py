"""
Quản lý Gemlogin profiles qua Local API.
API base: http://127.0.0.1:{port}

Endpoints (tất cả dùng GET):
  - GET /api/profiles              -> Danh sách profiles {"success":true, "data":[...]}
  - GET /api/profiles/start/{id}   -> Khởi động profile {"success":true, "data":{"remote_debugging_address":"127.0.0.1:xxxxx"}}
  - GET /api/profiles/close/{id}   -> Đóng profile {"success":true}

Tham khảo: E:\\0- Get Cookie\\getcookies.md
"""
import io
import json
import os
import re
import subprocess
import time
import zipfile
import threading
import urllib.parse
import urllib.request
from typing import Optional, Dict

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService

import config
from utils.logger import get_logger
from utils.error_handler import ProfileNotFoundError, ProfileStartError, retry

logger = get_logger()

# API base URL
API_BASE = f"http://127.0.0.1:{config.GEMLOGIN_API_PORT}"
_start_profile_lock = threading.Lock()
_ensure_gemlogin_lock = threading.Lock()
_profile_paths: Dict[str, str] = {}


def is_gemlogin_api_ready(timeout: float = 2.0) -> bool:
    """Kiểm tra GemLogin Local API đã sẵn sàng phản hồi (DeviceValid/profiles OK) chưa."""
    try:
        resp = requests.get(f"{API_BASE}/api/profiles?per_page=1", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return isinstance(data, dict) and data.get("success") is True
    except Exception:
        pass
    return False


def _is_gemlogin_process_running() -> bool:
    """Kiểm tra tiến trình GemLogin.exe đang chạy hay chưa (Windows)."""
    if os.name != "nt":
        return False
    try:
        res = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq GemLogin.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=0x08000000,
        )
        return "gemlogin.exe" in (res.stdout or "").lower()
    except Exception:
        return False


def _find_gemlogin_executable() -> Optional[str]:
    """Tự động tìm file GemLogin.exe qua config, đường dẫn cài đặt phổ biến và shortcut (.lnk)."""
    cfg_path = getattr(config, "GEMLOGIN_EXE_PATH", "")
    candidates = []
    if cfg_path:
        candidates.append(cfg_path)

    for env_key, rel_parts in (
        ("ProgramFiles", ("GemLogin", "GemLogin.exe")),
        ("ProgramFiles(x86)", ("GemLogin", "GemLogin.exe")),
        ("LOCALAPPDATA", ("Programs", "GemLogin", "GemLogin.exe")),
        ("LOCALAPPDATA", ("Programs", "gemlogin", "GemLogin.exe")),
    ):
        base = os.environ.get(env_key, "")
        if base:
            candidates.append(os.path.join(base, *rel_parts))

    candidates.extend([
        r"C:\Program Files\GemLogin\GemLogin.exe",
        r"D:\GemLogin\GemLogin.exe",
        r"E:\GemLogin\GemLogin.exe",
    ])

    for path in candidates:
        if path and os.path.isfile(path):
            return path

    if os.name == "nt":
        shortcut_dirs = [
            os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
            os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
        ]
        lnk_files = []
        for sdir in shortcut_dirs:
            if not sdir or not os.path.isdir(sdir):
                continue
            for root, _dirs, files in os.walk(sdir):
                for fn in files:
                    if "gemlogin" in fn.lower() and fn.lower().endswith(".lnk"):
                        lnk_files.append(os.path.join(root, fn))

        for lnk in lnk_files:
            try:
                ps_cmd = (
                    "$sh = New-Object -ComObject WScript.Shell; "
                    f"$s = $sh.CreateShortcut('{lnk.replace(chr(39), chr(39)*2)}'); "
                    "Write-Output $s.TargetPath"
                )
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=0x08000000,
                )
                target = (res.stdout or "").strip().splitlines()
                target_path = target[-1].strip() if target else ""
                if target_path and os.path.isfile(target_path):
                    return target_path
            except Exception:
                pass

    return None


def _launch_detached_gemlogin(exe_path: str) -> None:
    """
    Khởi chạy GemLogin hoàn toàn tách biệt (ngoài Job Object / process tree của tool)
    để khi tắt tool thì GemLogin không bị tắt theo.
    """
    work_dir = os.path.dirname(exe_path) or None
    if os.name == "nt":
        detached = 0x00000008  # DETACHED_PROCESS
        new_group = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        try:
            subprocess.Popen(
                ["explorer.exe", os.path.normpath(exe_path)],
                creationflags=detached | new_group,
                close_fds=True,
            )
            return
        except Exception as e:
            logger.debug(f"explorer.exe khởi chạy GemLogin lỗi ({e}), thử Popen tách Job...")

        for flags in (breakaway | detached | new_group, detached | new_group):
            try:
                subprocess.Popen(
                    [exe_path],
                    cwd=work_dir,
                    creationflags=flags,
                    close_fds=True,
                )
                return
            except OSError:
                continue
        os.startfile(exe_path)
    else:
        subprocess.Popen(
            [exe_path],
            cwd=work_dir,
            start_new_session=True,
            close_fds=True,
        )


def ensure_gemlogin_running(wait_timeout: int = 60, stop_flag=None) -> bool:
    """
    Kiểm tra GemLogin đã bật chưa; nếu chưa bật thì tự động khởi động GemLogin.exe
    và chờ Local API (127.0.0.1:1010) sẵn sàng.
    """
    if is_gemlogin_api_ready(timeout=2.0):
        logger.debug("GemLogin API đã đang chạy sẵn.")
        return True

    with _ensure_gemlogin_lock:
        if is_gemlogin_api_ready(timeout=2.0):
            return True

        logger.info("GemLogin chưa bật (API không phản hồi) -> Đang kiểm tra & tự khởi động GemLogin...")
        if _is_gemlogin_process_running():
            logger.info("Phát hiện tiến trình GemLogin.exe đang khởi chạy -> Chờ API sẵn sàng...")
        else:
            exe_path = _find_gemlogin_executable()
            if not exe_path:
                logger.error("Không tìm thấy GemLogin.exe để tự động bật! Vui lòng mở GemLogin thủ công.")
                return False

            logger.info(f"Đang tự động bật GemLogin: {exe_path}")
            try:
                _launch_detached_gemlogin(exe_path)
            except Exception as e:
                logger.error(f"Không thể khởi động GemLogin ({exe_path}): {e}")
                return False

        deadline = time.time() + max(5, int(wait_timeout))
        while time.time() < deadline:
            if stop_flag is not None and stop_flag.is_set():
                logger.warning("Đã dừng chờ khởi động GemLogin theo yêu cầu.")
                return False
            if is_gemlogin_api_ready(timeout=2.0):
                logger.info("✅ GemLogin đã khởi động và API sẵn sàng!")
                return True
            time.sleep(1.5)

        logger.error(f"Đã chờ {wait_timeout}s nhưng GemLogin API ({API_BASE}) vẫn chưa phản hồi.")
        return False


def _api_get(endpoint: str, timeout: int = 15) -> dict:
    """Gửi GET request tới Gemlogin API."""
    url = f"{API_BASE}{endpoint}"
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        return data
    except requests.RequestException as e:
        logger.error(f"Gemlogin API error: GET {url} -> {e}")
        raise


def _get_candidate_profile_names(profile_name: str) -> list[str]:
    """
    Sinh danh sách tên profile ứng viên (ưu tiên tên chính xác trước).
    Xử lý trường hợp tạo hàng loạt trên Gemlogin với tiền tố 'PH40' + số thứ tự 1..100
    khiến profile từ thứ 10..100 bị đặt tên là 'PH4010'..'PH4099', 'PH40100'
    thay vì 'PH410'..'PH499', 'PH500'.
    """
    import re
    clean = profile_name.strip()
    candidates = [clean]

    # Trường hợp Sheet ghi 'PH410'..'PH499' -> thêm ứng viên 'PH4010'..'PH4099'
    # Trường hợp Sheet ghi 'PH500' -> thêm ứng viên 'PH40100'
    m = re.match(r"^([A-Za-z]+)(\d)(\d{2})$", clean)
    if m:
        prefix, hundreds, rest = m.group(1), int(m.group(2)), m.group(3)
        if rest == "00" and hundreds > 0:
            alt = f"{prefix}{hundreds - 1}0100"
        else:
            alt = f"{prefix}{hundreds}0{rest}"
        if alt not in candidates:
            candidates.append(alt)

    return candidates


def find_profile_by_name(profile_name: str) -> str:
    """
    Tìm profile ID từ tên profile.

    Thử 2 cách:
    1. GET /api/profiles?search={name} (nếu API hỗ trợ)
    2. GET /api/profiles?per_page=1000 (lấy hết, tìm thủ công + fallback tên PH40xx)

    Returns:
        Profile ID (string).

    Raises:
        ProfileNotFoundError nếu không tìm thấy.
    """
    logger.info(f"Đang tìm profile: '{profile_name}'")
    if not is_gemlogin_api_ready(timeout=2.0):
        if not ensure_gemlogin_running():
            raise ProfileNotFoundError("GemLogin chưa sẵn sàng và không thể tự động khởi động.")
    candidates = _get_candidate_profile_names(profile_name)

    try:
        # Cách 1: Thử search parameter
        for cand in candidates:
            try:
                res = _api_get(f"/api/profiles?search={cand}&per_page=1000")
                if res.get("success"):
                    profiles = res.get("data", [])
                    logger.debug(f"Search API ('{cand}') trả về {len(profiles)} profiles")
                    for profile in profiles:
                        p_id = str(profile.get("id", ""))
                        p_path = profile.get("profile_path", "")
                        if p_id and p_path:
                            _profile_paths[p_id] = p_path
                        name = profile.get("name", "").strip()
                        if name == cand:
                            profile_id = p_id
                            if cand != profile_name.strip():
                                logger.info(
                                    f"Tìm thấy: '{profile_name}' (khớp tên Gemlogin '{cand}') -> ID: {profile_id}"
                                )
                            else:
                                logger.info(f"Tìm thấy: '{profile_name}' -> ID: {profile_id}")
                            return profile_id
            except Exception:
                pass

        # Cách 2: Lấy toàn bộ danh sách (per_page=1000 để không bị giới hạn 50 profiles mặc định)
        res = _api_get("/api/profiles?per_page=1000")

        if not res.get("success"):
            raise ProfileNotFoundError(f"API trả lỗi: {res.get('message', 'Unknown')}")

        profiles = res.get("data", [])
        logger.info(f"API trả về tổng cộng {len(profiles)} profiles")

        for profile in profiles:
            p_id = str(profile.get("id", ""))
            p_path = profile.get("profile_path", "")
            if p_id and p_path:
                _profile_paths[p_id] = p_path

        # Log vài tên đầu tiên để debug
        if profiles:
            sample_names = [p.get("name", "?") for p in profiles[:5]]
            logger.debug(f"Mẫu tên: {sample_names}")

        for cand in candidates:
            for profile in profiles:
                name = profile.get("name", "").strip()
                if name == cand:
                    profile_id = str(profile.get("id", ""))
                    if cand != profile_name.strip():
                        logger.info(
                            f"Tìm thấy: '{profile_name}' (khớp tên Gemlogin '{cand}') -> ID: {profile_id}"
                        )
                    else:
                        logger.info(f"Tìm thấy: '{profile_name}' -> ID: {profile_id}")
                    return profile_id

        # Không tìm thấy — log tất cả tên để debug
        all_names = [p.get("name", "?") for p in profiles]
        logger.error(f"Không tìm thấy '{profile_name}' (ứng viên: {candidates}) trong {len(profiles)} profiles")
        logger.debug(f"Danh sách tên: {all_names}")
        raise ProfileNotFoundError(f"Không tìm thấy profile: '{profile_name}'")

    except requests.RequestException as e:
        raise ProfileNotFoundError(f"Lỗi API khi tìm '{profile_name}': {e}")


def _disable_save_password_popup(profile_id: str) -> None:
    """
    Tự động tắt hộp thoại 'Save password?' (Offer to save passwords), 'Restore pages?'
    và cảnh báo lộ mật khẩu trong file Default/Preferences của profile Gemlogin trước khi mở.
    """
    profile_path = _profile_paths.get(str(profile_id), "")
    if not profile_path:
        default_dir = rf"F:\gemlogin\profiles\{profile_id}"
        if os.path.isdir(default_dir):
            profile_path = default_dir

    if not profile_path:
        return

    pref_dir = os.path.join(os.path.normpath(profile_path), "Default")
    pref_file = os.path.join(pref_dir, "Preferences")
    try:
        os.makedirs(pref_dir, exist_ok=True)
        prefs = {}
        if os.path.isfile(pref_file):
            with open(pref_file, "r", encoding="utf-8") as f:
                prefs = json.load(f)

        prefs["credentials_enable_service"] = False
        prefs["credentials_enable_autosignin"] = False

        prof_cfg = prefs.setdefault("profile", {})
        if isinstance(prof_cfg, dict):
            prof_cfg["password_manager_enabled"] = False
            prof_cfg["password_manager_leak_detection"] = False
            prof_cfg["exit_type"] = "Normal"
            prof_cfg["exited_cleanly"] = True

        autofill_cfg = prefs.setdefault("autofill", {})
        if isinstance(autofill_cfg, dict):
            autofill_cfg["profile_enabled"] = False
            autofill_cfg["credit_card_enabled"] = False

        with open(pref_file, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"Không thể cập nhật Preferences cho profile {profile_id}: {e}")


_gemcaptcha_cleaned = False


def _cleanup_gemcaptcha_extension() -> None:
    """
    Gỡ bỏ extension GemCaptcha (nếu từng được đăng ký trước đó) khỏi
    ~/.gemlogin/extensions/extension.json để tránh xung đột/chặn anti-bot.
    """
    global _gemcaptcha_cleaned
    if _gemcaptcha_cleaned:
        return

    try:
        gem_ext_json = os.path.join(os.path.expanduser("~"), ".gemlogin", "extensions", "extension.json")
        if os.path.isfile(gem_ext_json):
            with open(gem_ext_json, "r", encoding="utf-8") as f:
                ext_list = json.load(f)
            if isinstance(ext_list, list):
                filtered = [
                    item
                    for item in ext_list
                    if not (
                        isinstance(item, dict)
                        and (
                            item.get("id") == "gemcaptcha_auto_solve"
                            or "gemcaptcha" in (item.get("id") or "").lower()
                            or "gemcaptcha" in (item.get("name") or "").lower()
                            or "gemcaptcha" in (item.get("path") or "").lower()
                        )
                    )
                ]
                if len(filtered) != len(ext_list):
                    with open(gem_ext_json, "w", encoding="utf-8") as f:
                        json.dump(filtered, f, ensure_ascii=False)
                    logger.info("Đã gỡ extension GemCaptcha khỏi cấu hình Gemlogin")
        _gemcaptcha_cleaned = True
    except Exception as e:
        logger.debug(f"Lỗi khi dọn extension GemCaptcha: {e}")


@retry(max_retries=2, delay=3.0, exceptions=(ProfileStartError,))
def start_profile(profile_id: str) -> Dict:
    """
    Khởi động profile Gemlogin.

    API: GET /api/profiles/start/{id}
    Response: {"success": true, "data": {"remote_debugging_address": "127.0.0.1:61382", "driver_path": "..."}}

    Nếu profile đã mở sẵn, API trả về ngay cổng debug hiện tại.

    Returns:
        Dict chứa remote_debugging_address và driver_path.
    """
    logger.info(f"Đang khởi động profile ID: {profile_id}")

    try:
        with _start_profile_lock:
            _disable_save_password_popup(profile_id)
            _cleanup_gemcaptcha_extension()
            # Truyền đơn lẻ --disable-extensions vì Gemlogin bỏ qua addination_args nếu có khoảng trắng;
            # popup Save Password đã được tắt trực tiếp qua Default/Preferences trong _disable_save_password_popup().
            extra_args = "--disable-extensions"
            res = _api_get(
                f"/api/profiles/start/{profile_id}?addination_args={urllib.parse.quote(extra_args)}",
                timeout=30,
            )
            time.sleep(1.5)

        if not res.get("success"):
            raise ProfileStartError(f"API trả lỗi: {res.get('message', 'Unknown')}")

        data = res.get("data", {})
        remote_address = data.get("remote_debugging_address", "")
        driver_path = data.get("driver_path", "")

        if not remote_address:
            raise ProfileStartError(f"Không nhận được remote_debugging_address: {res}")

        logger.info(f"Profile started. Debug address: {remote_address}")
        return {
            "remote_debugging_address": remote_address,
            "driver_path": driver_path,
            "profile_id": profile_id,
        }

    except requests.RequestException as e:
        raise ProfileStartError(f"Lỗi khởi động profile {profile_id}: {e}")


def stop_profile(profile_id: str) -> bool:
    """
    Đóng profile Gemlogin.
    API: GET /api/profiles/close/{id}
    """
    try:
        res = _api_get(f"/api/profiles/close/{profile_id}", timeout=10)
        if res.get("success"):
            logger.info(f"Profile {profile_id} đã đóng")
            return True
        else:
            logger.warning(f"API đóng profile trả lỗi: {res.get('message')}")
            return False
    except Exception as e:
        logger.warning(f"Lỗi đóng profile {profile_id}: {e}")
        return False


def _ensure_driver_exists(driver_path: str) -> str:
    """
    Kiểm tra xem file gemlogindriver.exe của Gemlogin có tồn tại không.
    Nếu Gemlogin tải thiếu gemlogindriver.exe (ví dụ bản browser 150),
    tự động lấy đúng phiên bản từ thư mục Chrome-bin và tải chromedriver-win64 về.
    """
    if not driver_path:
        return ""
    norm_path = os.path.normpath(driver_path)
    if os.path.isfile(norm_path):
        return norm_path

    chrome_bin_dir = os.path.dirname(norm_path)
    if not os.path.isdir(chrome_bin_dir):
        return ""

    # Tìm thư mục con có tên dạng phiên bản đầy đủ (VD: 150.0.7871.46)
    full_version = ""
    for item in os.listdir(chrome_bin_dir):
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", item) and os.path.isdir(os.path.join(chrome_bin_dir, item)):
            full_version = item
            break

    if not full_version:
        return ""

    url = f"https://storage.googleapis.com/chrome-for-testing-public/{full_version}/win64/chromedriver-win64.zip"
    logger.info(f"Thiếu {norm_path} -> Đang tự động tải ChromeDriver {full_version}...")
    try:
        data = urllib.request.urlopen(url, timeout=45).read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with open(norm_path, "wb") as f:
                f.write(zf.read("chromedriver-win64/chromedriver.exe"))
        logger.info(f"✅ Đã tự động tải xong: {norm_path}")
        return norm_path
    except Exception as e:
        logger.error(f"Không thể tự động tải ChromeDriver {full_version}: {e}")
        return ""


def connect_selenium(profile_info: Dict) -> webdriver.Chrome:
    """
    Kết nối Selenium tới browser profile đã mở qua debuggerAddress.

    Args:
        profile_info: Dict từ start_profile(), chứa remote_debugging_address và driver_path.

    Returns:
        WebDriver instance đã kết nối tới browser.
    """
    remote_address = profile_info["remote_debugging_address"]
    driver_path = _ensure_driver_exists(profile_info.get("driver_path", ""))

    logger.info(f"Kết nối Selenium tới: {remote_address}")

    try:
        chrome_options = ChromeOptions()
        chrome_options.page_load_strategy = "eager"
        chrome_options.add_experimental_option("debuggerAddress", remote_address)

        # Dùng driver_path từ Gemlogin nếu có
        if driver_path:
            service = ChromeService(executable_path=driver_path)
            if os.name == "nt":
                service.creation_flags = 0x08000000  # CREATE_NO_WINDOW
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            driver = webdriver.Chrome(options=chrome_options)

        try:
            driver.command_executor._client_config.timeout = 90
        except Exception:
            pass
        driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
        driver.implicitly_wait(5)

        # Đóng bớt các tab thừa từ phiên chạy trước (chỉ giữ lại 1 tab đầu tiên)
        try:
            handles = driver.window_handles
            if len(handles) > 1:
                first_handle = handles[0]
                for h in handles[1:]:
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
                driver.switch_to.window(first_handle)
        except Exception as e:
            logger.debug(f"Không thể dọn tab thừa: {e}")

        logger.info("Kết nối Selenium thành công")
        return driver

    except Exception as e:
        raise ProfileStartError(f"Không thể kết nối Selenium: {e}")
