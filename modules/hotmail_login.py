"""
Bước 2: Tự động đăng nhập Hotmail trên Tab 2.
CHỈ chạy khi Bước 1 (Facebook login) thành công.
"""
import re
import time
from typing import Optional

import requests
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

import config
from utils.logger import get_logger
from utils.wait_helpers import (
    wait_for_element, wait_for_page_load, safe_click, safe_send_keys,
)
from utils.error_handler import (
    LoginFailedError,
    BrowserClosedError,
    is_browser_closed_exception,
    check_browser_alive,
)

logger = get_logger()

TEMPMAIL_DOMAIN = "nguyenmaucole.io.vn"
TEMPMAIL_WEB_URL = f"https://mail.{TEMPMAIL_DOMAIN}/"
TEMPMAIL_API_URL = "https://tempmail-backend.thincole.workers.dev/api/emails"

FVIA_DOMAIN = "fviainboxes.com"
FVIA_DOMAINS = (
    "fviainboxes.com",
    "fviadropinbox.com",
    "fviamail.work",
    "dropinboxes.com",
)
FVIA_WEB_URL = f"https://{FVIA_DOMAIN}/"
FVIA_MESSAGES_API = f"https://{FVIA_DOMAIN}/messages"
FVIA_MESSAGE_API = f"https://{FVIA_DOMAIN}/message"


def _is_fvia_domain(text_or_domain: str) -> bool:
    """Kiểm tra xem chuỗi/domain có thuộc hệ thống fviainboxes.com không."""
    t = (text_or_domain or "").lower()
    return any(d in t for d in FVIA_DOMAINS)


def _open_new_tab(driver: WebDriver, url: str) -> bool:
    """Mở tab mới và navigate tới URL."""
    try:
        try:
            driver.switch_to.new_window("tab")
        except Exception:
            driver.execute_script("window.open('about:blank', '_blank');")
            time.sleep(0.5)
            handles = driver.window_handles
            driver.switch_to.window(handles[-1])
        driver.set_page_load_timeout(15)
        try:
            driver.get(url)
        except Exception as nav_err:
            logger.warning(f"Trang load chậm ({nav_err}), dừng tải phụ nguyên và tiếp tục...")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        finally:
            driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
        logger.info(f"Đã mở Tab mới: {url}")
        return True
    except Exception as e:
        logger.error(f"Lỗi mở tab mới: {e}")
        return False


def _click_sign_in_button(driver: WebDriver) -> bool:
    """Tìm và click nút 'Đăng nhập' trên trang Microsoft Support."""
    logger.info("Đang tìm nút 'Đăng nhập' trên trang Support...")

    selectors = [
        (By.XPATH, "//a[contains(normalize-space(), 'Đăng nhập') or contains(normalize-space(), 'Đăng Nhập') or contains(normalize-space(), 'Sign in')]"),
        (By.CSS_SELECTOR, "a[href*='outlook.live.com'], a[href*='login.live.com'], a[href*='fwlink']"),
        (By.XPATH, "//button[contains(normalize-space(), 'Đăng nhập')]"),
    ]

    for by, value in selectors:
        btn = wait_for_element(driver, by, value, timeout=5, condition="presence")
        if btn:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            btn_text = btn.text.strip()
            logger.info(f"Tìm thấy nút: '{btn_text}'")
            if not safe_click(btn, delay=2.0):
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2.0)
            logger.info("Đã click nút 'Đăng nhập'")
            return True

    logger.error("Không tìm thấy nút 'Đăng nhập'")
    return False


def _enter_email(driver: WebDriver, email: str) -> bool:
    """Nhập email Hotmail vào trang đăng nhập Microsoft."""
    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
    time.sleep(2)

    current_url = driver.current_url.lower()
    if any(k in current_url for k in ["outlook.live.com/mail", "account.microsoft.com", "account.live.com"]):
        logger.info("Tài khoản Hotmail đã đăng nhập sẵn trong profile!")
        return True

    email_selectors = [
        (By.CSS_SELECTOR, "input[type='email'], #i0116, #usernameEntry, input[name='loginfmt']"),
    ]

    email_input = None
    for by, value in email_selectors:
        email_input = wait_for_element(
            driver, by, value, timeout=config.ELEMENT_WAIT_TIMEOUT, condition="visible",
        )
        if email_input:
            break

    if not email_input:
        current_url = driver.current_url.lower()
        if any(k in current_url for k in ["outlook.live.com/mail", "account.microsoft.com", "account.live.com"]):
            logger.info("Tài khoản Hotmail đã đăng nhập sẵn trong profile!")
            return True
        logger.error("Không tìm thấy ô email trên Microsoft Login")
        return False

    if not safe_send_keys(email_input, email, delay=1.0):
        return False
    logger.info(f"Đã nhập Hotmail email: {email}")

    # Click Next
    next_selectors = [
        (By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']"),
    ]
    for by, value in next_selectors:
        next_btn = wait_for_element(driver, by, value, timeout=3, condition="clickable")
        if next_btn:
            safe_click(next_btn, delay=3.0)
            logger.info("Đã click 'Next'")
            return True

    email_input.send_keys(Keys.RETURN)
    time.sleep(3)
    return True


def _try_use_your_password(
    driver: WebDriver,
    primary_pass: str = "",
    fallback_pass: str = "",
) -> bool:
    """
    Khi gặp màn hình 'Verify your email' (hoặc màn hình xác thực Microsoft) có nút 'Use your password':
    1. Luôn bấm 'Use your password'.
    2. Bước tiếp theo điền mật khẩu 'Khoa01@@c' (primary_pass) và bấm Sign in.
    3. Nếu báo mật khẩu không khớp và có fallback_pass thì thử fallback_pass.
    """
    if not primary_pass:
        primary_pass = getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c")

    driver.implicitly_wait(0)
    try:
        use_pwd_els = [
            el for el in driver.find_elements(
                By.XPATH,
                "//button[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use your password')"
                " or contains(normalize-space(), 'Dùng mật khẩu') or contains(normalize-space(), 'Sử dụng mật khẩu')]"
                " | //a[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use your password')"
                " or contains(normalize-space(), 'Dùng mật khẩu') or contains(normalize-space(), 'Sử dụng mật khẩu')]"
                " | //*[@id='idA_PWD_SwitchToPassword' or @id='otcLoginLink']"
                " | //*[@role='button' or contains(@class, 'fui-Link')][contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use your password')"
                " or contains(normalize-space(), 'Dùng mật khẩu') or contains(normalize-space(), 'Sử dụng mật khẩu')]",
            )
            if el.is_displayed()
        ]
    except Exception:
        use_pwd_els = []
    finally:
        driver.implicitly_wait(5)

    if not use_pwd_els:
        return False

    target_el = use_pwd_els[-1]
    logger.info(f"Phát hiện nút 'Use your password' -> Click và điền mật khẩu '{primary_pass}'...")
    if not safe_click(target_el, delay=1.5):
        driver.execute_script("arguments[0].click();", target_el)
        time.sleep(1.5)

    pass_input = wait_for_element(
        driver, By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']",
        timeout=8, condition="visible",
    )
    if not pass_input:
        return False

    safe_click(pass_input, delay=0.2)
    pass_input.send_keys(Keys.CONTROL + "a")
    pass_input.send_keys(Keys.BACKSPACE)
    safe_send_keys(pass_input, primary_pass, delay=0.8, clear_first=False)
    logger.info(f"Đã điền mật khẩu '{primary_pass}' sau khi chọn 'Use your password'")

    signin_btn = wait_for_element(
        driver, By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']",
        timeout=3, condition="clickable",
    )
    if signin_btn:
        safe_click(signin_btn, delay=3.0)
        logger.info("Đã click 'Sign in'")
    else:
        pass_input.send_keys(Keys.RETURN)
        time.sleep(3.0)

    # Kiểm tra nếu mật khẩu thứ nhất báo sai -> thử fallback_pass
    try:
        body_lower = driver.find_element(By.TAG_NAME, "body").text.lower()
        if "incorrect" in body_lower or "không chính xác" in body_lower:
            if fallback_pass and fallback_pass != primary_pass:
                logger.warning(f"Mật khẩu '{primary_pass}' không đúng, thử mật khẩu dự phòng '{fallback_pass}'...")
                pass_input2 = wait_for_element(
                    driver, By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']",
                    timeout=5, condition="visible",
                )
                if pass_input2:
                    safe_click(pass_input2, delay=0.2)
                    pass_input2.send_keys(Keys.CONTROL + "a")
                    pass_input2.send_keys(Keys.BACKSPACE)
                    safe_send_keys(pass_input2, fallback_pass, delay=0.8, clear_first=False)
                    signin_btn2 = wait_for_element(
                        driver, By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']",
                        timeout=3, condition="clickable",
                    )
                    if signin_btn2:
                        safe_click(signin_btn2, delay=3.0)
                    else:
                        pass_input2.send_keys(Keys.RETURN)
                        time.sleep(3.0)
                    body_lower = driver.find_element(By.TAG_NAME, "body").text.lower()

            # Nếu cả 2 mật khẩu đều không đúng và có nút "Send a code to ..." -> chuyển sang lấy code qua email khôi phục
            if "incorrect" in body_lower or "không chính xác" in body_lower:
                send_code_link = wait_for_element(
                    driver,
                    By.XPATH,
                    "//a[contains(., 'Send a code') or contains(., 'Gửi mã')]"
                    " | //button[contains(., 'Send a code') or contains(., 'Gửi mã')]"
                    " | //*[@role='button' or contains(@class, 'fui-Link')][contains(., 'Send a code') or contains(., 'Gửi mã')]"
                    " | //span[contains(text(), 'Send a code') or contains(text(), 'Gửi mã')]"
                    " | //*[@id='idA_PWD_SwitchToCredPicker']",
                    timeout=2,
                    condition="clickable",
                )
                if send_code_link:
                    logger.info("Click 'Send a code to ...' để xác minh qua email khôi phục")
                    if not safe_click(send_code_link, delay=2.0):
                        driver.execute_script("arguments[0].click();", send_code_link)
                        time.sleep(2.0)
                return False
    except Exception:
        pass

    return True


def _enter_password(
    driver: WebDriver, password: str, hotmail_email: str = "", recovery_email: str = "",
) -> bool:
    """Nhập password Hotmail (hoặc chọn 'Use your password' / xác minh email khôi phục nếu Microsoft yêu cầu)."""
    pass_input = None
    driver.implicitly_wait(0.5)
    try:
        for _ in range(15):
            time.sleep(1)
            current_url = driver.current_url.lower()
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body_text = ""
            body_lower = body_text.lower()

            # 1. Nếu đã chuyển thẳng sang các màn hình sau đăng nhập (Passkey, Stay signed in, TOU, Outlook...)
            if any(k in current_url for k in [
                "outlook.live.com/mail", "account.microsoft.com",
                "interrupt/credentialaction", "privacynotice", "account.live.com",
                "fido/create", "tou/accrue",
            ]) or "stay signed in?" in body_lower or "setting up your passkey" in body_lower:
                logger.info("Tài khoản đã chuyển thẳng qua bước sau đăng nhập / bảo mật")
                return True

            # 2. Kiểm tra ô nhập password đã hiển thị chưa
            pwd_els = [
                el for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']")
                if el.is_displayed()
            ]
            if pwd_els:
                pass_input = pwd_els[0]
                break

            # 3. Nếu hiện nút 'Use your password' -> ưu tiên mật khẩu gốc trong Cột B trước, fallback sang 'Khoa01@@c'
            if "use your password" in body_lower or "dùng mật khẩu" in body_lower or "sử dụng mật khẩu" in body_lower:
                if _try_use_your_password(
                    driver,
                    primary_pass=password,
                    fallback_pass=getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c"),
                ):
                    return True
                try:
                    body_text = driver.find_element(By.TAG_NAME, "body").text
                    body_lower = body_text.lower()
                except Exception:
                    pass

            # 4. Nếu trang hiện "Almost there" / "Verify your email" / "Verify your identity" hoặc ô nhập code 6 số
            if (
                "almost there" in body_lower
                or "just one more step" in body_lower
                or "send a code to" in body_lower
                or "verify your email" in body_lower
                or "verify your identity" in body_lower
                or TEMPMAIL_DOMAIN in body_lower
                or _is_fvia_domain(body_lower)
            ) and (hotmail_email or recovery_email):
                logger.info("Phát hiện màn hình xác minh ('Almost there' / 'Verify your email') -> Xử lý qua email khôi phục...")
                driver.implicitly_wait(5)
                if _handle_add_recovery_email(driver, hotmail_email, recovery_email, fallback_pass=password):
                    return True
    finally:
        driver.implicitly_wait(5)

    if not pass_input:
        logger.error("Không tìm thấy ô password trên Microsoft Login")
        return False

    if not safe_send_keys(pass_input, password, delay=1.0):
        return False
    logger.info("Đã nhập Hotmail password")

    # Click Sign in
    signin_selectors = [
        (By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']"),
    ]
    for by, value in signin_selectors:
        signin_btn = wait_for_element(driver, by, value, timeout=3, condition="clickable")
        if signin_btn:
            safe_click(signin_btn, delay=3.0)
            logger.info("Đã click 'Sign in'")
            break
    else:
        pass_input.send_keys(Keys.RETURN)
        time.sleep(3)

    # Kiểm tra nếu báo sai mật khẩu (do tài khoản đã đổi sang mật khẩu mới trước đó)
    try:
        body_lower = driver.find_element(By.TAG_NAME, "body").text.lower()
        if ("incorrect" in body_lower or "không chính xác" in body_lower) and password != config.NEW_HOTMAIL_PASSWORD:
            logger.warning("Mật khẩu cũ không đúng (có thể đã đổi trước đó), thử đăng nhập bằng mật khẩu mới...")
            pass_input2 = wait_for_element(
                driver, By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']",
                timeout=5, condition="visible",
            )
            if pass_input2:
                safe_send_keys(pass_input2, config.NEW_HOTMAIL_PASSWORD, delay=1.0)
                signin_btn2 = wait_for_element(
                    driver, By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']",
                    timeout=3, condition="clickable",
                )
                if signin_btn2:
                    safe_click(signin_btn2, delay=3.0)
                else:
                    pass_input2.send_keys(Keys.RETURN)
                    time.sleep(3)
                body_lower = driver.find_element(By.TAG_NAME, "body").text.lower()

        if "incorrect" in body_lower or "không chính xác" in body_lower:
            send_code_link = wait_for_element(
                driver,
                By.XPATH,
                "//a[contains(., 'Send a code') or contains(., 'Gửi mã')]"
                " | //button[contains(., 'Send a code') or contains(., 'Gửi mã')]"
                " | //*[@role='button' or contains(@class, 'fui-Link')][contains(., 'Send a code') or contains(., 'Gửi mã')]"
                " | //span[contains(text(), 'Send a code') or contains(text(), 'Gửi mã')]"
                " | //*[@id='idA_PWD_SwitchToCredPicker']",
                timeout=2,
                condition="clickable",
            )
            if send_code_link and (hotmail_email or recovery_email):
                logger.info("Click 'Send a code to ...' để đăng nhập bằng mã email khôi phục...")
                if not safe_click(send_code_link, delay=2.0):
                    driver.execute_script("arguments[0].click();", send_code_link)
                    time.sleep(2.0)
                return _handle_add_recovery_email(driver, hotmail_email, recovery_email, fallback_pass=password)
    except Exception:
        pass

    return True


def _fetch_code_from_tempmail(
    driver: WebDriver, prefix: str, min_id: int = 0, timeout: int = 60
) -> Optional[str]:
    """
    Lấy mã xác nhận 6 số từ mail.nguyenmaucole.io.vn.
    Kết hợp gọi API trực tiếp và mở trang mail.nguyenmaucole.io.vn để kiểm tra.
    """
    full_email = f"{prefix.lower()}@{TEMPMAIL_DOMAIN}"
    logger.info(f"Đang chờ mã xác nhận gửi về: {full_email} ...")

    start_time = time.time()
    while time.time() - start_time < timeout:
        check_browser_alive(driver)
        try:
            resp = requests.get(
                TEMPMAIL_API_URL,
                params={"address": full_email},
                timeout=10,
            )
            if resp.status_code == 200:
                emails = resp.json()
                if isinstance(emails, list) and emails:
                    # Lấy thư mới nhất (id > min_id)
                    latest = emails[0]
                    mail_id = int(latest.get("id", 0))
                    if mail_id > min_id:
                        body = latest.get("body_html", "") or ""
                        subject = latest.get("subject", "") or ""
                        match = re.search(r"Security code:\s*(\d{6})", body, re.IGNORECASE)
                        if not match:
                            match = re.search(r"\b(\d{6})\b", body + " " + subject)
                        if match:
                            code = match.group(1)
                            logger.info(f"✅ Đã lấy được mã xác nhận từ {TEMPMAIL_DOMAIN}: {code}")
                            return code
        except Exception as e:
            logger.debug(f"Lỗi khi gọi API tempmail: {e}")

        time.sleep(3)

    # Fallback: mở trực tiếp trang web mail.nguyenmaucole.io.vn trên trình duyệt
    logger.info("Thử mở trực tiếp trang mail.nguyenmaucole.io.vn trên trình duyệt...")
    current_handle = driver.current_window_handle
    try:
        driver.execute_script(f"window.open('{TEMPMAIL_WEB_URL}', '_blank');")
        time.sleep(2)
        driver.switch_to.window(driver.window_handles[-1])
        wait_for_page_load(driver, timeout=15)
        time.sleep(1)

        prefix_input = wait_for_element(driver, By.ID, "email-prefix", timeout=5, condition="visible")
        if prefix_input:
            safe_send_keys(prefix_input, prefix, delay=0.5)
            check_btn = wait_for_element(
                driver, By.XPATH, "//button[contains(text(), 'Kiểm tra Thư')]", timeout=5, condition="clickable"
            )
            if check_btn:
                safe_click(check_btn, delay=3.0)
                mail_item = wait_for_element(driver, By.CSS_SELECTOR, ".mail-item", timeout=10, condition="clickable")
                if mail_item:
                    safe_click(mail_item, delay=1.0)
                    view_body = wait_for_element(driver, By.ID, "view-body", timeout=5, condition="visible")
                    if view_body:
                        text = view_body.text
                        match = re.search(r"Security code:\s*(\d{6})", text, re.IGNORECASE) or re.search(r"\b(\d{6})\b", text)
                        if match:
                            code = match.group(1)
                            logger.info(f"✅ Đã lấy được mã trên web mail.nguyenmaucole.io.vn: {code}")
                            driver.close()
                            driver.switch_to.window(current_handle)
                            return code
    except Exception as e:
        logger.warning(f"Fallback mở web mail.nguyenmaucole.io.vn lỗi: {e}")
    finally:
        try:
            if driver.current_window_handle != current_handle and len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(current_handle)
        except Exception:
            pass

    logger.error(f"⏰ Hết thời gian chờ mã xác nhận cho {full_email}")
    return None


def _get_latest_tempmail_id(prefix: str) -> int:
    """Lấy ID email mới nhất hiện tại để tránh đọc nhầm thư cũ."""
    full_email = f"{prefix.lower()}@{TEMPMAIL_DOMAIN}"
    try:
        resp = requests.get(TEMPMAIL_API_URL, params={"address": full_email}, timeout=10)
        if resp.status_code == 200:
            emails = resp.json()
            if isinstance(emails, list) and emails:
                return int(emails[0].get("id", 0))
    except Exception:
        pass
    return 0


def _get_existing_fviainboxes_ids(prefix: str, domain: str = FVIA_DOMAIN) -> set:
    """Kích hoạt hòm thư fviainboxes.com và lấy danh sách ID thư cũ hiện có."""
    seen_ids = set()
    target_domain = (domain or FVIA_DOMAIN).strip().lower()
    try:
        resp = requests.get(
            FVIA_MESSAGES_API,
            params={"username": prefix.lower(), "domain": target_domain},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            for msg in data.get("result") or []:
                if msg.get("id"):
                    seen_ids.add(str(msg["id"]))
    except Exception as e:
        logger.debug(f"Lỗi khi khởi tạo hòm thư fviainboxes.com ({target_domain}): {e}")
    return seen_ids


def _fetch_code_from_fviainboxes(
    driver: WebDriver,
    prefix: str,
    domain: str = FVIA_DOMAIN,
    seen_ids: Optional[set] = None,
    timeout: int = 60,
) -> Optional[str]:
    """
    Lấy mã xác nhận 6 số từ trang fviainboxes.com (hỗ trợ mọi domain: fviainboxes.com, fviadropinbox.com, fviamail.work, dropinboxes.com).
    """
    seen_ids = seen_ids or set()
    username = prefix.strip().lower()
    target_domain = (domain or FVIA_DOMAIN).strip().lower()
    full_email = f"{username}@{target_domain}"
    logger.info(f"Đang chờ mã xác nhận gửi về: {full_email} (trên {FVIA_DOMAIN}) ...")

    # Bước 1: Thử lấy qua API fviainboxes.com (nhanh, không tốn tab nếu không bật Turnstile)
    start_time = time.time()
    while time.time() - start_time < min(timeout, 35):
        check_browser_alive(driver)
        try:
            resp = requests.get(
                FVIA_MESSAGES_API,
                params={"username": username, "domain": target_domain},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if resp.status_code == 403:
                logger.info("fviainboxes.com yêu cầu xác thực trình duyệt -> Mở tab fviainboxes.com...")
                break
            if resp.status_code == 200:
                messages = resp.json().get("result") or []
                for msg in messages:
                    msg_id = str(msg.get("id", ""))
                    if not msg_id or msg_id in seen_ids:
                        continue
                    subject = msg.get("subject", "") or ""
                    body_resp = requests.get(
                        FVIA_MESSAGE_API,
                        params={"username": username, "domain": target_domain, "id": msg_id},
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=10,
                    )
                    body_html = ""
                    if body_resp.status_code == 200:
                        try:
                            raw_json = body_resp.json()
                            if isinstance(raw_json, str):
                                body_html = raw_json
                            elif isinstance(raw_json, dict):
                                body_html = str(raw_json.get("result", "") or "")
                            else:
                                body_html = body_resp.text
                        except Exception:
                            body_html = body_resp.text
                    body_clean = re.sub(r"<[^>]+>", " ", body_html)
                    match = re.search(
                        r"(?:Security code|single-use code is|code is)[^0-9]*(\d{6,7})\b",
                        body_clean,
                        re.IGNORECASE,
                    )
                    if not match:
                        match = re.search(r"\b(\d{6})\b", body_clean + " " + subject)
                    if match:
                        code = match.group(1)
                        logger.info(f"✅ Đã lấy được mã xác nhận từ {target_domain}: {code}")
                        return code
        except Exception as e:
            logger.debug(f"Lỗi khi gọi API fviainboxes.com: {e}")

        time.sleep(3)

    # Bước 2: Mở trực tiếp trang fviainboxes.com trên trình duyệt để lấy code
    logger.info(f"Mở trang {FVIA_WEB_URL} để lấy mã cho {full_email}...")
    current_handle = driver.current_window_handle
    try:
        driver.execute_script(f"window.open('{FVIA_WEB_URL}', '_blank');")
        time.sleep(2)
        driver.switch_to.window(driver.window_handles[-1])
        wait_for_page_load(driver, timeout=15)
        time.sleep(2)

        user_inp = wait_for_element(
            driver, By.CSS_SELECTOR, "input#username, input[name='username'], input[placeholder*='username']",
            timeout=10, condition="visible",
        )
        if user_inp:
            safe_send_keys(user_inp, username, delay=0.5)

            # Chọn đúng domain trong dropdown bên cạnh ô username (vd: fviadropinbox.com, fviamail.work)
            try:
                domain_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "button[id^='headlessui-listbox-button'], button[aria-haspopup='listbox']",
                    timeout=3,
                    condition="clickable",
                )
                if domain_btn and target_domain not in (domain_btn.text or "").lower():
                    if not safe_click(domain_btn, delay=0.8):
                        driver.execute_script("arguments[0].click();", domain_btn)
                        time.sleep(0.8)
                    opt = wait_for_element(
                        driver,
                        By.XPATH,
                        f"//li[contains(normalize-space(), '{target_domain}')] | //*[@role='option'][contains(normalize-space(), '{target_domain}')]",
                        timeout=3,
                        condition="clickable",
                    )
                    if opt:
                        if not safe_click(opt, delay=0.8):
                            driver.execute_script("arguments[0].click();", opt)
                            time.sleep(0.8)
                        logger.info(f"Đã chọn domain '{target_domain}' trên fviainboxes.com")
            except Exception as dom_err:
                logger.debug(f"Không thể chọn domain dropdown trên fviainboxes.com: {dom_err}")

            get_btn = wait_for_element(
                driver,
                By.XPATH,
                "//button[contains(., 'Get Email')]",
                timeout=5,
                condition="clickable",
            )
            if get_btn:
                safe_click(get_btn, delay=3.0)

            # Chờ thư mới xuất hiện trong danh sách (tối đa 30s)
            for _ in range(8):
                mail_item = wait_for_element(
                    driver, By.CSS_SELECTOR, "ul li.group, main ul li", timeout=4, condition="clickable"
                )
                if mail_item:
                    safe_click(mail_item, delay=2.0)
                    iframe_el = wait_for_element(
                        driver, By.CSS_SELECTOR, "iframe[title='Email content'], article iframe, iframe",
                        timeout=5, condition="presence",
                    )
                    if iframe_el:
                        srcdoc = iframe_el.get_attribute("srcdoc") or ""
                        if not srcdoc:
                            try:
                                driver.switch_to.frame(iframe_el)
                                srcdoc = driver.find_element(By.TAG_NAME, "body").text
                                driver.switch_to.default_content()
                            except Exception:
                                driver.switch_to.default_content()
                        src_clean = re.sub(r"<[^>]+>", " ", srcdoc)
                        match = (
                            re.search(
                                r"(?:Security code|single-use code is|code is)[^0-9]*(\d{6,7})\b",
                                src_clean,
                                re.IGNORECASE,
                            )
                            or re.search(r"\b(\d{6})\b", src_clean)
                        )
                        if match:
                            code = match.group(1)
                            logger.info(f"✅ Đã lấy được mã trên web {FVIA_DOMAIN} ({target_domain}): {code}")
                            return code
                # Nếu chưa có thư, bấm lại Get Email / Refresh
                refresh_btn = wait_for_element(
                    driver, By.XPATH, "//button[contains(., 'Get Email')]", timeout=2, condition="clickable"
                )
                if refresh_btn:
                    safe_click(refresh_btn, delay=3.0)
                else:
                    time.sleep(3)
    except Exception as e:
        logger.warning(f"Lỗi khi lấy code trên web {FVIA_DOMAIN}: {e}")
    finally:
        try:
            if driver.current_window_handle != current_handle and len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(current_handle)
        except Exception:
            pass

    logger.error(f"⏰ Hết thời gian chờ mã xác nhận cho {full_email}")
    return None


def _resolve_recovery_email(hotmail_email: str, recovery_email: str, body_text: str) -> str:
    """
    Xác định email khôi phục cần điền:
    - Nếu trang Microsoft chỉ định rõ domain (@fviadropinbox.com, @fviamail.work, @fviainboxes.com, @nguyenmaucole.io.vn...) trên màn hình Verify, ưu tiên domain đó.
    - Nếu trong data Cột B có sẵn recovery_email, dùng recovery_email đó.
    - Ngược lại mặc định dùng <prefix>@nguyenmaucole.io.vn.
    """
    prefix = hotmail_email.split("@")[0].strip() if hotmail_email else ""
    body_lower = (body_text or "").lower()

    for fvia_dom in FVIA_DOMAINS:
        if fvia_dom in body_lower:
            if recovery_email and fvia_dom in recovery_email.lower():
                return recovery_email.strip()
            return f"{prefix}@{fvia_dom}"

    if TEMPMAIL_DOMAIN in body_lower:
        if recovery_email and TEMPMAIL_DOMAIN in recovery_email.lower():
            return recovery_email.strip()
        return f"{prefix}@{TEMPMAIL_DOMAIN}"

    if recovery_email and "@" in recovery_email:
        return recovery_email.strip()

    return f"{prefix}@{TEMPMAIL_DOMAIN}"


def _handle_add_recovery_email(
    driver: WebDriver, hotmail_email: str, recovery_email: str = "",
    fallback_pass: str = "",
) -> bool:
    """
    Xử lý màn hình 'Help protect your account', 'Help us protect your account' (identity/confirm), hoặc 'Verify your email':
    0. Nếu trên màn hình có nút 'Use your password', bấm 'Use your password'.
    1. Click 'Add email' hoặc chọn radio 'Email ...' (trên trang identity/confirm)
    2. Điền email khôi phục (hoặc prefix nếu ô có sẵn @domain) -> Click 'Next' / 'Send code'
    3. Lấy code 6 số từ fviainboxes.com hoặc mail.nguyenmaucole.io.vn và điền vào ô code
    """
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body_text = ""

    # Ưu tiên số 1: Nếu màn hình hiện nút "Use your password" -> bấm "Use your password"
    body_lower_init = body_text.lower()
    if "use your password" in body_lower_init or "dùng mật khẩu" in body_lower_init or "sử dụng mật khẩu" in body_lower_init:
        if _try_use_your_password(
            driver,
            primary_pass=fallback_pass or getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c"),
            fallback_pass=getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c") if fallback_pass else "",
        ):
            return True
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            body_lower_init = body_text.lower()
        except Exception:
            pass

    target_recovery_email = _resolve_recovery_email(hotmail_email, recovery_email, body_text)
    rec_prefix, rec_domain = target_recovery_email.split("@", 1)
    rec_prefix = rec_prefix.strip()
    rec_domain = rec_domain.strip().lower()

    # Trường hợp 0: Nếu màn hình ĐÃ ở sẵn bước nhập 6 ô code hoặc ô #iOttText
    existing_code_boxes = [
        el for el in driver.find_elements(By.CSS_SELECTOR, "#codeEntry-0, #iOttText, input[name='otc']")
        if el.is_displayed()
    ]
    if existing_code_boxes:
        logger.info(f"Microsoft đã tự gửi mã xác nhận về {target_recovery_email}, tiến hành lấy code...")
        code_el = existing_code_boxes[0]
        is_six_boxes = (code_el.get_attribute("id") or "") == "codeEntry-0"
        if _is_fvia_domain(rec_domain):
            code = _fetch_code_from_fviainboxes(driver, rec_prefix, domain=rec_domain, seen_ids=set(), timeout=60)
        else:
            code = _fetch_code_from_tempmail(driver, rec_prefix, min_id=0, timeout=60)
        if not code:
            return False
        if is_six_boxes:
            url_before_code = driver.current_url
            logger.info(f"Đang điền mã {code} vào 6 ô codeEntry...")
            for i, ch in enumerate(code[:6]):
                box = driver.find_element(By.ID, f"codeEntry-{i}")
                box.send_keys(ch)
                time.sleep(0.15)
            for _ in range(12):
                time.sleep(1)
                if driver.current_url != url_before_code:
                    break
                try:
                    if not driver.find_element(By.ID, "codeEntry-0").is_displayed():
                        break
                except Exception:
                    break
            time.sleep(2)
        else:
            logger.info(f"Đang điền mã {code} vào ô xác nhận...")
            safe_send_keys(code_el, code, delay=1.0)
            btn = wait_for_element(
                driver, By.CSS_SELECTOR, "#iVerifyCodeAction, button[type='submit'], input[type='submit'], #idSIButton9",
                timeout=3, condition="clickable",
            )
            if btn:
                safe_click(btn, delay=3.0)
        logger.info("✅ Đã xác nhận email khôi phục thành công!")
        return True

    # Bước 1: Nếu đang ở màn hình có nút "Add email", bấm "Add email"
    if "help protect your account" in body_lower_init or "add an email" in body_lower_init:
        logger.info("Phát hiện màn hình 'Help protect your account' -> Bấm 'Add email'")
        add_btn_selectors = [
            (By.XPATH, "//button[contains(., 'Add email')]"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.ID, "iLandingViewAction"),
        ]
        for by, val in add_btn_selectors:
            btn = wait_for_element(driver, by, val, timeout=3, condition="clickable")
            if btn:
                safe_click(btn, delay=3.0)
                logger.info("Đã click nút 'Add email'")
                break

    # Bước 1a: Nếu đang ở màn hình "Help us protect your account" (account.live.com/identity/confirm) có radio button #iProof0
    proof_radios = [
        el for el in driver.find_elements(By.CSS_SELECTOR, "#iProof0, input[type='radio'][value*='Email']")
        if el.is_displayed()
    ]
    if proof_radios:
        logger.info("Phát hiện radio 'Email ...' trên trang Help us protect your account -> Chọn radio Email")
        radio_el = proof_radios[0]
        if not safe_click(radio_el, delay=1.5):
            driver.execute_script("arguments[0].click();", radio_el)
            time.sleep(1.5)
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            target_recovery_email = _resolve_recovery_email(hotmail_email, recovery_email, body_text)
            rec_prefix, rec_domain = target_recovery_email.split("@", 1)
            rec_prefix = rec_prefix.strip()
            rec_domain = rec_domain.strip().lower()
        except Exception:
            pass

    # Bước 1b: Nếu đang ở màn hình "Almost there" ("Send a code to ...") hoặc "Verify your identity" chọn phương thức nhận code qua email
    body_lower_curr = body_text.lower()
    if (
        "almost there" in body_lower_curr
        or "just one more step" in body_lower_curr
        or "send a code to" in body_lower_curr
        or ("verify your identity" in body_lower_curr and "verify your email" not in body_lower_curr)
    ):
        email_option = wait_for_element(
            driver,
            By.XPATH,
            "//button[contains(., 'Send a code') or contains(., 'Gửi mã')]"
            " | //a[contains(., 'Send a code') or contains(., 'Gửi mã')]"
            " | //*[@role='button' or contains(@class, 'fui-Link')][contains(., 'Send a code') or contains(., 'Gửi mã')]"
            " | //span[contains(text(), 'Send a code') or contains(text(), 'Gửi mã')]"
            f" | //div[@role='button' or contains(@class, 'tile')][contains(., '{TEMPMAIL_DOMAIN}') or contains(., 'fvia') or contains(., 'Email ')]",
            timeout=3,
            condition="clickable",
        )
        if email_option:
            if not safe_click(email_option, delay=2.0):
                driver.execute_script("arguments[0].click();", email_option)
                time.sleep(2.0)
            logger.info("Đã chọn phương thức 'Send a code to ...' / xác minh qua Email khôi phục")
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                target_recovery_email = _resolve_recovery_email(hotmail_email, recovery_email, body_text)
                rec_prefix, rec_domain = target_recovery_email.split("@", 1)
                rec_prefix = rec_prefix.strip()
                rec_domain = rec_domain.strip().lower()
            except Exception:
                pass

    # Bước 2: Chờ màn hình nhập địa chỉ email khôi phục (hoặc nếu đã chuyển thẳng sang ô nhập 6 số code)
    time.sleep(1)
    min_mail_id = 0
    fvia_seen_ids = set()
    if _is_fvia_domain(rec_domain):
        fvia_seen_ids = _get_existing_fviainboxes_ids(rec_prefix, domain=rec_domain)
    else:
        min_mail_id = _get_latest_tempmail_id(rec_prefix)

    email_inp = None
    inp_selectors = [
        (By.CSS_SELECTOR, "#iProofEmail, #proof-confirmation-email-input, input[id^='floatingLabelInput']:not([id^='codeEntry']), input[type='email'], #EmailAddress"),
    ]
    for by, val in inp_selectors:
        email_inp = wait_for_element(driver, by, val, timeout=4, condition="visible")
        if email_inp:
            break

    if email_inp:
        # Nếu là ô #iProofEmail (trên trang identity/confirm có sẵn hậu tố @domain bên cạnh) -> chỉ điền phần prefix
        inp_id = (email_inp.get_attribute("id") or "").strip()
        domain_addon_els = [
            el for el in driver.find_elements(By.ID, "iConfirmProofEmailDomain")
            if el.is_displayed() and "@" in (el.text or "")
        ]
        if inp_id == "iProofEmail" or domain_addon_els:
            value_to_type = rec_prefix
            logger.info(f"Nhập phần ẩn email khôi phục (prefix): {value_to_type} (domain: @{rec_domain})")
        else:
            value_to_type = target_recovery_email
            logger.info(f"Nhập email khôi phục: {value_to_type}")

        safe_send_keys(email_inp, value_to_type, delay=1.0)

        next_btn_selectors = [
            (By.CSS_SELECTOR, "#iSelectProofAction"),
            (By.XPATH, "//button[contains(., 'Next') or contains(., 'Send code') or contains(., 'Gửi mã')]"),
            (By.CSS_SELECTOR, "button[type='submit'], input[type='submit'], #iNext"),
        ]
        for by, val in next_btn_selectors:
            next_btn = wait_for_element(driver, by, val, timeout=5, condition="clickable")
            if next_btn:
                safe_click(next_btn, delay=3.0)
                logger.info("Đã click 'Next'/'Send code' để gửi mã xác nhận về email khôi phục")
                break
    else:
        # Kiểm tra xem có phải đã chuyển thẳng sang màn hình nhập code 6 số không
        direct_code_boxes = [
            el for el in driver.find_elements(By.CSS_SELECTOR, "#codeEntry-0, #iOttText, input[name='otc']")
            if el.is_displayed()
        ]
        if not direct_code_boxes:
            logger.warning("Không tìm thấy ô nhập email khôi phục")
            return False
        fvia_seen_ids = set()
        min_mail_id = 0

    # Bước 3: Chờ ô nhập code 6 số xuất hiện (6 ô codeEntry-0..5 hoặc 1 ô đơn #iOttText)
    code_el = wait_for_element(
        driver,
        By.CSS_SELECTOR,
        "#codeEntry-0, #iOttText, input[name='otc'], input[type='tel'], input[type='number'], input[id^='floatingLabelInput'], input[type='text']",
        timeout=10,
        condition="visible",
    )
    if not code_el:
        logger.error("Không tìm thấy ô nhập mã xác nhận trên trang Microsoft")
        return False

    is_six_boxes = (code_el.get_attribute("id") or "") == "codeEntry-0"

    # Lấy code từ fviainboxes.com hoặc mail.nguyenmaucole.io.vn
    if _is_fvia_domain(rec_domain):
        code = _fetch_code_from_fviainboxes(driver, rec_prefix, domain=rec_domain, seen_ids=fvia_seen_ids, timeout=60)
    else:
        code = _fetch_code_from_tempmail(driver, rec_prefix, min_id=min_mail_id, timeout=60)

    if not code:
        return False

    # Điền code vào 6 ô (codeEntry-0 .. codeEntry-5) hoặc 1 ô (#iOttText)
    if is_six_boxes:
        url_before_code = driver.current_url
        logger.info(f"Đang điền mã {code} vào 6 ô codeEntry...")
        for i, ch in enumerate(code[:6]):
            box = driver.find_element(By.ID, f"codeEntry-{i}")
            box.send_keys(ch)
            time.sleep(0.15)
        # Chờ trang tự chuyển sang bước tiếp theo (hoặc popup tự đóng) sau khi điền đủ 6 số (tối đa 12s)
        for _ in range(12):
            time.sleep(1)
            try:
                if driver.current_url != url_before_code:
                    break
                if not driver.find_element(By.ID, "codeEntry-0").is_displayed():
                    break
            except Exception:
                break
        time.sleep(1.5)
    else:
        logger.info(f"Đang điền mã {code} vào ô xác nhận...")
        safe_send_keys(code_el, code, delay=1.0)
        verify_btn_selectors = [
            (By.CSS_SELECTOR, "#iVerifyCodeAction"),
            (By.XPATH, "//button[contains(., 'Next') or contains(., 'Verify') or contains(., 'Xác minh')]"),
            (By.CSS_SELECTOR, "button[type='submit'], input[type='submit'], #iNext, #idSIButton9"),
        ]
        for by, val in verify_btn_selectors:
            btn = wait_for_element(driver, by, val, timeout=3, condition="clickable")
            if btn:
                safe_click(btn, delay=3.0)
                break

    logger.info("✅ Đã xác nhận email khôi phục thành công!")
    return True


def handle_microsoft_prompts(
    driver: WebDriver, hotmail_email: str, recovery_email: str = "",
    current_password: str = "",
) -> None:
    """
    Xử lý chuỗi các màn hình trung gian của Microsoft sau khi đăng nhập:
    1. Help protect your account / Help us protect your account (identity/confirm)
    1b. Verify your email / Verify your identity (điền email khôi phục -> lấy code)
    1c. Let's protect your account (proofs/Add -> bấm Skip for now #iShowSkip)
    2. Setting up your passkey... (fido/create -> bấm Cancel)
    3. A quick note about your Microsoft account (privacynotice -> bấm OK)
    4. Stay signed in? (bấm Yes)
    """
    default_new_pass = getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c")
    primary_pwd = current_password or default_new_pass
    fallback_pwd = default_new_pass if current_password else ""

    for _ in range(7):
        time.sleep(2)
        try:
            current_url = driver.current_url.lower()
            # Nếu đã vào hòm thư Outlook hoặc trang quản lý chung thì dừng kiểm tra prompt
            if "outlook.live.com" in current_url or "office.com" in current_url:
                break

            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body_text = ""
            body_lower = body_text.lower()

            # 0. Nếu hiện màn hình có nút 'Use your password' -> ưu tiên mật khẩu hiện tại, fallback sang 'Khoa01@@c'
            if "use your password" in body_lower or "dùng mật khẩu" in body_lower or "sử dụng mật khẩu" in body_lower:
                if _try_use_your_password(
                    driver,
                    primary_pass=primary_pwd,
                    fallback_pass=fallback_pwd,
                ):
                    continue
                try:
                    body_text = driver.find_element(By.TAG_NAME, "body").text
                    body_lower = body_text.lower()
                except Exception:
                    pass

            # 1c. Nếu ở trang "Let's protect your account" (proofs/Add) có nút "Skip for now" (#iShowSkip) -> bấm Skip
            if "proofs/add" in current_url or "let's protect your account" in body_lower:
                skip_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "#iShowSkip, a#iShowSkip, button#iShowSkip",
                    timeout=3,
                    condition="clickable",
                )
                if not skip_btn:
                    skip_btn = wait_for_element(
                        driver,
                        By.XPATH,
                        "//a[contains(., 'Skip for now') or contains(., 'Bỏ qua')] | //button[contains(., 'Skip for now') or contains(., 'Bỏ qua')]",
                        timeout=2,
                        condition="clickable",
                    )
                if skip_btn:
                    logger.info("Phát hiện màn hình 'Let's protect your account' -> Bấm 'Skip for now'")
                    if not safe_click(skip_btn, delay=2.5):
                        driver.execute_script("arguments[0].click();", skip_btn)
                        time.sleep(2.5)
                    continue

            # 1. Màn hình Help protect your account / Help us protect your account (identity/confirm) / Add email
            if (
                "interrupt/credentialaction" in current_url
                or "identity/confirm" in current_url
                or "help protect your account" in body_lower
                or "help us protect your account" in body_lower
                or "add an email address" in body_lower
            ):
                if not _handle_add_recovery_email(driver, hotmail_email, recovery_email, fallback_pass=primary_pwd):
                    break
                continue

            # 1b. Màn hình Almost there / Verify your identity / Verify your email (khi tài khoản đã gắn email khôi phục trước đó)
            if (
                "almost there" in body_lower
                or "just one more step" in body_lower
                or "send a code to" in body_lower
                or "verify your identity" in body_lower
                or "verify your email" in body_lower
                or "we need to verify your identity" in body_lower
            ):
                logger.info("Phát hiện màn hình xác minh danh tính ('Almost there' / 'Verify your email')...")
                if not _handle_add_recovery_email(driver, hotmail_email, recovery_email, fallback_pass=primary_pwd):
                    break
                continue

            # 2. Màn hình điều khoản dịch vụ mới (tou/accrue -> bấm Next)
            if "tou/accrue" in current_url:
                logger.info("Phát hiện màn hình cập nhật Điều khoản (tou/accrue) -> Bấm 'Next'")
                next_tou_btn = wait_for_element(
                    driver, By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']",
                    timeout=3, condition="clickable",
                )
                if next_tou_btn:
                    safe_click(next_tou_btn, delay=3.0)
                    continue

            # 2b. Màn hình Setting up your passkey... (fido/create) -> bấm Cancel (#idBtn_Back / input[value='Cancel'])
            if "fido/create" in current_url or "passkey" in body_lower:
                logger.info("Phát hiện màn hình 'Setting up your passkey...' -> Bấm 'Cancel'")
                cancel_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "#idBtn_Back, input[value='Cancel'], input[value='Hủy'], button[id*='Cancel'], button[id*='Back']",
                    timeout=3,
                    condition="clickable",
                )
                if not cancel_btn:
                    cancel_btn = wait_for_element(
                        driver,
                        By.XPATH,
                        "//button[normalize-space()='Cancel' or normalize-space()='Hủy' or contains(., 'Cancel')]",
                        timeout=2,
                        condition="clickable",
                    )
                if cancel_btn:
                    if not safe_click(cancel_btn, delay=3.0):
                        driver.execute_script("arguments[0].click();", cancel_btn)
                        time.sleep(3.0)
                    logger.info("Đã click 'Cancel' bỏ qua cài đặt Passkey")
                    continue

            # 3. Màn hình Privacy Notice ("A quick note about your Microsoft account" -> nút OK)
            if "privacynotice" in current_url or "a quick note about your microsoft account" in body_lower:
                ok_btn = wait_for_element(
                    driver, By.XPATH, "//button[normalize-space()='OK']", timeout=3, condition="clickable"
                )
                if ok_btn:
                    safe_click(ok_btn, delay=3.0)
                    logger.info("Đã click 'OK' trên trang Privacy Notice")
                    continue

            # 4. Màn hình "Stay signed in?" -> nút Yes
            if "stay signed in?" in body_lower or "duy trì đăng nhập?" in body_lower:
                yes_selectors = [
                    (By.XPATH, "//button[normalize-space()='Yes']"),
                    (By.XPATH, "//button[normalize-space()='Có']"),
                    (By.ID, "idSIButton9"),
                    (By.CSS_SELECTOR, "button[type='submit']"),
                    (By.CSS_SELECTOR, "input[type='submit']"),
                ]
                clicked_yes = False
                for by, val in yes_selectors:
                    yes_btn = wait_for_element(driver, by, val, timeout=3, condition="clickable")
                    if yes_btn:
                        safe_click(yes_btn, delay=3.0)
                        logger.info("Đã xử lý popup 'Stay signed in?' (Yes)")
                        clicked_yes = True
                        break
                if clicked_yes:
                    continue

            break
        except Exception as e:
            logger.debug(f"Lỗi khi kiểm tra prompt Microsoft: {e}")
            break


def _dismiss_outlook_popups(driver: WebDriver) -> None:
    """
    Tự động đóng các popup quảng cáo / giới thiệu / quyền riêng tư xuất hiện lần đầu
    khi truy cập Outlook (tránh bấm nhầm nút 'Close settings').
    """
    driver.implicitly_wait(0)
    try:
        for _ in range(3):
            dismissed = False

            # 1. Ưu tiên bấm nút 'Accept' / 'Accept all' / 'Chấp nhận' trên hộp thoại Privacy/Cookie của Outlook
            accept_xp = (
                "//button[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận' "
                "or .//span[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận']] | "
                "//*[@role='button'][normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận']"
            )
            try:
                accept_btns = [b for b in driver.find_elements(By.XPATH, accept_xp) if b.is_displayed()]
                if accept_btns:
                    btn = accept_btns[-1]
                    if not safe_click(btn, delay=1.5):
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.5)
                    logger.info("✅ Đã bấm 'Accept' trên thông báo quyền riêng tư Outlook")
                    dismissed = True
                    continue
            except Exception:
                pass

            # 2. Kiểm tra iframe privacynotice / consent nếu có
            try:
                iframes = [
                    f for f in driver.find_elements(
                        By.CSS_SELECTOR, "iframe[src*='privacynotice'], iframe[src*='consent']"
                    )
                    if f.is_displayed()
                ]
                for frame in iframes:
                    try:
                        driver.switch_to.frame(frame)
                        btns = driver.find_elements(
                            By.XPATH,
                            "//button[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='OK' "
                            "or normalize-space()='Next' or normalize-space()='Continue' or normalize-space()='Chấp nhận']",
                        )
                        for b in btns:
                            if b.is_displayed():
                                if not safe_click(b, delay=1.5):
                                    driver.execute_script("arguments[0].click();", b)
                                    time.sleep(1.5)
                                dismissed = True
                                logger.info("✅ Đã bấm 'Accept/OK' đóng popup Privacy/Consent trong iframe Outlook")
                                break
                    finally:
                        driver.switch_to.default_content()
                    if dismissed:
                        break
            except Exception:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

            if dismissed:
                continue

            # 3. Kiểm tra các nút đóng popup (Close / Dismiss / Got it / Not now / Skip / OK) ngoài Settings
            popup_btn_selectors = [
                (
                    By.CSS_SELECTOR,
                    "button[aria-label='Close']:not([aria-label='Close settings']), "
                    "button[aria-label='Dismiss'], "
                    "button[aria-label='Đóng']:not([aria-label*='cài đặt']), "
                    "button[title='Close']:not([aria-label='Close settings'])",
                ),
                (
                    By.XPATH,
                    "//button[normalize-space()='Got it' or normalize-space()='OK' or normalize-space()='Not now' "
                    "or normalize-space()='Maybe later' or normalize-space()='No thanks' or normalize-space()='Skip' "
                    "or normalize-space()='Accept' or normalize-space()='Continue' or normalize-space()='Đã hiểu' "
                    "or normalize-space()='Để sau' or normalize-space()='Bỏ qua']",
                ),
            ]
            for by, val in popup_btn_selectors:
                try:
                    btns = [b for b in driver.find_elements(by, val) if b.is_displayed()]
                    if btns:
                        btn = btns[-1]
                        btn_label = (btn.text or btn.get_attribute("aria-label") or "popup").strip()
                        if not safe_click(btn, delay=1.5):
                            driver.execute_script("arguments[0].click();", btn)
                            time.sleep(1.5)
                        logger.info(f"Đã đóng popup lần đầu trên Outlook: '{btn_label}'")
                        dismissed = True
                        break
                except Exception:
                    pass

            if not dismissed:
                break
    finally:
        driver.implicitly_wait(5)


def _handle_forwarding_signin(
    driver: WebDriver,
    hotmail_email: str,
    new_hotmail_pass: str = "",
    old_hotmail_pass: str = "",
    recovery_email: str = "",
) -> bool:
    """
    Xử lý trường hợp vào trang cài đặt Forwarding (outlook.live.com/mail/options/mail/forwarding)
    mà Outlook yêu cầu bấm nút 'Sign in' để xác thực lại tài khoản:
      1. Bấm nút 'Sign in' trong khung Forwarding and IMAP.
      2. Chuyển sang cửa sổ popup Microsoft OAuth (login.live.com/oauth20_authorize.srf...).
      3. Nếu hiện màn hình 'Verify your email' có nút 'Use your password', bấm 'Use your password'.
      4. Điền mật khẩu 'Khoa01@@c' (new_hotmail_pass, hoặc fallback old_hotmail_pass) và bấm Sign in.
      5. Chờ popup tự đóng và quay lại tab cài đặt Forwarding.
    """
    if not new_hotmail_pass:
        new_hotmail_pass = getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c")

    driver.implicitly_wait(0)
    try:
        signin_btns = [
            el for el in driver.find_elements(
                By.XPATH,
                "//div[@role='dialog']//button[contains(normalize-space(), 'Sign in') or contains(normalize-space(), 'Đăng nhập')]"
                " | //button[contains(normalize-space(), 'Sign in') or contains(normalize-space(), 'Đăng nhập')]",
            )
            if el.is_displayed()
        ]
    except Exception:
        signin_btns = []
    finally:
        driver.implicitly_wait(5)

    if not signin_btns:
        return False

    logger.info("Phát hiện nút 'Sign in' trong cài đặt Forwarding -> Click để xác thực lại...")
    main_handle = driver.current_window_handle
    handles_before = set(driver.window_handles)

    btn = signin_btns[0]
    if not safe_click(btn, delay=1.5):
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(1.5)

    # Chờ popup đăng nhập Microsoft mở ra (hoặc redirect trên cùng tab)
    popup_handle = None
    for _ in range(16):
        time.sleep(0.5)
        cur_handles = set(driver.window_handles)
        diff = cur_handles - handles_before
        if diff:
            popup_handle = list(diff)[0]
            break
        if "login.live.com" in (driver.current_url or "").lower():
            break

    if popup_handle:
        driver.switch_to.window(popup_handle)
        logger.info(f"Đã chuyển sang popup đăng nhập Forwarding: {driver.current_url[:70]}")

    try:
        wait_for_page_load(driver, timeout=15)
    except Exception:
        pass
    time.sleep(2)

    # Tìm và click 'Use your password' (hoặc chờ ô nhập password hiện ra)
    pass_input = None
    driver.implicitly_wait(0)
    try:
        for _ in range(15):
            # Nếu popup đã tự đóng (SSO tự động hoàn tất)
            if popup_handle and popup_handle not in driver.window_handles:
                logger.info("Popup đăng nhập Forwarding đã tự động xác thực và đóng")
                driver.switch_to.window(main_handle)
                time.sleep(3)
                return True

            # 1. Kiểm tra nếu ô nhập password đã hiển thị sẵn
            pwd_els = [
                el for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']")
                if el.is_displayed()
            ]
            if pwd_els:
                pass_input = pwd_els[0]
                break

            # 2. Kiểm tra nút/link 'Use your password' (như trên màn hình Verify your email)
            use_pwd_els = [
                el for el in driver.find_elements(
                    By.XPATH,
                    "//button[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use your password')"
                    " or contains(normalize-space(), 'Dùng mật khẩu') or contains(normalize-space(), 'Sử dụng mật khẩu')]"
                    " | //a[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use your password')"
                    " or contains(normalize-space(), 'Dùng mật khẩu') or contains(normalize-space(), 'Sử dụng mật khẩu')]"
                    " | //*[@id='idA_PWD_SwitchToPassword' or @id='otcLoginLink']"
                    " | //*[@role='button' or contains(@class, 'fui-Link')][contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use your password')"
                    " or contains(normalize-space(), 'Dùng mật khẩu') or contains(normalize-space(), 'Sử dụng mật khẩu')]",
                )
                if el.is_displayed()
            ]
            if use_pwd_els:
                target_el = use_pwd_els[-1]
                logger.info("Phát hiện nút 'Use your password' -> Click để chuyển sang nhập mật khẩu...")
                if not safe_click(target_el, delay=1.5):
                    driver.execute_script("arguments[0].click();", target_el)
                    time.sleep(1.5)
                continue

            # 3. Nếu hiện 'Other ways to sign in' trước khi hiện 'Use your password'
            other_ways_els = [
                el for el in driver.find_elements(
                    By.XPATH,
                    "//*[normalize-space()='Other ways to sign in' or normalize-space()='Các cách đăng nhập khác']",
                )
                if el.is_displayed()
            ]
            if other_ways_els:
                if not safe_click(other_ways_els[0], delay=1.5):
                    driver.execute_script("arguments[0].click();", other_ways_els[0])
                    time.sleep(1.5)
                continue

            # 4. Nếu hiện màn hình chọn tài khoản (Pick an account)
            if hotmail_email:
                acc_els = [
                    el for el in driver.find_elements(
                        By.XPATH,
                        f"//*[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{hotmail_email.lower()}')]",
                    )
                    if el.is_displayed()
                ]
                if acc_els and "pick an account" in (driver.find_element(By.TAG_NAME, "body").text or "").lower():
                    safe_click(acc_els[0], delay=1.5)
                    continue

            # 5. Nếu popup đang ở màn hình 'Almost there' / 'Send a code to' / 'Help us protect your account' (không có nút Use your password) -> thoát vòng lặp ngay để xử lý mã code
            try:
                b_txt = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
                if (
                    "almost there" in b_txt
                    or "just one more step" in b_txt
                    or "send a code to" in b_txt
                    or "help us protect your account" in b_txt
                ):
                    break
            except Exception:
                pass

            time.sleep(1)
    finally:
        driver.implicitly_wait(5)

    if not pass_input:
        pass_input = wait_for_element(
            driver, By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']",
            timeout=2, condition="visible",
        )

    if pass_input:
        logger.info(f"Điền mật khẩu Hotmail ('{new_hotmail_pass}') để xác thực Forwarding...")
        safe_click(pass_input, delay=0.2)
        pass_input.send_keys(Keys.CONTROL + "a")
        pass_input.send_keys(Keys.BACKSPACE)
        safe_send_keys(pass_input, new_hotmail_pass, delay=0.8, clear_first=False)
        submit_btn = wait_for_element(
            driver, By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']",
            timeout=3, condition="clickable",
        )
        if submit_btn:
            safe_click(submit_btn, delay=2.5)
        else:
            pass_input.send_keys(Keys.RETURN)
            time.sleep(2.5)

        # Nếu mật khẩu đầu tiên không đúng -> thử lần lượt các mật khẩu ứng viên ('Khoa01@c', 'Khoa01@@c', mật khẩu gốc)
        try:
            fallback_candidates = []
            for p_cand in ("Khoa01@c", "Khoa01@@c", old_hotmail_pass):
                if p_cand and p_cand != new_hotmail_pass and p_cand not in fallback_candidates:
                    fallback_candidates.append(p_cand)

            for alt_pass in fallback_candidates:
                if popup_handle and popup_handle not in driver.window_handles:
                    break
                body_lower = driver.find_element(By.TAG_NAME, "body").text.lower()
                if "incorrect" not in body_lower and "không chính xác" not in body_lower:
                    break
                logger.warning(f"Mật khẩu trước không khớp khi xác thực Forwarding, thử tiếp mật khẩu '{alt_pass}'...")
                pass_input2 = wait_for_element(
                    driver, By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']",
                    timeout=3, condition="visible",
                )
                if pass_input2:
                    safe_click(pass_input2, delay=0.2)
                    pass_input2.send_keys(Keys.CONTROL + "a")
                    pass_input2.send_keys(Keys.BACKSPACE)
                    safe_send_keys(pass_input2, alt_pass, delay=0.8, clear_first=False)
                    submit_btn2 = wait_for_element(
                        driver, By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']",
                        timeout=3, condition="clickable",
                    )
                    if submit_btn2:
                        safe_click(submit_btn2, delay=2.5)
                    else:
                        pass_input2.send_keys(Keys.RETURN)
                        time.sleep(2.5)
        except Exception:
            pass

    # Kiểm tra cả TRƯỚC và SAU khi nhập mật khẩu: nếu popup hiện 'Almost there' / 'Send a code to ...' / 'Verify your email' / 'Help us protect your account'
    try:
        if not popup_handle or popup_handle in driver.window_handles:
            body_lower_popup = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
            cur_u_popup = (driver.current_url or "").lower()
            if (
                "almost there" in body_lower_popup
                or "just one more step" in body_lower_popup
                or "send a code to" in body_lower_popup
                or "verify your email" in body_lower_popup
                or "verify your identity" in body_lower_popup
                or "help us protect your account" in body_lower_popup
                or "identity/confirm" in cur_u_popup
            ):
                logger.info("Popup xác thực Forwarding yêu cầu xác minh email khôi phục ('Almost there' / 'Send a code to')...")
                _handle_add_recovery_email(driver, hotmail_email, recovery_email, fallback_pass=new_hotmail_pass)
    except Exception as e:
        logger.debug(f"Lỗi khi xử lý xác minh email trong popup Forwarding: {e}")

    # Chờ popup tự đóng (tối đa 10s)
    if popup_handle:
        for _ in range(20):
            if popup_handle not in driver.window_handles:
                break
            time.sleep(0.5)
        if main_handle in driver.window_handles:
            driver.switch_to.window(main_handle)

    time.sleep(3)
    logger.info("✅ Đã hoàn tất bước Sign in xác thực Forwarding!")
    return True


def _is_hotmail_already_logged_in(driver: WebDriver) -> bool:
    """
    Kiểm tra xem tab hiện tại đã đăng nhập vào hộp thư Outlook/Hotmail chưa.
    """
    driver.implicitly_wait(0)
    try:
        cur_u = (driver.current_url or "").lower()
        if "outlook.live.com/mail" not in cur_u:
            return False
        if any(k in cur_u for k in ("login.live.com", "login.microsoftonline.com", "oauth20_authorize", "prompt=select_account")):
            return False

        # Nếu trên trang có ô nhập email / password hoặc màn hình Pick an account -> chưa login
        login_inputs = [
            el for el in driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='email'], #i0116, #usernameEntry, input[name='loginfmt'], input[type='password'], #i0118, input[name='passwd']",
            )
            if el.is_displayed()
        ]
        if login_inputs:
            return False

        body_low = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
        if "pick an account" in body_low or "sign in to your account" in body_low:
            return False

        return True
    except Exception:
        return False
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass


def _ensure_hotmail_logged_in_before_forwarding(
    driver: WebDriver,
    hotmail_email: str,
    new_hotmail_pass: str = "",
    old_hotmail_pass: str = "",
    recovery_email: str = "",
) -> bool:
    """
    Trước khi sang Bước 3 (Add mail forwarding):
    1. Vào trang Hotmail kiểm tra xem đã login chưa.
    2. Nếu đã login rồi -> chuyển thẳng sang Bước 3 luôn.
    3. Nếu chưa login -> đăng nhập bằng tài khoản Hotmail với mật khẩu mới ('Khoa01@c' / new_hotmail_pass)
       rồi mới chuyển sang Bước 3.
    """
    cur_u = (driver.current_url or "").lower()

    # Nếu đang đứng ở tab Facebook (ví dụ bỏ qua Bước 2, nhảy thẳng từ Bước 1 sang Bước 3) -> mở tab mới cho Hotmail
    if "facebook.com" in cur_u or len(driver.window_handles) == 1 and "live.com" not in cur_u and "microsoft" not in cur_u:
        logger.info("Mở tab mới để kiểm tra trạng thái đăng nhập Hotmail trước Bước 3...")
        _open_new_tab(driver, "https://outlook.live.com/mail/0/")
    elif "outlook.live.com/mail" not in cur_u:
        logger.info("Truy cập https://outlook.live.com/mail/0/ để kiểm tra đã đăng nhập Hotmail chưa...")
        driver.get("https://outlook.live.com/mail/0/")

    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
    time.sleep(3)

    if _is_hotmail_already_logged_in(driver):
        logger.info("✅ Hotmail đã đăng nhập sẵn -> Chuyển thẳng sang Bước 3 (Add mail forwarding)!")
        return True

    # Chưa đăng nhập -> Thực hiện đăng nhập bằng mật khẩu mới (ưu tiên 'Khoa01@c' & new_hotmail_pass)
    primary_new_pass = new_hotmail_pass or "Khoa01@c"
    logger.info(
        f"ℹ️ Hotmail chưa đăng nhập (URL: {driver.current_url[:65]}) "
        f"-> Đăng nhập tài khoản {hotmail_email} với mật khẩu mới trước khi sang Bước 3..."
    )

    cur_u = (driver.current_url or "").lower()
    driver.implicitly_wait(0)
    try:
        has_login_ui = bool(
            driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='email'], #i0116, #usernameEntry, input[name='loginfmt'], input[type='password'], #i0118, input[name='passwd']",
            )
        )
    except Exception:
        has_login_ui = False
    finally:
        driver.implicitly_wait(5)

    if "login.live.com" not in cur_u and not has_login_ui:
        driver.get("https://outlook.live.com/mail/?prompt=select_account")
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2)

    # Nếu hiện màn hình 'Pick an account' có sẵn email, bấm vào tài khoản đó; ngược lại nhập email
    clicked_Tile = False
    driver.implicitly_wait(0)
    try:
        b_low = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
        if "pick an account" in b_low and hotmail_email:
            acc_els = [
                el for el in driver.find_elements(
                    By.XPATH,
                    f"//*[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{hotmail_email.lower()}')]",
                )
                if el.is_displayed()
            ]
            if acc_els:
                logger.info(f"Chọn tài khoản '{hotmail_email}' trên màn hình Pick an account...")
                safe_click(acc_els[0], delay=2.0)
                clicked_Tile = True
    except Exception:
        pass
    finally:
        driver.implicitly_wait(5)

    if not clicked_Tile:
        # Kiểm tra xem đang ở ô nhập email hay đã ở sẵn ô nhập password
        driver.implicitly_wait(0)
        try:
            pwd_visible = any(
                el.is_displayed()
                for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']")
            )
        except Exception:
            pwd_visible = False
        finally:
            driver.implicitly_wait(5)

        if not pwd_visible:
            _enter_email(driver, hotmail_email)

    # Nhập mật khẩu mới (ưu tiên 'Khoa01@c' / new_hotmail_pass, tự động thử cả 'Khoa01@c', 'Khoa01@@c', và mật khẩu gốc nếu cần)
    _enter_password(driver, primary_new_pass, hotmail_email, recovery_email)

    # Kiểm tra nếu sau khi _enter_password vẫn báo sai mật khẩu -> thử lần lượt 'Khoa01@c', 'Khoa01@@c', old_hotmail_pass
    try:
        for cand_pw in ("Khoa01@c", "Khoa01@@c", new_hotmail_pass, old_hotmail_pass):
            if not cand_pw:
                continue
            body_low = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
            if "incorrect" not in body_low and "không chính xác" not in body_low:
                break
            logger.info(f"Thử đăng nhập Hotmail với mật khẩu '{cand_pw}'...")
            p_inp = wait_for_element(
                driver, By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']",
                timeout=3, condition="visible",
            )
            if p_inp:
                safe_click(p_inp, delay=0.2)
                p_inp.send_keys(Keys.CONTROL + "a")
                p_inp.send_keys(Keys.BACKSPACE)
                safe_send_keys(p_inp, cand_pw, delay=0.8, clear_first=False)
                s_btn = wait_for_element(
                    driver, By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']",
                    timeout=3, condition="clickable",
                )
                if s_btn:
                    safe_click(s_btn, delay=2.5)
                else:
                    p_inp.send_keys(Keys.RETURN)
                    time.sleep(2.5)
    except Exception:
        pass

    handle_microsoft_prompts(driver, hotmail_email, recovery_email, current_password=primary_new_pass)
    time.sleep(2)
    logger.info("✅ Đã đăng nhập lại Hotmail bằng mật khẩu mới thành công, chuyển sang Bước 3!")
    return True


def setup_hotmail_forwarding(
    driver: WebDriver,
    hotmail_email: str,
    recovery_email: str = "",
    new_hotmail_pass: str = "",
    old_hotmail_pass: str = "",
) -> str:
    """
    Trước khi vào cài đặt Forwarding:
    - Kiểm tra trang Hotmail đã login chưa; nếu chưa thì đăng nhập bằng mật khẩu mới ('Khoa01@c' / new_hotmail_pass).
    Sau đó truy cập https://outlook.live.com/mail/options/mail/forwarding,
    đóng popup lần đầu (nếu có), xử lý nút 'Sign in' (nếu có),
    bật 'Enable forwarding', điền <prefix>@nguyenmaucole.io.vn và bấm 'Save'.
    Trả về địa chỉ email forwarding nếu thành công, ngược lại trả về chuỗi rỗng "".
    """
    prefix = hotmail_email.split("@")[0].strip()
    forward_email = f"{prefix}@{TEMPMAIL_DOMAIN}"
    forwarding_url = "https://outlook.live.com/mail/options/mail/forwarding"
    if not new_hotmail_pass:
        new_hotmail_pass = getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@c")

    logger.info(f"=== BƯỚC 3: Cài đặt Forwarding ({forwarding_url} -> {forward_email}) ===")
    try:
        # Kiểm tra đã login Hotmail chưa; nếu chưa thì login bằng mật khẩu mới trước khi vào trang Forwarding
        _ensure_hotmail_logged_in_before_forwarding(
            driver, hotmail_email, new_hotmail_pass, old_hotmail_pass, recovery_email
        )

        driver.get(forwarding_url)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(4)

        cur_u = (driver.current_url or "").lower()
        if "login." in cur_u or "account.live.com" in cur_u or "privacynotice" in cur_u:
            handle_microsoft_prompts(driver, hotmail_email, recovery_email, current_password=old_hotmail_pass)
            if "outlook.live.com/mail/options/mail/forwarding" not in (driver.current_url or "").lower():
                driver.get(forwarding_url)
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(4)

        # Đóng các popup xuất hiện lần đầu trên Outlook (nếu có)
        _dismiss_outlook_popups(driver)

        # Chờ khung Settings render ra 'Enable forwarding' hoặc nút 'Sign in' (tối đa 20s)
        enable_lbl = None
        for _ in range(20):
            _dismiss_outlook_popups(driver)
            driver.implicitly_wait(0)
            try:
                lbls = [
                    el for el in driver.find_elements(
                        By.XPATH,
                        "//label[contains(normalize-space(), 'Enable forwarding') or contains(normalize-space(), 'Bật chuyển tiếp')]",
                    )
                    if el.is_displayed()
                ]
            except Exception:
                lbls = []
            finally:
                driver.implicitly_wait(5)

            if lbls:
                enable_lbl = lbls[0]
                break

            if _handle_forwarding_signin(driver, hotmail_email, new_hotmail_pass, old_hotmail_pass, recovery_email):
                _dismiss_outlook_popups(driver)
                enable_lbl = wait_for_element(
                    driver,
                    By.XPATH,
                    "//label[contains(normalize-space(), 'Enable forwarding') or contains(normalize-space(), 'Bật chuyển tiếp')]",
                    timeout=15,
                    condition="visible",
                )
                break

            time.sleep(1)

        if not enable_lbl:
            # Thử đóng popup lần nữa và mở lại link forwarding nếu Settings bị che/đóng
            _dismiss_outlook_popups(driver)
            driver.get(forwarding_url)
            time.sleep(4)
            _dismiss_outlook_popups(driver)
            _handle_forwarding_signin(driver, hotmail_email, new_hotmail_pass, old_hotmail_pass, recovery_email)
            enable_lbl = wait_for_element(
                driver,
                By.XPATH,
                "//label[contains(normalize-space(), 'Enable forwarding') or contains(normalize-space(), 'Bật chuyển tiếp')]",
                timeout=15,
                condition="visible",
            )
        if not enable_lbl:
            logger.error("Không tìm thấy nút 'Enable forwarding' trên trang cài đặt Outlook")
            return ""

        # Kiểm tra công tắc switch đã bật chưa
        switches = driver.find_elements(By.CSS_SELECTOR, "[role='dialog'] input[role='switch'], input[role='switch']")
        is_enabled = False
        if switches:
            sw = switches[0]
            is_enabled = bool(sw.is_selected() or sw.get_attribute("checked") == "true" or sw.get_attribute("aria-checked") == "true")

        if not is_enabled:
            if not safe_click(enable_lbl, delay=1.5):
                driver.execute_script("arguments[0].click();", enable_lbl)
                time.sleep(1.5)
            logger.info("Đã bật 'Enable forwarding'")
        else:
            logger.info("'Enable forwarding' đã được bật sẵn")

        # Tìm ô nhập 'Forward my email to:'
        fwd_input = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "input[placeholder='Enter an email address'], input[placeholder*='email'], [role='dialog'] input[type='text']",
            timeout=8,
            condition="visible",
        )
        if not fwd_input:
            logger.error("Không tìm thấy ô nhập email Forwarding")
            return ""

        # Xóa nội dung cũ (nếu có) và nhập email @nguyenmaucole.io.vn
        safe_click(fwd_input, delay=0.3)
        fwd_input.send_keys(Keys.CONTROL + "a")
        fwd_input.send_keys(Keys.BACKSPACE)
        safe_send_keys(fwd_input, forward_email, delay=1.0, clear_first=False)
        logger.info(f"Đã điền email chuyển tiếp: {forward_email}")

        # Tích chọn 'Keep a copy of forwarded messages' nếu chưa tích
        try:
            keep_lbl = wait_for_element(
                driver,
                By.XPATH,
                "//label[contains(normalize-space(), 'Keep a copy') or contains(normalize-space(), 'Giữ một bản sao') or contains(normalize-space(), 'Giữ bản sao')]",
                timeout=3,
                condition="visible",
            )
            if keep_lbl:
                keep_cbs = driver.find_elements(
                    By.CSS_SELECTOR, "[role='dialog'] input[type='checkbox']:not([role='switch'])"
                )
                if keep_cbs and not (keep_cbs[0].is_selected() or keep_cbs[0].get_attribute("checked")):
                    safe_click(keep_lbl, delay=0.5)
                    logger.info("Đã tích 'Keep a copy of forwarded messages'")
        except Exception:
            pass

        # Bấm nút Save (nếu nút Save hiển thị / có thay đổi)
        save_btn = wait_for_element(
            driver,
            By.XPATH,
            "//div[@role='dialog']//button[normalize-space()='Save' or normalize-space()='Lưu'] | //button[normalize-space()='Save' or normalize-space()='Lưu']",
            timeout=5,
            condition="clickable",
        )
        if save_btn:
            if not safe_click(save_btn, delay=3.0):
                driver.execute_script("arguments[0].click();", save_btn)
                time.sleep(3.0)
            logger.info("✅ Đã bấm 'Save' lưu cài đặt Forwarding thành công!")
        else:
            logger.info("Cài đặt Forwarding đã được lưu trước đó (không cần bấm Save)")

        return forward_email
    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi khi cài đặt Forwarding Hotmail: {e}")
        return ""


def login_hotmail(
    driver: WebDriver, hotmail_email: str, hotmail_password: str,
    recovery_email: str = "",
) -> bool:
    """
    Đăng nhập Hotmail trên Tab 2.
    CHỈ chạy khi Facebook login (Bước 1) thành công.

    Returns:
        True nếu đăng nhập thành công.
    """
    logger.info(f"=== BƯỚC 2: Đăng nhập Hotmail (Email: {hotmail_email}) ===")

    try:
        # Mở Tab 2 trực tiếp tới trang đăng nhập Outlook Web App (tránh treo ở trang support.microsoft.com)
        login_url = "https://outlook.live.com/mail/?prompt=select_account"
        if not _open_new_tab(driver, login_url):
            raise LoginFailedError("Không thể mở Tab 2")

        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2)

        # Nhập email
        if not _enter_email(driver, hotmail_email):
            raise LoginFailedError("Không thể nhập email Hotmail")

        # Nhập password (hoặc Verify your email nếu có)
        if not _enter_password(driver, hotmail_password, hotmail_email, recovery_email):
            raise LoginFailedError("Không thể nhập password Hotmail")

        # Xử lý các bước bảo mật / Add email / Privacy notice / Stay signed in
        handle_microsoft_prompts(driver, hotmail_email, recovery_email, current_password=hotmail_password)

        # Xác nhận
        time.sleep(2)
        current_url = driver.current_url

        cur_lower = current_url.lower()
        if (
            "login.live.com/login.srf" not in cur_lower
            and "login.live.com/oauth20_authorize.srf" not in cur_lower
            and "interrupt/credentialaction" not in cur_lower
            and "identity/confirm" not in cur_lower
        ):
            logger.info("✅ Đăng nhập Hotmail thành công!")
            return True
        else:
            logger.warning(f"Hotmail có thể chưa login. URL: {current_url}")
            return False

    except (BrowserClosedError, LoginFailedError):
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi đăng nhập Hotmail: {e}")
        raise LoginFailedError(f"Lỗi đăng nhập Hotmail: {e}")
