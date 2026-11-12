"""
Bước 2: Tự động đăng nhập Hotmail trên Tab 2.
CHỈ chạy khi Bước 1 (Facebook login) thành công.
"""
import re
import time
import threading
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

BOXNT_DOMAIN = "boxnt.com"
BOXNT_DOMAINS = (
    "boxnt.com",
    "tempinbox.vn",
    "tmpmails.net",
)
BOXNT_WEB_URL = f"https://{BOXNT_DOMAIN}/"
BOXNT_API_BASE = f"https://{BOXNT_DOMAIN}/api/v1"

_thread_ctx = threading.local()
_global_default_recovery_domain = TEMPMAIL_DOMAIN


def set_preferred_recovery_domain(domain: str) -> None:
    """Thiết lập domain khôi phục ưu tiên cho luồng hiện tại (vd: fviainboxes.com hoặc nguyenmaucole.io.vn)."""
    global _global_default_recovery_domain
    dom = (domain or TEMPMAIL_DOMAIN).strip().lstrip("@").lower()
    _thread_ctx.recovery_domain = dom
    _global_default_recovery_domain = dom


def get_preferred_recovery_domain(explicit_domain: str = "") -> str:
    """Lấy domain khôi phục ưu tiên đã chọn trên GUI."""
    if explicit_domain and explicit_domain.strip():
        return explicit_domain.strip().lstrip("@").lower()
    return getattr(_thread_ctx, "recovery_domain", None) or _global_default_recovery_domain or TEMPMAIL_DOMAIN


def _is_fvia_domain(text_or_domain: str) -> bool:
    """Kiểm tra xem chuỗi/domain có thuộc hệ thống fviainboxes.com không."""
    t = (text_or_domain or "").lower()
    return any(d in t for d in FVIA_DOMAINS)


def _is_boxnt_domain(text_or_domain: str) -> bool:
    """Kiểm tra xem chuỗi/domain có thuộc hệ thống boxnt.com (TmpMails.Net) không."""
    t = (text_or_domain or "").lower()
    return any(d in t for d in BOXNT_DOMAINS)


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


def _handle_microsoft_outlook_landing_page(driver: WebDriver) -> bool:
    """
    Xử lý khi trình duyệt bị chuyển hướng sang trang giới thiệu Microsoft 365 Outlook
    (microsoft.com/.../microsoft-365/outlook/...):
      1. Tự động bấm 'Accept' trên thanh thông báo Cookie (nếu có).
      2. Bấm nút 'Sign in' trên thanh điều hướng Outlook để vào đúng luồng đăng nhập OWA.
      3. Nếu nút 'Sign in' mở tab mới thì chuyển sang tab mới và đóng tab giới thiệu cũ.
    """
    current_url = (driver.current_url or "").lower()
    if "microsoft.com/" not in current_url or "login.live.com" in current_url or "account.microsoft.com" in current_url:
        return False

    logger.info(f"Phát hiện trang giới thiệu Microsoft 365 Outlook ({current_url[:70]}) -> Xử lý Cookie 'Accept' & 'Sign in'...")
    driver.implicitly_wait(0)
    try:
        # 1. Bấm nút 'Accept' trên thông báo Cookie của Microsoft (nếu có)
        accept_btns = [
            b for b in driver.find_elements(
                By.XPATH,
                "//button[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận' "
                "or .//span[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận']]",
            )
            if b.is_displayed()
        ]
        if accept_btns:
            btn = accept_btns[0]
            if not safe_click(btn, delay=1.0):
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(1.0)
            logger.info("✅ Đã bấm 'Accept' trên thông báo Cookie của Microsoft")

        # 2. Tìm và bấm nút 'Sign in' trên trang Microsoft 365 Outlook
        signin_els = [
            el for el in driver.find_elements(
                By.XPATH,
                "//a[normalize-space()='Sign in' or normalize-space()='Đăng nhập' or contains(@data-bi-cn, 'SignIn') or contains(@href, 'fwlink/?linkid=2276500')]"
                " | //button[normalize-space()='Sign in' or normalize-space()='Đăng nhập']",
            )
            if el.is_displayed()
        ]
        if signin_els:
            target = signin_els[0]
            old_handle = driver.current_window_handle
            handles_before = set(driver.window_handles)
            if not safe_click(target, delay=1.5):
                driver.execute_script("arguments[0].click();", target)
                time.sleep(1.5)
            logger.info("✅ Đã bấm nút 'Sign in' trên trang Microsoft 365 Outlook")

            # Chờ tab mới mở ra hoặc URL hiện tại chuyển sang login.live.com / outlook.live.com/mail
            for _ in range(12):
                time.sleep(0.5)
                cur_handles = set(driver.window_handles)
                diff = cur_handles - handles_before
                if diff:
                    new_handle = list(diff)[0]
                    try:
                        if old_handle in driver.window_handles and len(driver.window_handles) > 1:
                            driver.switch_to.window(old_handle)
                            driver.close()
                    except Exception:
                        pass
                    driver.switch_to.window(new_handle)
                    break
                u_now = (driver.current_url or "").lower()
                if "login.live.com" in u_now or "outlook.live.com/mail" in u_now:
                    break
    except Exception as e:
        logger.debug(f"Lỗi khi xử lý trang Microsoft 365 Outlook landing page: {e}")
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass

    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
    time.sleep(2)
    return True


def _enter_email(driver: WebDriver, email: str) -> bool:
    """Nhập email Hotmail vào trang đăng nhập Microsoft."""
    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
    time.sleep(2)

    current_url = driver.current_url.lower()
    if any(k in current_url for k in ["outlook.live.com/mail/0", "account.microsoft.com", "account.live.com"]):
        if "login.live.com" not in current_url and "oauth20_authorize" not in current_url:
            logger.info("Tài khoản Hotmail đã đăng nhập sẵn trong profile!")
            return True

    # Nếu bị redirect sang trang quảng cáo Microsoft 365 -> bấm Accept cookie + Sign in
    if "microsoft.com/" in current_url and "login.live.com" not in current_url:
        _handle_microsoft_outlook_landing_page(driver)
        current_url = (driver.current_url or "").lower()
        if "outlook.live.com/mail" in current_url and "login.live.com" not in current_url:
            logger.info("Tài khoản Hotmail đã đăng nhập sẵn sau khi bấm Sign in!")
            return True

    email_selectors = [
        (By.CSS_SELECTOR, "input[type='email'], #i0116, #usernameEntry, input[name='loginfmt']"),
    ]

    email_input = None
    for by, value in email_selectors:
        email_input = wait_for_element(
            driver, by, value, timeout=8, condition="visible",
        )
        if email_input:
            break

    if not email_input:
        current_url = driver.current_url.lower()
        if "outlook.live.com/mail" in current_url and "login.live.com" not in current_url:
            logger.info("Tài khoản Hotmail đã đăng nhập sẵn trong profile!")
            return True
        # Thử mở trực tiếp login.live.com một lần nữa
        logger.info("Chưa thấy ô email -> Mở trực tiếp https://login.live.com/ ...")
        driver.get("https://login.live.com/")
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2)
        for by, value in email_selectors:
            email_input = wait_for_element(driver, by, value, timeout=8, condition="visible")
            if email_input:
                break

    if not email_input:
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


def _submit_password_with_fallbacks(driver: WebDriver, candidates: list) -> bool:
    """
    Thử nhập lần lượt các mật khẩu trong danh sách `candidates` vào ô password của Microsoft.
    Nếu mật khẩu hiện tại (vd 'Khoa01@@c') báo 'That password is incorrect' / 'không chính xác',
    tự động xóa sạch ô password và dùng mật khẩu tiếp theo (vd mật khẩu gốc trong Google Sheet).
    """
    unique_cands = []
    for c in candidates:
        if c and isinstance(c, str) and c.strip() and c.strip() not in unique_cands:
            unique_cands.append(c.strip())

    if not unique_cands:
        return False

    pwd_selector = "input[type='password'], #i0118, #passwordEntry, input[name='passwd']"
    initial_handles = set(driver.window_handles)

    for idx, cand_pw in enumerate(unique_cands):
        # Nếu cửa sổ popup OAuth đã tự đóng sau lần submit trước -> thành công
        try:
            if len(driver.window_handles) < len(initial_handles):
                return True
        except Exception:
            return True

        pass_input = wait_for_element(
            driver, By.CSS_SELECTOR, pwd_selector,
            timeout=5 if idx == 0 else 4, condition="visible",
        )
        if not pass_input:
            # Không còn ô nhập password trên màn hình -> đã qua bước mật khẩu
            return True

        if idx == 0:
            logger.info(f"Điền mật khẩu Hotmail: '{cand_pw}'...")
        else:
            logger.warning(
                f"⚠️ Mật khẩu trước không đúng -> Chuyển sang dùng mật khẩu dự phòng (Google Sheet / cấu hình): '{cand_pw}'..."
            )

        try:
            safe_click(pass_input, delay=0.2)
            pass_input.send_keys(Keys.CONTROL + "a")
            pass_input.send_keys(Keys.BACKSPACE)
            try:
                pass_input.clear()
            except Exception:
                pass
            safe_send_keys(pass_input, cand_pw, delay=0.8, clear_first=False)
        except Exception as e:
            logger.debug(f"Lỗi khi nhập mật khẩu '{cand_pw}': {e}")
            continue

        signin_btn = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "button[type='submit'], #idSIButton9, input[type='submit']",
            timeout=3,
            condition="clickable",
        )
        if signin_btn:
            safe_click(signin_btn, delay=2.0)
            logger.info("Đã click 'Next' / 'Sign in'")
        else:
            pass_input.send_keys(Keys.RETURN)
            time.sleep(2.0)

        # Chờ tối đa 4s để kiểm tra kết quả: hoặc chuyển trang (mất ô password), hoặc hiện thông báo lỗi đỏ
        pwd_accepted = False
        driver.implicitly_wait(0)
        try:
            for _ in range(8):
                if len(driver.window_handles) < len(initial_handles):
                    pwd_accepted = True
                    break
                try:
                    body_lower = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
                except Exception:
                    body_lower = ""

                has_pwd_error = (
                    "incorrect" in body_lower
                    or "không chính xác" in body_lower
                    or "không đúng" in body_lower
                    or "doesn't match" in body_lower
                )
                if has_pwd_error:
                    pwd_accepted = False
                    break

                visible_pwds = [
                    el for el in driver.find_elements(By.CSS_SELECTOR, pwd_selector)
                    if el.is_displayed()
                ]
                if not visible_pwds:
                    # Đợi thêm 0.5s xác nhận không phải trang đang reload lại ô password
                    time.sleep(0.5)
                    visible_pwds_retry = [
                        el for el in driver.find_elements(By.CSS_SELECTOR, pwd_selector)
                        if el.is_displayed()
                    ]
                    body_retry = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
                    if not visible_pwds_retry and "incorrect" not in body_retry and "không chính xác" not in body_retry:
                        pwd_accepted = True
                        break

                time.sleep(0.5)
        finally:
            driver.implicitly_wait(5)

        if pwd_accepted:
            logger.info(f"✅ Mật khẩu '{cand_pw}' chính xác!")
            return True

    return False


def _try_use_your_password(
    driver: WebDriver,
    primary_pass: str = "",
    fallback_pass: str = "",
    extra_fallbacks: Optional[list] = None,
) -> bool:
    """
    Khi gặp màn hình 'Verify your email' (hoặc màn hình xác thực Microsoft) có nút 'Use your password':
    1. Luôn bấm 'Use your password'.
    2. Bước tiếp theo điền mật khẩu primary_pass; nếu không đúng thì tự động dùng mật khẩu trong Google Sheet (fallback_pass).
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
    logger.info("Phát hiện nút 'Use your password' -> Click để chuyển sang nhập mật khẩu...")
    if not safe_click(target_el, delay=1.5):
        driver.execute_script("arguments[0].click();", target_el)
        time.sleep(1.5)

    candidates = [
        primary_pass,
        fallback_pass,
        getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c"),
        "Khoa01@@c",
        "Khoa01@c",
    ]
    if extra_fallbacks:
        candidates.extend(extra_fallbacks)

    if _submit_password_with_fallbacks(driver, candidates):
        return True

    # Nếu tất cả mật khẩu đều không đúng và có nút "Send a code to ..." -> chuyển sang lấy code qua email khôi phục
    try:
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
            if send_code_link:
                logger.info("Click 'Send a code to ...' để xác minh qua email khôi phục")
                if not safe_click(send_code_link, delay=2.0):
                    driver.execute_script("arguments[0].click();", send_code_link)
                    time.sleep(2.0)
    except Exception:
        pass

    return False


def _enter_password(
    driver: WebDriver,
    password: str,
    hotmail_email: str = "",
    recovery_email: str = "",
    fallback_password: str = "",
) -> bool:
    """
    Nhập password Hotmail:
    - Nếu mật khẩu đầu tiên ('Khoa01@@c' hoặc password truyền vào) không đúng,
      tự động thử mật khẩu trong data Google Sheet (fallback_password) và các mật khẩu cấu hình.
    """
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

            # 1. Nếu đã chuyển thẳng sang các màn hình sau đăng nhập (Passkey, Stay signed in, TOU, Looks good, Outlook...)
            if (
                any(k in current_url for k in [
                    "outlook.live.com/mail", "account.microsoft.com",
                    "interrupt/credentialaction", "privacynotice", "account.live.com",
                    "fido/create", "tou/accrue", "proofs/remind",
                ])
                or "stay signed in?" in body_lower
                or "setting up your passkey" in body_lower
                or "is your security info still accurate" in body_lower
                or "looks good!" in body_lower
            ):
                logger.info("Tài khoản đã chuyển thẳng qua bước sau đăng nhập / bảo mật")
                return True

            # 2. Kiểm tra ô nhập password đã hiển thị chưa
            pwd_els = [
                el for el in driver.find_elements(
                    By.CSS_SELECTOR, "input[type='password'], #i0118, #passwordEntry, input[name='passwd']"
                )
                if el.is_displayed()
            ]
            if pwd_els:
                pass_input = pwd_els[0]
                break

            # 3. Nếu hiện nút 'Use your password' -> thử password & fallback_password (mật khẩu Google Sheet)
            if "use your password" in body_lower or "dùng mật khẩu" in body_lower or "sử dụng mật khẩu" in body_lower:
                if _try_use_your_password(
                    driver,
                    primary_pass=password,
                    fallback_pass=fallback_password or getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c"),
                    extra_fallbacks=[fallback_password, getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c"), "Khoa01@@c", "Khoa01@c"],
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
                or _is_boxnt_domain(body_lower)
            ) and (hotmail_email or recovery_email):
                logger.info("Phát hiện màn hình xác minh ('Almost there' / 'Verify your email') -> Xử lý qua email khôi phục...")
                driver.implicitly_wait(5)
                if _handle_add_recovery_email(driver, hotmail_email, recovery_email, fallback_pass=fallback_password or password):
                    return True
    finally:
        driver.implicitly_wait(5)

    if not pass_input:
        logger.error("Không tìm thấy ô password trên Microsoft Login")
        return False

    # Thử lần lượt: mật khẩu chính -> mật khẩu trong Google Sheet (fallback_password) -> Khoa01@@c -> Khoa01@c
    candidates = [
        password,
        fallback_password,
        getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c"),
        "Khoa01@@c",
        "Khoa01@c",
    ]
    if _submit_password_with_fallbacks(driver, candidates):
        return True

    # Nếu tất cả mật khẩu đều báo sai -> thử bấm 'Send a code to ...' nếu có
    try:
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
                return _handle_add_recovery_email(driver, hotmail_email, recovery_email, fallback_pass=fallback_password or password)
    except Exception:
        pass

    return False


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


def _get_existing_boxnt_ids(prefix: str, domain: str = BOXNT_DOMAIN) -> set:
    """Kích hoạt hòm thư boxnt.com (TmpMails.Net) qua API /api/v1/mailboxes và lấy danh sách ID thư cũ hiện có."""
    seen_ids = set()
    target_domain = (domain or BOXNT_DOMAIN).strip().lower()
    try:
        create_resp = requests.post(
            f"{BOXNT_API_BASE}/mailboxes",
            json={"localPart": prefix.strip().lower(), "domain": target_domain},
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
            timeout=10,
        )
        if create_resp.status_code in (200, 201):
            tok = (create_resp.json().get("data") or {}).get("token", "")
            if tok:
                msg_resp = requests.get(
                    f"{BOXNT_API_BASE}/mailboxes/me/messages",
                    headers={"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {tok}"},
                    timeout=10,
                )
                if msg_resp.status_code == 200:
                    for msg in (msg_resp.json().get("data") or []):
                        if msg.get("id"):
                            seen_ids.add(str(msg["id"]))
    except Exception as e:
        logger.debug(f"Lỗi khi khởi tạo hòm thư boxnt.com ({target_domain}): {e}")
    return seen_ids


def _fetch_code_from_boxnt(
    driver: WebDriver,
    prefix: str,
    domain: str = BOXNT_DOMAIN,
    seen_ids: Optional[set] = None,
    timeout: int = 60,
) -> Optional[str]:
    """
    Lấy mã xác nhận 6-7 số từ hệ thống boxnt.com (hỗ trợ @boxnt.com, @tempinbox.vn, @tmpmails.net).
    Kết hợp gọi API chính thức (/api/v1/mailboxes -> /api/v1/mailboxes/me/messages) và fallback mở web https://boxnt.com/.
    """
    seen_ids = seen_ids or set()
    username = prefix.strip().lower()
    target_domain = (domain or BOXNT_DOMAIN).strip().lower()
    full_email = f"{username}@{target_domain}"
    logger.info(f"Đang chờ mã xác nhận gửi về: {full_email} (trên {BOXNT_DOMAIN}) ...")

    # Bước 1: Lấy qua API v1 của boxnt.com (POST /api/v1/mailboxes -> GET /api/v1/mailboxes/me/messages)
    token = ""
    try:
        c_resp = requests.post(
            f"{BOXNT_API_BASE}/mailboxes",
            json={"localPart": username, "domain": target_domain},
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
            timeout=10,
        )
        if c_resp.status_code in (200, 201):
            token = (c_resp.json().get("data") or {}).get("token", "")
    except Exception as e:
        logger.debug(f"Lỗi tạo/mở mailbox trên API boxnt.com: {e}")

    start_time = time.time()
    while token and (time.time() - start_time < min(timeout, 45)):
        check_browser_alive(driver)
        try:
            resp = requests.get(
                f"{BOXNT_API_BASE}/mailboxes/me/messages",
                headers={"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                messages = resp.json().get("data") or []
                for msg in messages:
                    msg_id = str(msg.get("id", ""))
                    if not msg_id or msg_id in seen_ids:
                        continue
                    subject = str(msg.get("subject", "") or "")
                    snippet = str(msg.get("snippet", "") or "")
                    body_text = f"{subject} {snippet}"

                    # Lấy thêm nội dung chi tiết từ /api/v1/messages/{id}
                    try:
                        detail_resp = requests.get(
                            f"{BOXNT_API_BASE}/messages/{msg_id}",
                            headers={"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {token}"},
                            timeout=10,
                        )
                        if detail_resp.status_code == 200:
                            d_data = detail_resp.json().get("data") or {}
                            raw_html = str(d_data.get("html", "") or "")
                            raw_txt = str(d_data.get("text", "") or "")
                            body_text += " " + raw_txt + " " + re.sub(r"<[^>]+>", " ", raw_html)
                    except Exception:
                        pass

                    match = re.search(
                        r"(?:Security code|single-use code is|code is|mã bảo mật)[^0-9]*(\d{6,7})\b",
                        body_text,
                        re.IGNORECASE,
                    )
                    if not match:
                        match = re.search(r"\b(\d{6})\b", body_text)
                    if match:
                        code = match.group(1)
                        logger.info(f"✅ Đã lấy được mã xác nhận từ {target_domain} (API boxnt.com): {code}")
                        return code
        except Exception as e:
            logger.debug(f"Lỗi khi gọi API boxnt.com: {e}")

        time.sleep(3)

    # Bước 2: Fallback mở trực tiếp trang https://boxnt.com/ trên trình duyệt (như trong ảnh)
    logger.info(f"Mở trang {BOXNT_WEB_URL} trên trình duyệt để lấy mã cho {full_email}...")
    current_handle = driver.current_window_handle
    try:
        driver.execute_script(f"window.open('{BOXNT_WEB_URL}', '_blank');")
        time.sleep(2)
        driver.switch_to.window(driver.window_handles[-1])
        wait_for_page_load(driver, timeout=15)
        time.sleep(2.5)

        user_inp = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "[data-test='picker-localpart'] input, input[placeholder*='Tên người dùng'], input[type='text']",
            timeout=10,
            condition="visible",
        )
        if user_inp:
            safe_click(user_inp, delay=0.2)
            user_inp.send_keys(Keys.CONTROL + "a")
            user_inp.send_keys(Keys.BACKSPACE)
            safe_send_keys(user_inp, username, delay=0.5, clear_first=False)

            # Chọn chip domain tương ứng (@boxnt.com / @tempinbox.vn / @tmpmails.net)
            try:
                dom_chip = wait_for_element(
                    driver,
                    By.XPATH,
                    f"//*[@data-test='picker-domain-quick'][contains(normalize-space(), '{target_domain}')]",
                    timeout=3,
                    condition="clickable",
                )
                if dom_chip:
                    if not safe_click(dom_chip, delay=0.5):
                        driver.execute_script("arguments[0].click();", dom_chip)
                        time.sleep(0.5)
            except Exception:
                pass

            # Click nút '+ Tạo' (picker-submit)
            create_btn = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "[data-test='picker-submit']",
                timeout=4,
                condition="clickable",
            )
            if not create_btn:
                create_btn = wait_for_element(
                    driver,
                    By.XPATH,
                    "//button[contains(., 'Tạo') or contains(., 'Create')]",
                    timeout=3,
                    condition="clickable",
                )
            if create_btn:
                if not safe_click(create_btn, delay=2.5):
                    driver.execute_script("arguments[0].click();", create_btn)
                    time.sleep(2.5)

            # Chờ thư mới trong danh sách Hộp thư ([data-test='inbox-row'])
            for _ in range(8):
                mail_row = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "[data-test='inbox-row']",
                    timeout=4,
                    condition="clickable",
                )
                if mail_row:
                    if not safe_click(mail_row, delay=1.5):
                        driver.execute_script("arguments[0].click();", mail_row)
                        time.sleep(1.5)

                    content_str = ""
                    iframe_el = wait_for_element(
                        driver, By.CSS_SELECTOR, "iframe.message-frame, iframe", timeout=4, condition="presence"
                    )
                    if iframe_el:
                        content_str = iframe_el.get_attribute("srcdoc") or ""
                    if not content_str:
                        try:
                            content_str = driver.find_element(By.TAG_NAME, "body").text
                        except Exception:
                            content_str = ""

                    clean_str = re.sub(r"<[^>]+>", " ", content_str)
                    match = (
                        re.search(
                            r"(?:Security code|single-use code is|code is|mã bảo mật)[^0-9]*(\d{6,7})\b",
                            clean_str,
                            re.IGNORECASE,
                        )
                        or re.search(r"\b(\d{6})\b", clean_str)
                    )
                    if match:
                        code = match.group(1)
                        logger.info(f"✅ Đã lấy được mã trên web {BOXNT_DOMAIN} ({target_domain}): {code}")
                        return code
                time.sleep(3)
    except Exception as e:
        logger.warning(f"Lỗi khi lấy code trên web {BOXNT_DOMAIN}: {e}")
    finally:
        try:
            if driver.current_window_handle != current_handle and len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(current_handle)
        except Exception:
            pass

    logger.error(f"⏰ Hết thời gian chờ mã xác nhận cho {full_email}")
    return None


def _resolve_recovery_email(
    hotmail_email: str,
    recovery_email: str,
    body_text: str,
    preferred_domain: str = "",
    is_adding_new: bool = False,
) -> str:
    """
    Xác định email khôi phục cần điền:
    - Nếu đang ở màn hình 'Help protect your account' -> 'Add an email address' (is_adding_new=True, gắn mới email khôi phục),
      luôn dùng <prefix>@<preferred_domain> theo lựa chọn trên GUI (@fviainboxes.com hoặc @nguyenmaucole.io.vn).
    - Nếu trang Microsoft chỉ định rõ domain (@boxnt.com, @fviadropinbox.com, @fviamail.work, @fviainboxes.com, @nguyenmaucole.io.vn...) trên màn hình Verify, ưu tiên domain đó.
    - Nếu trong data Cột B có sẵn recovery_email, dùng recovery_email đó.
    - Ngược lại dùng <prefix>@<preferred_domain>.
    """
    prefix = hotmail_email.split("@")[0].strip().lower() if hotmail_email else ""
    pref_dom = get_preferred_recovery_domain(preferred_domain)

    if is_adding_new:
        return f"{prefix}@{pref_dom}"

    body_lower = (body_text or "").lower()

    for boxnt_dom in BOXNT_DOMAINS:
        if boxnt_dom in body_lower:
            if recovery_email and boxnt_dom in recovery_email.lower():
                return recovery_email.strip()
            return f"{prefix}@{boxnt_dom}"

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

    return f"{prefix}@{pref_dom}"


def _handle_add_recovery_email(
    driver: WebDriver,
    hotmail_email: str,
    recovery_email: str = "",
    fallback_pass: str = "",
    preferred_domain: str = "",
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

    # Ưu tiên số 0: Nếu màn hình là "Is your security info still accurate?" -> bấm "Looks good!"
    body_lower_init = body_text.lower()
    if (
        "proofs/remind" in (driver.current_url or "").lower()
        or "is your security info still accurate" in body_lower_init
        or "looks good!" in body_lower_init
    ):
        looks_good_btn = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "#iLooksGood, input#iLooksGood, button#iLooksGood, input[value*='Looks good']",
            timeout=3,
            condition="clickable",
        )
        if not looks_good_btn:
            looks_good_btn = wait_for_element(
                driver,
                By.XPATH,
                "//button[contains(., 'Looks good')] | //input[contains(@value, 'Looks good')] | //a[contains(., 'Looks good')]",
                timeout=2,
                condition="clickable",
            )
        if looks_good_btn:
            logger.info("Phát hiện màn hình 'Is your security info still accurate?' -> Bấm 'Looks good!'")
            if not safe_click(looks_good_btn, delay=2.5):
                driver.execute_script("arguments[0].click();", looks_good_btn)
                time.sleep(2.5)
            return True

    # Ưu tiên số 1: Nếu màn hình hiện nút "Use your password" -> bấm "Use your password"
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

    is_adding_new = "help protect your account" in body_lower_init or "add an email" in body_lower_init
    target_recovery_email = _resolve_recovery_email(
        hotmail_email, recovery_email, body_text, preferred_domain=preferred_domain, is_adding_new=is_adding_new
    )
    rec_prefix, rec_domain = target_recovery_email.split("@", 1)
    rec_prefix = rec_prefix.strip().lower()
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
            code = _fetch_code_from_fviainboxes(driver, rec_prefix, domain=rec_domain, seen_ids=set(), timeout=90)
        elif _is_boxnt_domain(rec_domain):
            code = _fetch_code_from_boxnt(driver, rec_prefix, domain=rec_domain, seen_ids=set(), timeout=90)
        else:
            code = _fetch_code_from_tempmail(driver, rec_prefix, min_id=0, timeout=90)
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

    # Bước 1: Nếu đang ở màn hình có nút "Add email" ('Help protect your account'), bấm "Add email"
    if "help protect your account" in body_lower_init:
        logger.info("Phát hiện màn hình 'Help protect your account' -> Bấm 'Add email'")
        add_btn_selectors = [
            (By.XPATH, "//button[contains(., 'Add email')]"),
            (By.CSS_SELECTOR, "button[data-testid='primaryButton'], button[type='submit'], #iLandingViewAction"),
        ]
        for by, val in add_btn_selectors:
            btn = wait_for_element(driver, by, val, timeout=3, condition="clickable")
            if btn:
                if not safe_click(btn, delay=2.5):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(2.5)
                logger.info("Đã click nút 'Add email'")
                is_adding_new = True
                break
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if "add an email" in body_text.lower():
                is_adding_new = True
            target_recovery_email = _resolve_recovery_email(
                hotmail_email, recovery_email, body_text, preferred_domain=preferred_domain, is_adding_new=is_adding_new
            )
            rec_prefix, rec_domain = target_recovery_email.split("@", 1)
            rec_prefix = rec_prefix.strip().lower()
            rec_domain = rec_domain.strip().lower()
        except Exception:
            pass

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
            target_recovery_email = _resolve_recovery_email(
                hotmail_email, recovery_email, body_text, preferred_domain=preferred_domain, is_adding_new=False
            )
            rec_prefix, rec_domain = target_recovery_email.split("@", 1)
            rec_prefix = rec_prefix.strip().lower()
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
            f" | //div[@role='button' or contains(@class, 'tile')][contains(., '{TEMPMAIL_DOMAIN}') or contains(., 'fvia') or contains(., 'boxnt') or contains(., 'tempinbox') or contains(., 'tmpmails') or contains(., 'Email ')]",
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
                target_recovery_email = _resolve_recovery_email(
                    hotmail_email, recovery_email, body_text, preferred_domain=preferred_domain, is_adding_new=False
                )
                rec_prefix, rec_domain = target_recovery_email.split("@", 1)
                rec_prefix = rec_prefix.strip().lower()
                rec_domain = rec_domain.strip().lower()
            except Exception:
                pass

    # Bước 2: Chờ màn hình nhập địa chỉ email khôi phục (hoặc nếu đã chuyển thẳng sang ô nhập 6 số code)
    time.sleep(1)
    try:
        body_now = (driver.find_element(By.TAG_NAME, "body").text or "")
        if "add an email" in body_now.lower():
            is_adding_new = True
            target_recovery_email = _resolve_recovery_email(
                hotmail_email, recovery_email, body_now, preferred_domain=preferred_domain, is_adding_new=True
            )
            rec_prefix, rec_domain = target_recovery_email.split("@", 1)
            rec_prefix = rec_prefix.strip().lower()
            rec_domain = rec_domain.strip().lower()
    except Exception:
        pass

    min_mail_id = 0
    fvia_seen_ids = set()
    boxnt_seen_ids = set()
    if _is_fvia_domain(rec_domain):
        fvia_seen_ids = _get_existing_fviainboxes_ids(rec_prefix, domain=rec_domain)
    elif _is_boxnt_domain(rec_domain):
        boxnt_seen_ids = _get_existing_boxnt_ids(rec_prefix, domain=rec_domain)
    else:
        min_mail_id = _get_latest_tempmail_id(rec_prefix)

    email_inp = wait_for_element(
        driver,
        By.CSS_SELECTOR,
        "#iProofEmail, #proof-confirmation-email-input, input[type='email'], #EmailAddress, input[id^='floatingLabelInput']:not([id^='codeEntry'])",
        timeout=4,
        condition="visible",
    )

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

        safe_click(email_inp, delay=0.2)
        safe_send_keys(email_inp, value_to_type, delay=0.8)

        next_btn_selectors = [
            (By.CSS_SELECTOR, "button[data-testid='primaryButton'], #iSelectProofAction, #iNext, button[type='submit'], input[type='submit']"),
            (By.XPATH, "//button[contains(., 'Next') or contains(., 'Send code') or contains(., 'Gửi mã')]"),
        ]
        clicked_next = False
        for by, val in next_btn_selectors:
            next_btn = wait_for_element(driver, by, val, timeout=3, condition="clickable")
            if next_btn:
                if not safe_click(next_btn, delay=2.5):
                    driver.execute_script("arguments[0].click();", next_btn)
                    time.sleep(2.5)
                logger.info("Đã click 'Next'/'Send code' để gửi mã xác nhận về email khôi phục")
                clicked_next = True
                break
        if not clicked_next:
            try:
                email_inp.send_keys(Keys.RETURN)
                time.sleep(2.5)
            except Exception:
                pass

        # Đảm bảo trang đã chuyển khỏi màn hình 'Add an email address' sang màn hình nhập code
        driver.implicitly_wait(0)
        try:
            for retry_submit in range(6):
                otp_ready = any(
                    el.is_displayed()
                    for el in driver.find_elements(
                        By.CSS_SELECTOR,
                        "#codeEntry-0, #iOttText, input[name='otc'], input[name='ottCode'], input[autocomplete='one-time-code']",
                    )
                )
                if otp_ready:
                    break
                b_after = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
                if "add an email address" not in b_after and "enter your email" not in b_after:
                    break
                if retry_submit == 2:
                    btns_retry = [
                        b for b in driver.find_elements(
                            By.CSS_SELECTOR,
                            "button[data-testid='primaryButton'], button[type='submit'], #iSelectProofAction, #iNext",
                        )
                        if b.is_displayed()
                    ]
                    if btns_retry:
                        driver.execute_script("arguments[0].click();", btns_retry[0])
                    else:
                        try:
                            email_inp.send_keys(Keys.RETURN)
                        except Exception:
                            pass
                time.sleep(1.0)
        except Exception:
            pass
        finally:
            driver.implicitly_wait(5)
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
        boxnt_seen_ids = set()
        min_mail_id = 0

    # Bước 3: Chờ ô nhập code 6 số xuất hiện (6 ô codeEntry-0..5 hoặc 1 ô đơn #iOttText / ottCode) — tuyệt đối không bắt nhầm input[type='email']
    code_el = wait_for_element(
        driver,
        By.CSS_SELECTOR,
        "#codeEntry-0, #iOttText, input[name='otc'], input[name='ottCode'], input[autocomplete='one-time-code'], input[type='tel'], input[type='number']",
        timeout=12,
        condition="visible",
    )
    if not code_el:
        code_el = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "input[type='text']:not([type='email']):not([name='loginfmt'])",
            timeout=3,
            condition="visible",
        )
        if code_el and "@" in (code_el.get_attribute("value") or ""):
            code_el = None
    if not code_el:
        logger.error("Không tìm thấy ô nhập mã xác nhận trên trang Microsoft")
        return False

    is_six_boxes = (code_el.get_attribute("id") or "") == "codeEntry-0"

    # Lấy code từ fviainboxes.com, boxnt.com, hoặc mail.nguyenmaucole.io.vn
    if _is_fvia_domain(rec_domain):
        code = _fetch_code_from_fviainboxes(driver, rec_prefix, domain=rec_domain, seen_ids=fvia_seen_ids, timeout=90)
    elif _is_boxnt_domain(rec_domain):
        code = _fetch_code_from_boxnt(driver, rec_prefix, domain=rec_domain, seen_ids=boxnt_seen_ids, timeout=90)
    else:
        code = _fetch_code_from_tempmail(driver, rec_prefix, min_id=min_mail_id, timeout=90)

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
    current_password: str = "", preferred_domain: str = "",
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

            # 1d. Nếu ở trang "Is your security info still accurate?" (proofs/Remind) -> bấm "Looks good!" (#iLooksGood)
            if (
                "proofs/remind" in current_url
                or "is your security info still accurate" in body_lower
                or "looks good!" in body_lower
            ):
                looks_good_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "#iLooksGood, input#iLooksGood, button#iLooksGood, input[value*='Looks good']",
                    timeout=3,
                    condition="clickable",
                )
                if not looks_good_btn:
                    looks_good_btn = wait_for_element(
                        driver,
                        By.XPATH,
                        "//button[contains(., 'Looks good')] | //input[contains(@value, 'Looks good')] | //a[contains(., 'Looks good')]",
                        timeout=2,
                        condition="clickable",
                    )
                if looks_good_btn:
                    logger.info("Phát hiện màn hình 'Is your security info still accurate?' -> Bấm 'Looks good!'")
                    if not safe_click(looks_good_btn, delay=2.5):
                        driver.execute_script("arguments[0].click();", looks_good_btn)
                        time.sleep(2.5)
                    continue

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
                if not _handle_add_recovery_email(
                    driver, hotmail_email, recovery_email, fallback_pass=primary_pwd, preferred_domain=preferred_domain
                ):
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
                if not _handle_add_recovery_email(
                    driver, hotmail_email, recovery_email, fallback_pass=primary_pwd, preferred_domain=preferred_domain
                ):
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
                "//button[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận' or normalize-space()='Đồng ý' "
                "or .//*[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận' or normalize-space()='Đồng ý']] | "
                "//*[@role='button'][normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận' or normalize-space()='Đồng ý']"
            )
            try:
                accept_btns = [b for b in driver.find_elements(By.XPATH, accept_xp) if b.is_displayed()]
                if accept_btns:
                    btn = accept_btns[-1]
                    if not safe_click(btn, delay=1.5):
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.5)
                    logger.info("✅ Đã bấm 'Accept' trên hộp thoại quyền riêng tư Outlook")
                    dismissed = True
                    continue
            except Exception:
                pass

            # 2. Kiểm tra tất cả iframe hiển thị trên trang (bao gồm privacynotice / consent / dialog iframe của Outlook)
            try:
                iframes = [
                    f for f in driver.find_elements(By.TAG_NAME, "iframe")
                    if f.is_displayed()
                ]
                for frame in iframes:
                    try:
                        driver.switch_to.frame(frame)
                        btns = driver.find_elements(
                            By.XPATH,
                            "//button[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='OK' "
                            "or normalize-space()='Next' or normalize-space()='Continue' or normalize-space()='Chấp nhận' or normalize-space()='Đồng ý' "
                            "or .//*[normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận' or normalize-space()='Đồng ý']] | "
                            "//*[@role='button'][normalize-space()='Accept' or normalize-space()='Accept all' or normalize-space()='Chấp nhận' or normalize-space()='Đồng ý']",
                        )
                        for b in btns:
                            if b.is_displayed():
                                if not safe_click(b, delay=1.5):
                                    driver.execute_script("arguments[0].click();", b)
                                    time.sleep(1.5)
                                dismissed = True
                                logger.info("✅ Đã bấm 'Accept/OK' đóng hộp thoại Privacy/Consent trong iframe Outlook")
                                break
                    except Exception:
                        pass
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
            driver, By.CSS_SELECTOR, "input[type='password'], #i0118, #passwordEntry, input[name='passwd']",
            timeout=2, condition="visible",
        )

    if pass_input:
        # Ưu tiên mật khẩu mới ('Khoa01@@c' / new_hotmail_pass), nếu không đúng thì dùng mật khẩu trong data Google Sheet (old_hotmail_pass)
        candidates = [
            new_hotmail_pass,
            "Khoa01@@c",
            "Khoa01@c",
            old_hotmail_pass,
        ]
        _submit_password_with_fallbacks(driver, candidates)

    # Kiểm tra cả TRƯỚC và SAU khi nhập mật khẩu: nếu popup hiện 'Looks good!' hoặc 'Almost there' / 'Send a code to ...'
    try:
        if not popup_handle or popup_handle in driver.window_handles:
            body_lower_popup = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
            cur_u_popup = (driver.current_url or "").lower()
            if (
                "proofs/remind" in cur_u_popup
                or "is your security info still accurate" in body_lower_popup
                or "looks good!" in body_lower_popup
            ):
                looks_good_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "#iLooksGood, input#iLooksGood, button#iLooksGood, input[value*='Looks good']",
                    timeout=3,
                    condition="clickable",
                )
                if not looks_good_btn:
                    looks_good_btn = wait_for_element(
                        driver,
                        By.XPATH,
                        "//button[contains(., 'Looks good')] | //input[contains(@value, 'Looks good')] | //a[contains(., 'Looks good')]",
                        timeout=2,
                        condition="clickable",
                    )
                if looks_good_btn:
                    logger.info("Popup Forwarding hiện 'Is your security info still accurate?' -> Bấm 'Looks good!'")
                    if not safe_click(looks_good_btn, delay=2.5):
                        driver.execute_script("arguments[0].click();", looks_good_btn)
                        time.sleep(2.5)
            elif (
                "almost there" in body_lower_popup
                or "just one more step" in body_lower_popup
                or "send a code to" in body_lower_popup
                or "verify your email" in body_lower_popup
                or "verify your identity" in body_lower_popup
                or "help us protect your account" in body_lower_popup
                or "identity/confirm" in cur_u_popup
            ):
                logger.info("Popup xác thực Forwarding yêu cầu xác minh email khôi phục ('Almost there' / 'Send a code to')...")
                _handle_add_recovery_email(driver, hotmail_email, recovery_email, fallback_pass=old_hotmail_pass or new_hotmail_pass)
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
                "input[type='email'], #i0116, #usernameEntry, input[name='loginfmt'], input[type='password'], #i0118, #passwordEntry, input[name='passwd']",
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
    3. Nếu chưa login -> đăng nhập bằng tài khoản Hotmail:
       - Thử mật khẩu mới ('Khoa01@@c' / 'Khoa01@c' / new_hotmail_pass).
       - Nếu không đúng -> tự động dùng mật khẩu trong data Google Sheet (old_hotmail_pass).
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

    if "microsoft.com/" in (driver.current_url or "").lower() and "login.live.com" not in (driver.current_url or "").lower():
        _handle_microsoft_outlook_landing_page(driver)

    if _is_hotmail_already_logged_in(driver):
        _dismiss_outlook_popups(driver)
        logger.info("✅ Hotmail đã đăng nhập sẵn -> Chuyển thẳng sang Bước 3 (Add mail forwarding)!")
        return True

    primary_new_pass = new_hotmail_pass or getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c")
    logger.info(
        f"ℹ️ Hotmail chưa đăng nhập (URL: {driver.current_url[:65]}) "
        f"-> Đăng nhập tài khoản {hotmail_email} (ưu tiên '{primary_new_pass}', nếu sai dùng pass Google Sheet)..."
    )

    cur_u = (driver.current_url or "").lower()
    driver.implicitly_wait(0)
    try:
        has_login_ui = bool(
            driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='email'], #i0116, #usernameEntry, input[name='loginfmt'], input[type='password'], #i0118, #passwordEntry, input[name='passwd']",
            )
        )
    except Exception:
        has_login_ui = False
    finally:
        driver.implicitly_wait(5)

    if "login.live.com" not in cur_u and not has_login_ui:
        driver.get("https://outlook.live.com/owa/?nlp=1")
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2)
        if "microsoft.com/" in (driver.current_url or "").lower() and "login.live.com" not in (driver.current_url or "").lower():
            _handle_microsoft_outlook_landing_page(driver)
            if _is_hotmail_already_logged_in(driver):
                logger.info("✅ Hotmail đã đăng nhập sẵn sau khi bấm Sign in!")
                return True

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
        driver.implicitly_wait(0)
        try:
            pwd_visible = any(
                el.is_displayed()
                for el in driver.find_elements(
                    By.CSS_SELECTOR, "input[type='password'], #i0118, #passwordEntry, input[name='passwd']"
                )
            )
        except Exception:
            pwd_visible = False
        finally:
            driver.implicitly_wait(5)

        if not pwd_visible:
            _enter_email(driver, hotmail_email)

    # Nhập mật khẩu: thử primary_new_pass ('Khoa01@@c'), nếu không đúng tự động dùng old_hotmail_pass (trong Google Sheet)
    _enter_password(
        driver,
        primary_new_pass,
        hotmail_email,
        recovery_email,
        fallback_password=old_hotmail_pass,
    )

    handle_microsoft_prompts(driver, hotmail_email, recovery_email, current_password=old_hotmail_pass or primary_new_pass)
    time.sleep(2)
    logger.info("✅ Đã hoàn tất đăng nhập lại Hotmail, chuyển sang Bước 3!")
    return True


def setup_hotmail_forwarding(
    driver: WebDriver,
    hotmail_email: str,
    recovery_email: str = "",
    new_hotmail_pass: str = "",
    old_hotmail_pass: str = "",
    target_domain: str = TEMPMAIL_DOMAIN,
) -> str:
    """
    Trước khi vào cài đặt Forwarding:
    - Kiểm tra trang Hotmail đã login chưa; nếu chưa thì đăng nhập bằng mật khẩu mới ('Khoa01@c' / new_hotmail_pass).
    Sau đó truy cập https://outlook.live.com/mail/options/mail/forwarding,
    đóng popup lần đầu (nếu có), xử lý nút 'Sign in' (nếu có),
    bật 'Enable forwarding', điền <prefix>@<target_domain> (nguyenmaucole.io.vn hoặc fviainboxes.com) và bấm 'Save'.
    Trả về địa chỉ email forwarding nếu thành công, ngược lại trả về chuỗi rỗng "".
    """
    prefix = hotmail_email.split("@")[0].strip().lower()
    target_dom = (target_domain or TEMPMAIL_DOMAIN).strip().lstrip("@").lower()
    forward_email = f"{prefix}@{target_dom}"
    forwarding_url = "https://outlook.live.com/mail/options/mail/forwarding"
    if not new_hotmail_pass:
        new_hotmail_pass = getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@c")

    logger.info(f"=== BƯỚC 4: Cài đặt Forwarding ({forwarding_url} -> {forward_email}) ===")
    try:
        # Kiểm tra đã login Hotmail chưa; nếu chưa thì login bằng mật khẩu mới trước khi vào trang Forwarding
        _ensure_hotmail_logged_in_before_forwarding(
            driver, hotmail_email, new_hotmail_pass, old_hotmail_pass, recovery_email
        )

        driver.get(forwarding_url)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(4)

        cur_u = (driver.current_url or "").lower()
        if "microsoft.com/" in cur_u and "login.live.com" not in cur_u:
            _handle_microsoft_outlook_landing_page(driver)
            cur_u = (driver.current_url or "").lower()

        if "login." in cur_u or "account.live.com" in cur_u or "privacynotice" in cur_u:
            handle_microsoft_prompts(driver, hotmail_email, recovery_email, current_password=old_hotmail_pass)
            if "outlook.live.com/mail/options/mail/forwarding" not in (driver.current_url or "").lower():
                driver.get(forwarding_url)
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(4)
        elif "outlook.live.com/mail/options/mail/forwarding" not in cur_u:
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

        # Đóng hộp thoại quyền riêng tư ('Accept') nếu xuất hiện trễ sau khi đăng nhập
        _dismiss_outlook_popups(driver)

        # Kiểm tra công tắc switch đã bật chưa
        switches = driver.find_elements(By.CSS_SELECTOR, "[role='dialog'] input[role='switch'], input[role='switch']")
        is_enabled = False
        if switches:
            sw = switches[0]
            is_enabled = bool(sw.is_selected() or sw.get_attribute("checked") == "true" or sw.get_attribute("aria-checked") == "true")

        if not is_enabled:
            if not safe_click(enable_lbl, delay=1.5):
                _dismiss_outlook_popups(driver)
                if not safe_click(enable_lbl, delay=1.5):
                    driver.execute_script("arguments[0].click();", enable_lbl)
                    time.sleep(1.5)
            logger.info("Đã bật 'Enable forwarding'")
        else:
            logger.info("'Enable forwarding' đã được bật sẵn")

        _dismiss_outlook_popups(driver)

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

        # Kiểm tra giá trị hiện tại trong ô Forwarding; nếu chưa đúng thì xóa sạch và điền forward_email mới
        current_fwd_val = (fwd_input.get_attribute("value") or "").strip().lower()
        if current_fwd_val != forward_email.lower():
            if not safe_click(fwd_input, delay=0.3):
                _dismiss_outlook_popups(driver)
                safe_click(fwd_input, delay=0.3)
            fwd_input.send_keys(Keys.CONTROL + "a")
            fwd_input.send_keys(Keys.BACKSPACE)
            if (fwd_input.get_attribute("value") or "").strip():
                try:
                    fwd_input.clear()
                except Exception:
                    pass
            safe_send_keys(fwd_input, forward_email, delay=1.0, clear_first=False)
            logger.info(f"Đã điền email chuyển tiếp: {forward_email}")
        else:
            logger.info(f"Ô Forwarding đã có sẵn địa chỉ đúng: {forward_email}")

        _dismiss_outlook_popups(driver)

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
                    if not safe_click(keep_lbl, delay=0.5):
                        _dismiss_outlook_popups(driver)
                        safe_click(keep_lbl, delay=0.5)
                    logger.info("Đã tích 'Keep a copy of forwarded messages'")
        except Exception:
            pass

        # Đóng hộp thoại quyền riêng tư ('Accept') nếu xuất hiện trước khi bấm Save
        _dismiss_outlook_popups(driver)

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
                # Nếu bị che bởi hộp thoại quyền riêng tư mới hiện lên -> bấm 'Accept' rồi bấm lại 'Save'
                _dismiss_outlook_popups(driver)
                if not safe_click(save_btn, delay=3.0):
                    driver.execute_script("arguments[0].click();", save_btn)
                    time.sleep(3.0)
            _dismiss_outlook_popups(driver)
            logger.info("✅ Đã bấm 'Save' lưu cài đặt Forwarding thành công!")
        else:
            logger.info("Cài đặt Forwarding đã được lưu trước đó (không cần bấm Save)")

        if _is_fvia_domain(target_dom):
            try:
                _get_existing_fviainboxes_ids(prefix, domain=target_dom)
            except Exception:
                pass

        return forward_email
    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi khi cài đặt Forwarding Hotmail: {e}")
        return ""


def replace_hotmail_recovery_email(
    driver: WebDriver,
    hotmail_email: str,
    recovery_email: str = "",
    current_password: str = "",
    new_hotmail_pass: str = "",
    target_domain: str = TEMPMAIL_DOMAIN,
) -> bool:
    """
    Thay đổi email khôi phục của Hotmail sang <prefix>@<target_domain>
    (hỗ trợ cả @nguyenmaucole.io.vn và @fviainboxes.com) tại
    https://account.live.com/proofs/Manage/additional:
      1. Nếu email khôi phục hiện tại đã đúng là <prefix>@<target_domain> (và không còn mail cũ khác) -> bỏ qua ngay.
      2. Thêm email khôi phục mới <prefix>@<target_domain> trước (nhận mã 6 số qua API tương ứng).
      3. Xóa các email khôi phục cũ khác sau.
    """
    if not hotmail_email or "@" not in hotmail_email:
        logger.error("Thiếu địa chỉ Hotmail để thay email khôi phục")
        return False

    prefix = hotmail_email.split("@")[0].strip().lower()
    target_dom = get_preferred_recovery_domain(target_domain)
    set_preferred_recovery_domain(target_dom)
    new_rec_email = f"{prefix}@{target_dom}"

    if recovery_email and recovery_email.strip().lower() == new_rec_email:
        logger.info(
            f"ℹ️ Email khôi phục trong dữ liệu đã đúng là {new_rec_email} -> Bỏ qua bước thay mail khôi phục!"
        )
        return True

    logger.info(f"=== Thay email khôi phục Hotmail sang {new_rec_email} ===")
    manage_url = "https://account.live.com/proofs/Manage/additional"

    try:
        driver.get(manage_url)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2)

        # Vòng lặp xử lý xác thực lại mật khẩu / OTP nếu Microsoft yêu cầu trước khi vào proofs/Manage/additional
        pwd_candidates = [
            new_hotmail_pass,
            getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c"),
            "Khoa01@@c",
            "Khoa01@c",
            current_password,
        ]
        for _ in range(4):
            check_browser_alive(driver)
            cur_u = (driver.current_url or "").lower()
            if "proofs/manage" in cur_u:
                manage_els = driver.find_elements(
                    By.CSS_SELECTOR, "#ManageProofs, #AddProofLink, div[data-mp-proof-type='email']"
                )
                if any(el.is_displayed() for el in manage_els):
                    break

            # Nếu màn hình xác minh hiện 'Send a code to ...@<target_dom>' mà không có recovery_email cũ
            try:
                body_txt_check = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
                other_known_domains = [
                    d
                    for d in (
                        TEMPMAIL_DOMAIN,
                        FVIA_DOMAIN,
                        "fviadropinbox.com",
                        "fviamail.work",
                        "dropinboxes.com",
                        BOXNT_DOMAIN,
                        "tempinbox.vn",
                        "tmpmails.net",
                    )
                    if d != target_dom
                ]
                if (
                    f"@{target_dom}" in body_txt_check
                    and not recovery_email
                    and not any(d in body_txt_check for d in other_known_domains)
                ):
                    logger.info(
                        f"ℹ️ Phát hiện tài khoản đã dùng sẵn email khôi phục @{target_dom} -> Bỏ qua bước thay mail khôi phục!"
                    )
                    return True
            except Exception:
                pass

            pwd_inputs = [
                el for el in driver.find_elements(
                    By.CSS_SELECTOR, "input[type='password'], #i0118, #passwordEntry, input[name='passwd']"
                )
                if el.is_displayed()
            ]
            if pwd_inputs:
                _submit_password_with_fallbacks(driver, pwd_candidates)
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(2)
                continue

            handle_microsoft_prompts(
                driver,
                hotmail_email,
                recovery_email,
                current_password=new_hotmail_pass or current_password,
                preferred_domain=target_dom,
            )
            if "proofs/manage" not in (driver.current_url or "").lower():
                driver.get(manage_url)
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(2)

        wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "#ManageProofs, #AddProofLink, div[data-mp-proof-type='email']",
            timeout=10,
            condition="presence",
        )
        time.sleep(1.5)

        rec_low = (recovery_email or "").strip().lower()

        def _is_matching_target_proof(proof_text: str) -> bool:
            t_low = (proof_text or "").lower()
            if new_rec_email in t_low:
                return True
            if f"@{target_dom}" in t_low:
                # Nếu proof chứa đúng địa chỉ recovery_email cũ khác prefix (vd: random999@fviainboxes.com) thì coi là mail cũ cần thay
                if rec_low and rec_low != new_rec_email and rec_low in t_low:
                    return False
                return True
            return False

        email_proofs = driver.find_elements(By.CSS_SELECTOR, "div[data-mp-proof-type='email']")
        active_email_proofs = [
            el for el in email_proofs
            if (el.get_attribute("data-pending-deletion") or "0") != "1"
        ]
        has_target_rec = any(
            _is_matching_target_proof(el.get_attribute("textContent") or "")
            for el in active_email_proofs
        )
        old_proofs = [
            el for el in active_email_proofs
            if not _is_matching_target_proof(el.get_attribute("textContent") or "")
        ]

        if has_target_rec and not old_proofs:
            logger.info(
                f"ℹ️ Email khôi phục hiện tại trên Microsoft đã là @{target_dom} -> Bỏ qua sang bước tiếp theo!"
            )
            return True

        # Bước A: Thêm email khôi phục mới <prefix>@<target_dom> (nếu chưa có)
        if not has_target_rec:
            logger.info(f"Đang thêm email khôi phục mới: {new_rec_email}...")
            add_link = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#AddProofLink, a[data-mp-control='AddProofItem.AddProofLink']",
                timeout=8,
                condition="clickable",
            )
            if not add_link:
                add_link = wait_for_element(
                    driver,
                    By.XPATH,
                    "//a[contains(., 'Add another way') or contains(., 'Add a new way') or contains(., 'Thêm cách')]"
                    " | //button[contains(., 'Add another way') or contains(., 'Add a new way') or contains(., 'Thêm cách')]",
                    timeout=5,
                    condition="clickable",
                )
            if not add_link:
                logger.error("Không tìm thấy nút 'Add another way to sign in to your account' (#AddProofLink)")
                return False

            if not safe_click(add_link, delay=2.0):
                driver.execute_script("arguments[0].click();", add_link)
                time.sleep(2.0)

            # Chọn 'Email a code' (#Add_email) trong modal AddProofModal
            add_email_opt = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#Add_email, a[data-mp-proof-type='email']",
                timeout=5,
                condition="clickable",
            )
            if not add_email_opt:
                # Nếu có nút 'Show more options' thì bấm trước
                more_btn = wait_for_element(
                    driver,
                    By.XPATH,
                    "//button[contains(., 'Show more') or contains(., 'Hiển thị thêm')] | //a[contains(., 'Show more') or contains(., 'Hiển thị thêm')]",
                    timeout=2,
                    condition="clickable",
                )
                if more_btn:
                    if not safe_click(more_btn, delay=1.5):
                        driver.execute_script("arguments[0].click();", more_btn)
                        time.sleep(1.5)
                add_email_opt = wait_for_element(
                    driver,
                    By.XPATH,
                    "//a[@id='Add_email' or @data-mp-proof-type='email' or contains(., 'Email a code') or contains(., 'Gửi mã qua email')]",
                    timeout=5,
                    condition="clickable",
                )

            if not add_email_opt:
                logger.error("Không tìm thấy tùy chọn 'Email a code' (#Add_email) trong hộp thoại")
                return False

            if not safe_click(add_email_opt, delay=2.0):
                driver.execute_script("arguments[0].click();", add_email_opt)
                time.sleep(2.0)

            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
            time.sleep(1.5)

            # Nhập địa chỉ email mới vào ô #EmailAddress (trang proofs/Add?apt=2) hoặc #email (AddEmailProofModal)
            email_inp = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#EmailAddress, #iProofEmail, input[name='EmailAddress'], div[data-mp-dialog='AddEmailProofModal'] input#email, input#email, input[type='email']",
                timeout=10,
                condition="visible",
            )
            if not email_inp:
                logger.error("Không tìm thấy ô nhập email khôi phục mới (#EmailAddress / #email)")
                return False

            min_mail_id = 0
            fvia_seen_ids = set()
            if _is_fvia_domain(target_dom):
                fvia_seen_ids = _get_existing_fviainboxes_ids(prefix, domain=target_dom)
            else:
                min_mail_id = _get_latest_tempmail_id(prefix)
            safe_click(email_inp, delay=0.3)
            email_inp.clear()
            safe_send_keys(email_inp, new_rec_email, delay=0.8)
            try:
                driver.execute_script(
                    "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true }));",
                    email_inp,
                    new_rec_email,
                )
            except Exception:
                pass
            logger.info(f"Đã điền email khôi phục mới: {new_rec_email}")

            next_btn = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#iNext, [data-mp-control='AddEmailProofModal.PrimaryOnNext'], div[data-mp-dialog='AddEmailProofModal'] #iBtn_action, input[type='submit'], button[type='submit']",
                timeout=5,
                condition="clickable",
            )
            if not next_btn:
                logger.error("Không tìm thấy nút Next khi thêm email khôi phục mới")
                return False

            if not safe_click(next_btn, delay=2.5):
                driver.execute_script("arguments[0].click();", next_btn)
                time.sleep(2.5)

            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
            time.sleep(1.5)

            # Chờ ô nhập mã xác minh #iOttText (trang proofs/Add) hoặc #ottCode (AddAddressableProofModal)
            ott_inp = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#iOttText, input[name='iOttText'], div[data-mp-dialog='AddAddressableProofModal'] input#ottCode, input#ottCode, input[name='ottCode'], input[name='otc'], input[type='tel']",
                timeout=10,
                condition="visible",
            )
            if not ott_inp:
                logger.error("Không tìm thấy ô nhập mã OTP (#iOttText / #ottCode) khi thêm email khôi phục mới")
                return False

            if _is_fvia_domain(target_dom):
                code = _fetch_code_from_fviainboxes(
                    driver, prefix, domain=target_dom, seen_ids=fvia_seen_ids, timeout=90
                )
            else:
                code = _fetch_code_from_tempmail(driver, prefix, min_id=min_mail_id, timeout=90)
            if not code:
                logger.error(f"Không lấy được mã xác nhận từ {new_rec_email}")
                return False

            safe_click(ott_inp, delay=0.3)
            ott_inp.clear()
            safe_send_keys(ott_inp, code, delay=0.5)
            try:
                driver.execute_script(
                    "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true }));",
                    ott_inp,
                    code,
                )
            except Exception:
                pass
            logger.info(f"Đã điền mã OTP xác nhận email khôi phục mới: {code}")

            verify_btn = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#iNext, [data-mp-control='AddAddressableProofModal.PrimaryAddProof'], div[data-mp-dialog='AddAddressableProofModal'] #iBtn_action, input[type='submit'], button[type='submit']",
                timeout=5,
                condition="clickable",
            )
            if verify_btn:
                if not safe_click(verify_btn, delay=3.5):
                    driver.execute_script("arguments[0].click();", verify_btn)
                    time.sleep(3.5)

            logger.info(f"✅ Đã thêm thành công email khôi phục mới: {new_rec_email}")
            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
            time.sleep(2)

            # Đảm bảo quay về trang Manage/additional để tiếp tục Bước B (xóa email khôi phục cũ)
            if "proofs/manage" not in (driver.current_url or "").lower():
                driver.get(manage_url)
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(2)
            wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#ManageProofs, div[data-mp-proof-type='email']",
                timeout=10,
                condition="presence",
            )

        # Bước B: Xóa các email khôi phục cũ (không khớp <prefix>@<target_dom>)
        for _ in range(5):
            email_proofs = driver.find_elements(By.CSS_SELECTOR, "div[data-mp-proof-type='email']")
            target_old_el = None
            old_email_text = ""
            for el in email_proofs:
                if (el.get_attribute("data-pending-deletion") or "0") == "1":
                    continue
                txt = (el.get_attribute("textContent") or "").strip()
                if not _is_matching_target_proof(txt):
                    target_old_el = el
                    try:
                        spans = el.find_elements(By.CSS_SELECTOR, ".pullout-textwrap span, .pullout-textwrap")
                        for sp in spans:
                            sp_txt = (sp.get_attribute("textContent") or "").strip()
                            if "@" in sp_txt:
                                old_email_text = sp_txt
                                break
                    except Exception:
                        pass
                    if not old_email_text:
                        old_email_text = "email khôi phục cũ"
                    break

            if not target_old_el:
                break

            logger.info(f"Đang xóa email khôi phục cũ: {old_email_text}...")
            try:
                rem_btns = [
                    b for b in target_old_el.find_elements(
                        By.CSS_SELECTOR, "button[data-mp-control='ProofPullout.Remove'], button#Remove"
                    )
                    if b.is_displayed()
                ]
                if not rem_btns:
                    toggle_link = target_old_el.find_element(
                        By.CSS_SELECTOR, "a[data-mp-control='Pullout.Toggle'], a.pullout-link"
                    )
                    if not safe_click(toggle_link, delay=1.5):
                        driver.execute_script("arguments[0].click();", toggle_link)
                        time.sleep(1.5)
                    rem_btns = [
                        b for b in target_old_el.find_elements(
                            By.CSS_SELECTOR, "button[data-mp-control='ProofPullout.Remove'], button#Remove"
                        )
                        if b.is_displayed()
                    ]

                if not rem_btns:
                    logger.warning(f"Không tìm thấy nút Remove cho {old_email_text}")
                    break

                if not safe_click(rem_btns[0], delay=2.0):
                    driver.execute_script("arguments[0].click();", rem_btns[0])
                    time.sleep(2.0)

                # Nếu modal RemoveProofModal có checkbox #RemoveAcknowledgement thì tích vào
                try:
                    ack_cbs = [
                        cb for cb in driver.find_elements(
                            By.CSS_SELECTOR, "#RemoveAcknowledgement, input[data-mp-control='RemoveProofModal.Acknowledgement']"
                        )
                        if cb.is_displayed()
                    ]
                    if ack_cbs and not ack_cbs[0].is_selected():
                        if not safe_click(ack_cbs[0], delay=0.5):
                            driver.execute_script("arguments[0].click();", ack_cbs[0])
                            time.sleep(0.5)
                except Exception:
                    pass

                confirm_del_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "[data-mp-control='RemoveProofModal.PrimaryDeleteProof'], div[data-mp-dialog='RemoveProofModal'] #iBtn_action, .modal button#iBtn_action",
                    timeout=5,
                    condition="clickable",
                )
                if confirm_del_btn:
                    if not safe_click(confirm_del_btn, delay=3.0):
                        driver.execute_script("arguments[0].click();", confirm_del_btn)
                        time.sleep(3.0)

                # Đóng modal thông báo đã xóa (nếu hiện nút Close/OK)
                try:
                    close_success_btns = [
                        b for b in driver.find_elements(
                            By.CSS_SELECTOR,
                            "[data-mp-control='RemoveProofModal.PrimaryCloseSuccessModal'], [data-mp-control='RemoveProofModal.PrimaryCloseModal']",
                        )
                        if b.is_displayed()
                    ]
                    if close_success_btns:
                        if not safe_click(close_success_btns[0], delay=1.5):
                            driver.execute_script("arguments[0].click();", close_success_btns[0])
                            time.sleep(1.5)
                except Exception:
                    pass

                logger.info(f"✅ Đã xóa email khôi phục cũ: {old_email_text}")
                time.sleep(1.5)
            except Exception as rem_err:
                logger.warning(f"Lỗi khi xóa email khôi phục cũ ({old_email_text}): {rem_err}")
                break

        return True

    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi khi thay email khôi phục Hotmail: {e}")
        return False


def login_hotmail(
    driver: WebDriver, hotmail_email: str, hotmail_password: str,
    recovery_email: str = "", new_hotmail_pass: str = "",
    target_domain: str = "",
) -> bool:
    """
    Đăng nhập Hotmail trên Tab 2.
    CHỈ chạy khi Facebook login (Bước 1) thành công.

    Returns:
        True nếu đăng nhập thành công.
    """
    if target_domain:
        set_preferred_recovery_domain(target_domain)
    logger.info(f"=== BƯỚC 3: Đăng nhập Hotmail (Email: {hotmail_email}) ===")

    try:
        # Mở Tab 2 trực tiếp tới trang đăng nhập Outlook Web App (nlp=1 bỏ qua trang giới thiệu Microsoft 365)
        login_url = "https://outlook.live.com/owa/?nlp=1"
        if not _open_new_tab(driver, login_url):
            raise LoginFailedError("Không thể mở Tab 2")

        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2)

        # Nhập email
        if not _enter_email(driver, hotmail_email):
            raise LoginFailedError("Không thể nhập email Hotmail")

        # Nhập password: thử mật khẩu trong Google Sheet & mật khẩu mới ('Khoa01@@c'), cái nào đúng dùng cái đó
        fallback_pw = new_hotmail_pass or getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c")
        if not _enter_password(
            driver,
            hotmail_password,
            hotmail_email,
            recovery_email,
            fallback_password=fallback_pw,
        ):
            raise LoginFailedError("Không thể nhập password Hotmail")

        # Xử lý các bước bảo mật / Add email / Privacy notice / Stay signed in
        handle_microsoft_prompts(
            driver,
            hotmail_email,
            recovery_email,
            current_password=hotmail_password,
            preferred_domain=target_domain,
        )

        # Xác nhận
        time.sleep(2)
        current_url = driver.current_url

        cur_lower = current_url.lower()
        if (
            "login.live.com/login.srf" not in cur_lower
            and "login.live.com/oauth20_authorize.srf" not in cur_lower
            and "login.live.com/ppsecure/post.srf" not in cur_lower
            and "interrupt/credentialaction" not in cur_lower
            and "identity/confirm" not in cur_lower
        ):
            if "outlook.live.com" in cur_lower:
                _dismiss_outlook_popups(driver)
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
