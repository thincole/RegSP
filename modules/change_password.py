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
from modules.hotmail_login import handle_microsoft_prompts
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


def _handle_reverify(driver: WebDriver, current_password: str) -> None:
    """
    Xử lý nếu trang yêu cầu nhập lại mật khẩu để xác minh (chỉ khi có đúng 1 ô password).
    """
    time.sleep(1)

    pwd_inputs = [
        el for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
        if el.is_displayed()
    ]
    if len(pwd_inputs) == 1:
        pass_input = pwd_inputs[0]
        logger.info("Trang yêu cầu xác minh lại mật khẩu")
        safe_send_keys(pass_input, current_password, delay=1.0)

        submit_selectors = [
            (By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']"),
        ]
        for by, value in submit_selectors:
            btn = wait_for_element(driver, by, value, timeout=3, condition="clickable")
            if btn:
                safe_click(btn, delay=3.0)
                logger.info("Đã xác minh lại mật khẩu")
                break
        else:
            pass_input.send_keys(Keys.RETURN)
            time.sleep(3)

        # Nếu mật khẩu cũ báo sai (do đã đổi sang mật khẩu mới trước đó), thử mật khẩu mới
        try:
            body_lower = driver.find_element(By.TAG_NAME, "body").text.lower()
            if ("incorrect" in body_lower or "không chính xác" in body_lower) and current_password != config.NEW_HOTMAIL_PASSWORD:
                logger.warning("Mật khẩu cũ không đúng khi xác minh lại, thử mật khẩu mới...")
                pass_input2 = wait_for_element(
                    driver, By.CSS_SELECTOR, "input[type='password'], #i0118, input[name='passwd']",
                    timeout=5, condition="visible",
                )
                if pass_input2:
                    safe_send_keys(pass_input2, config.NEW_HOTMAIL_PASSWORD, delay=1.0)
                    btn2 = wait_for_element(
                        driver, By.CSS_SELECTOR, "button[type='submit'], #idSIButton9, input[type='submit']",
                        timeout=3, condition="clickable",
                    )
                    if btn2:
                        safe_click(btn2, delay=3.0)
                    else:
                        pass_input2.send_keys(Keys.RETURN)
                        time.sleep(3)
        except Exception:
            pass
    else:
        logger.debug("Không cần xác minh lại mật khẩu")


def _click_change_password_link(driver: WebDriver) -> bool:
    """
    Tìm và click link 'Change password' / 'Thay đổi mật khẩu'.
    """
    selectors = [
        (By.ID, "changePasswordLink"),
        (By.CSS_SELECTOR, "a[href*='password/Change']"),
        (By.CSS_SELECTOR, "a[href*='password']"),
        (By.XPATH, "//a[contains(text(), 'Change password')]"),
        (By.XPATH, "//a[contains(text(), 'Thay đổi mật khẩu')]"),
        (By.XPATH, "//a[contains(text(), 'change password')]"),
        (By.XPATH, "//button[contains(text(), 'Change password')]"),
        (By.XPATH, "//button[contains(text(), 'Thay đổi mật khẩu')]"),
        (By.CSS_SELECTOR, "a#iChangePassword"),
        (By.ID, "iChangePassword"),
    ]

    for by, value in selectors:
        link = wait_for_element(driver, by, value, timeout=3, condition="clickable")
        if link:
            link_text = link.text.strip()
            logger.info(f"Tìm thấy link đổi mật khẩu: '{link_text}'")
            if safe_click(link, delay=3.0):
                return True

    logger.error("Không tìm thấy link 'Change password'")
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
        # Xử lý các prompt còn tồn đọng (Passkey -> Cancel, Stay signed in -> Yes, ...)
        if hotmail_email or recovery_email:
            handle_microsoft_prompts(driver, hotmail_email, recovery_email, current_password=current_password)

        # Mở trực tiếp trang đổi mật khẩu để tiết kiệm thời gian
        logger.info("Mở trang đổi mật khẩu: https://account.live.com/password/Change")
        driver.get("https://account.live.com/password/Change")
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2)

        # Vòng lặp xử lý các bước trung gian (xác minh lại mật khẩu / Verify your email / Passkey / Stay signed in)
        # cho đến khi vào được form đổi mật khẩu (có >= 2 ô input[type='password'])
        for _ in range(4):
            pwd_inputs = [
                el for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
                if el.is_displayed()
            ]
            if len(pwd_inputs) >= 2:
                break
            elif len(pwd_inputs) == 1:
                _handle_reverify(driver, current_password)
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(2)
            else:
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
