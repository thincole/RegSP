"""
Bước 3: Đổi mật khẩu Hotmail trên Tab 2.
CHỈ chạy khi Bước 2 (Hotmail login) thành công.
Vẫn ở Tab 2, navigate tới account.live.com/proofs/Manage/ để đổi pass.
"""
import time

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

import config
from modules.hotmail_login import handle_microsoft_prompts, _submit_password_with_fallbacks, _enter_email
from utils.logger import get_logger
from utils.wait_helpers import (
    wait_for_element, wait_for_page_load, safe_click, safe_send_keys,
)
from utils.error_handler import (
    PasswordChangeError,
    BrowserClosedError,
    is_browser_closed_exception,
)

logger = get_logger()


def _handle_reverify(driver: WebDriver, current_password: str, new_password: str = "") -> None:
    """
    Xử lý nếu trang yêu cầu nhập lại mật khẩu để xác minh (chỉ khi có đúng 1 ô password).
    Tự động thử cả mật khẩu mới ('Khoa01@@c') lẫn mật khẩu trong Google Sheet (current_password).
    """
    time.sleep(1)

    pwd_inputs = [
        el for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password'], #i0118, #passwordEntry, input[name='passwd']")
        if el.is_displayed()
    ]
    if len(pwd_inputs) == 1:
        logger.info("Trang yêu cầu xác minh lại mật khẩu trước khi đổi pass...")
        candidates = [
            current_password,
            new_password,
            getattr(config, "NEW_HOTMAIL_PASSWORD", "Khoa01@@c"),
            "Khoa01@@c",
            "Khoa01@c",
        ]
        _submit_password_with_fallbacks(driver, candidates)
    else:
        logger.debug("Không cần xác minh lại mật khẩu")


def _click_change_password_link(driver: WebDriver) -> bool:
    """
    Tìm và click nút/link 'Change password' ngay trên trang account.live.com/proofs/Manage/additional.
    """
    selectors = [
        (By.CSS_SELECTOR, "#ChangePassword, button[data-mp-control='PasswordPullout.ChangePassword'], #changePasswordLink, a[href*='password/Change']"),
        (By.XPATH, "//button[normalize-space()='Change password' or normalize-space()='Thay đổi mật khẩu'] | //a[contains(., 'Change password') or contains(., 'Thay đổi mật khẩu')]"),
    ]

    for by, value in selectors:
        link = wait_for_element(driver, by, value, timeout=3, condition="clickable")
        if link:
            link_text = (link.text or link.get_attribute("id") or "Change password").strip()
            logger.info(f"Bấm nút đổi mật khẩu trực tiếp trên trang Manage: '{link_text}'")
            if not safe_click(link, delay=2.5):
                driver.execute_script("arguments[0].click();", link)
                time.sleep(2.5)
            return True

    # Nếu thẻ Password đang đóng thu gọn thì mở ra rồi bấm #ChangePassword
    pwd_toggle = wait_for_element(
        driver,
        By.CSS_SELECTOR,
        "#Password a[data-mp-control='Pullout.Toggle'], #Password .pullout-link",
        timeout=2,
        condition="clickable",
    )
    if pwd_toggle:
        if not safe_click(pwd_toggle, delay=1.0):
            driver.execute_script("arguments[0].click();", pwd_toggle)
            time.sleep(1.0)
        btn = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "#ChangePassword, button[data-mp-control='PasswordPullout.ChangePassword']",
            timeout=3,
            condition="clickable",
        )
        if btn:
            if not safe_click(btn, delay=2.5):
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2.5)
            return True

    return False


def _fill_password_form(
    driver: WebDriver, old_password: str, new_password: str,
) -> bool:
    """
    Điền form đổi mật khẩu: (Old password nếu có), New password, Confirm.
    """
    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
    wait_for_element(
        driver, By.CSS_SELECTOR, "#iPassword, input[type='password']",
        timeout=10, condition="visible",
    )
    time.sleep(1)

    pwd_inputs = [
        el for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
        if el.is_displayed()
    ]

    if len(pwd_inputs) == 2:
        logger.info("Form đổi mật khẩu có 2 ô (không yêu cầu Old Password)")
        safe_click(pwd_inputs[0], delay=0.3)
        safe_send_keys(pwd_inputs[0], new_password, delay=0.5)
        logger.info(f"Đã nhập New Password: {new_password}")

        safe_click(pwd_inputs[1], delay=0.3)
        safe_send_keys(pwd_inputs[1], new_password, delay=0.5)
        if not (pwd_inputs[1].get_attribute("value") or ""):
            pwd_inputs[1].send_keys(new_password)
            time.sleep(0.5)
        logger.info("Đã nhập Confirm Password")
        return True

    if len(pwd_inputs) >= 3:
        safe_click(pwd_inputs[0], delay=0.3)
        safe_send_keys(pwd_inputs[0], old_password, delay=0.5)
        logger.info("Đã nhập Old Password")

        safe_click(pwd_inputs[1], delay=0.3)
        safe_send_keys(pwd_inputs[1], new_password, delay=0.5)
        logger.info(f"Đã nhập New Password: {new_password}")

        safe_click(pwd_inputs[2], delay=0.3)
        safe_send_keys(pwd_inputs[2], new_password, delay=0.5)
        if not (pwd_inputs[2].get_attribute("value") or ""):
            pwd_inputs[2].send_keys(new_password)
            time.sleep(0.5)
        logger.info("Đã nhập Confirm Password")
        return True

    logger.error(f"Không tìm thấy đủ ô password trên form (tìm thấy {len(pwd_inputs)} ô)")
    return False


def _click_save_button(driver: WebDriver) -> bool:
    """Click nút Save / Lưu."""
    save_selectors = [
        (By.CSS_SELECTOR, "#UpdatePasswordAction, #idSIButton9, input[type='submit'][value='Save'], button[type='submit'], input[type='submit']"),
        (By.XPATH, "//button[contains(text(), 'Save') or contains(text(), 'Lưu')] | //input[@value='Save']"),
    ]

    for by, value in save_selectors:
        btn = wait_for_element(driver, by, value, timeout=3, condition="clickable")
        if btn:
            if not safe_click(btn, delay=2.0):
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2.0)
            else:
                # Nếu vẫn còn nút Save trên trang sau 1s thì thử JS click bồi thêm
                try:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(2.0)
                except Exception:
                    pass
            logger.info("Đã click nút Save")
            return True

    logger.error("Không tìm thấy nút Save")
    return False


def change_hotmail_password(
    driver: WebDriver, current_password: str, new_password: str,
    hotmail_email: str = "", recovery_email: str = "",
) -> bool:
    """
    Đổi mật khẩu Hotmail trên Tab 2.
    CHỈ chạy khi Bước 2 (Hotmail login) thành công.

    Quy trình:
    1. Vẫn ở Tab 2, navigate tới account.live.com/proofs/Manage/
    2. Xử lý các prompt bảo mật / Add email / Stay signed in nếu có
    3. Click 'Change password' (hoặc truy cập trực tiếp account.live.com/password/Change)
    4. Nhập (Old password nếu có), New password, Confirm
    5. Click Save

    Returns:
        True nếu đổi pass thành công.
    """
    logger.info(f"=== BƯỚC 3: Đổi mật khẩu Hotmail → '{new_password}' ===")

    try:
        # Nếu trình duyệt đang đứng sẵn ở trang account.live.com/proofs/Manage (sau bước thay mail khôi phục),
        # bấm trực tiếp nút 'Change password' ngay trên trang để giữ nguyên phiên xác thực (không cần load lại URL mới)
        cur_url_low = (driver.current_url or "").lower()
        clicked_on_manage = False
        if "proofs/manage" in cur_url_low:
            clicked_on_manage = _click_change_password_link(driver)
            if clicked_on_manage:
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(2)

        if not clicked_on_manage and "password/change" not in (driver.current_url or "").lower():
            # Xử lý các prompt còn tồn đọng (Passkey -> Cancel, Stay signed in -> Yes, ...)
            if hotmail_email or recovery_email:
                handle_microsoft_prompts(driver, hotmail_email, recovery_email, current_password=current_password)

            logger.info("Mở trang đổi mật khẩu: https://account.live.com/password/Change")
            driver.get("https://account.live.com/password/Change")
            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
            time.sleep(2)

        # Vòng lặp xử lý các bước trung gian (nhập email / xác minh lại mật khẩu / Verify your email / Passkey / Stay signed in)
        # cho đến khi vào được form đổi mật khẩu (có >= 2 ô input[type='password'])
        for _ in range(4):
            pwd_inputs = [
                el for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
                if el.is_displayed()
            ]
            if len(pwd_inputs) >= 2:
                break
            elif len(pwd_inputs) == 1:
                _handle_reverify(driver, current_password, new_password)
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(2)
            else:
                email_inputs = [
                    el for el in driver.find_elements(
                        By.CSS_SELECTOR, "input[type='email'], #i0116, #usernameEntry, input[name='loginfmt']"
                    )
                    if el.is_displayed()
                ]
                if email_inputs and hotmail_email:
                    _enter_email(driver, hotmail_email)
                    _handle_reverify(driver, current_password, new_password)
                if hotmail_email or recovery_email:
                    handle_microsoft_prompts(driver, hotmail_email, recovery_email, current_password=current_password)
                if "password/change" not in driver.current_url.lower():
                    driver.get("https://account.live.com/password/Change")
                    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                    time.sleep(2)

        # Điền form
        if not _fill_password_form(driver, current_password, new_password):
            raise PasswordChangeError("Không thể điền form đổi mật khẩu")

        # Click Save
        if not _click_save_button(driver):
            raise PasswordChangeError("Không thể click nút Save")

        # Xác nhận
        time.sleep(3)
        # Kiểm tra có thông báo lỗi không
        error_elements = driver.find_elements(
            By.CSS_SELECTOR, ".alert-error, .error, #iError, .pError"
        )
        for err in error_elements:
            if err.is_displayed() and err.text.strip():
                err_txt = err.text.strip()
                if "you used an old password" in err_txt.lower() or "mật khẩu cũ" in err_txt.lower():
                    logger.info(f"ℹ️ Mật khẩu Hotmail đã là '{new_password}' sẵn từ trước!")
                    return True
                logger.error(f"Lỗi đổi mật khẩu: {err_txt}")
                return False

        logger.info(f"✅ Đổi mật khẩu Hotmail thành công! Mật khẩu mới: {new_password}")
        return True

    except (BrowserClosedError, PasswordChangeError):
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi đổi mật khẩu Hotmail: {e}")
        raise PasswordChangeError(f"Lỗi đổi mật khẩu: {e}")
