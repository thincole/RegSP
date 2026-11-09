"""
Bước 1: Tự động đăng nhập Facebook trên Tab 1.
Phải thành công mới được chuyển sang bước tiếp theo.
"""
import time

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

import config
from modules.totp_generator import generate_2fa_code, get_remaining_seconds
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


def _has_c_user_cookie(driver: WebDriver) -> bool:
    """Kiểm tra trình duyệt đã có cookie c_user của Facebook chưa."""
    try:
        for cookie in driver.get_cookies():
            if cookie.get("name") == "c_user" and cookie.get("value"):
                return True
    except Exception:
        pass
    return False


def _dismiss_automated_behavior_warning(driver: WebDriver) -> bool:
    """
    Xử lý cảnh báo 'Chúng tôi nghi là tài khoản của bạn có hành vi tự động' /
    'We suspect automated behavior on your account' (URL thường chứa /checkpoint/601051028565049).
    Tự động tìm và bấm nút 'Bỏ qua' / 'Dismiss'.
    """
    driver.implicitly_wait(0)
    try:
        cur_url = (driver.current_url or "").lower()
        body_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()

        is_auto_warning = (
            "601051028565049" in cur_url
            or "hành vi tự động" in body_txt
            or "automated behavior" in body_txt
            or "temporarily restricted or permanently disabled" in body_txt
        )
        if not is_auto_warning:
            return False

        logger.info("⚠️ Phát hiện cảnh báo 'hành vi tự động' (checkpoint 601051028565049) -> Tự động bấm 'Bỏ qua' / 'Dismiss'...")
        dismiss_xp = (
            "//*[@role='button'][normalize-space()='Bỏ qua' or normalize-space()='Dismiss' or normalize-space()='Continue' or normalize-space()='Tiếp tục'] | "
            "//span[normalize-space()='Bỏ qua' or normalize-space()='Dismiss' or normalize-space()='Continue' or normalize-space()='Tiếp tục']/ancestor::*[@role='button' or self::button][1] | "
            "//button[normalize-space()='Bỏ qua' or normalize-space()='Dismiss' or normalize-space()='Continue' or normalize-space()='Tiếp tục'] | "
            "//div[@role='button'][contains(., 'Bỏ qua') or contains(., 'Dismiss')]"
        )
        for _ in range(8):
            els = [e for e in driver.find_elements(By.XPATH, dismiss_xp) if e.is_displayed()]
            if els:
                if not safe_click(els[0], delay=2.0):
                    driver.execute_script("arguments[0].click();", els[0])
                    time.sleep(2.0)
                logger.info("✅ Đã bấm 'Bỏ qua' (Dismiss) cảnh báo hành vi tự động!")
                return True
            time.sleep(0.5)
    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.debug(f"Lỗi khi kiểm tra cảnh báo hành vi tự động: {e}")
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return False


def _check_already_logged_in(driver: WebDriver) -> bool:
    """Kiểm tra xem Facebook đã đăng nhập sẵn chưa (bắt buộc phải có cookie c_user)."""
    try:
        _dismiss_automated_behavior_warning(driver)
        current_url = driver.current_url.lower()
        blocked_paths = ["login", "/checkpoint", "two_step_verification", "two_factor", "recover"]
        if "facebook.com" in current_url and not any(p in current_url for p in blocked_paths):
            if _has_c_user_cookie(driver):
                logger.info("Facebook đã đăng nhập sẵn (có c_user cookie)")
                return True
    except BrowserClosedError:
        raise
    except Exception:
        pass
    return False


def _is_2fa_page(driver: WebDriver) -> bool:
    """
    Kiểm tra xem trang hiện tại có phải là màn hình nhập mã 2FA, chờ phê duyệt thiết bị (Try another way),
    hoặc Remember Browser của Facebook hay không (để không bị nhầm với màn hình Captcha pre_authentication).
    """
    driver.implicitly_wait(0)
    try:
        cur_url = (driver.current_url or "").lower()
        if "two_step_verification/two_factor" in cur_url or "flow=two_factor" in cur_url or "two_factor/remember_browser" in cur_url:
            return True
        if driver.find_elements(By.CSS_SELECTOR, "#approvals_code, input[name='approvals_code'], input[autocomplete='one-time-code']"):
            return True
        body_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
        two_fa_phrases = (
            "go to your authentication app",
            "enter the 6-digit code",
            "two-factor authentication",
            "check your notifications on another device",
            "waiting for approval",
            "choose a way to confirm it's you",
            "choose a way to confirm it’s you",
            "kiểm tra thông báo trên thiết bị khác",
            "đang chờ phê duyệt",
            "mã xác thực",
            "ứng dụng xác thực",
            "trust this device",
            "tin cậy thiết bị này",
        )
        if any(p in body_txt for p in two_fa_phrases):
            return True
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return False


def _handle_post_2fa_prompts(driver: WebDriver) -> None:
    """
    Xử lý các hộp thoại xuất hiện sau khi nhập mã 2FA thành công:
    0. Cảnh báo 'Chúng tôi nghi là tài khoản của bạn có hành vi tự động' -> Bấm 'Bỏ qua' / 'Dismiss'.
    1. Popup 'You're back on Facebook' -> Bấm nút X (aria-label='Close').
    2. Màn hình 'You’re logged in. Trust this device?' (two_factor/remember_browser) -> Bấm 'Trust this device'.
    3. Hộp thoại 'Remember Password' trên trang chủ Facebook -> Bấm 'OK'.
    """
    close_xp = "//*[@role='button' and (@aria-label='Close' or @aria-label='Đóng')]"
    trust_xp = (
        "//*[@role='button'][normalize-space()='Trust this device' or normalize-space()='Tin cậy thiết bị này' "
        "or normalize-space()='Save browser' or normalize-space()='Lưu trình duyệt'] | "
        "//span[normalize-space()='Trust this device' or normalize-space()='Tin cậy thiết bị này' "
        "or normalize-space()='Save browser' or normalize-space()='Lưu trình duyệt']/ancestor::*[@role='button'][1] | "
        "//button[contains(., 'Trust this device') or contains(., 'Save browser') or contains(., 'Continue')]"
    )
    ok_xp = (
        "//*[@role='button'][normalize-space()='OK'] | "
        "//span[normalize-space()='OK']/ancestor::*[@role='button'][1] | "
        "//button[normalize-space()='OK']"
    )

    driver.implicitly_wait(0)
    try:
        for _ in range(10):
            _dismiss_automated_behavior_warning(driver)
            driver.implicitly_wait(0)

            cur_u = (driver.current_url or "").lower()

            # 1. Đóng popup "You're back on Facebook" bằng nút X (aria-label='Close') nếu có
            els = [e for e in driver.find_elements(By.XPATH, close_xp) if e.is_displayed()]
            if els:
                logger.info("Đóng popup thông báo bằng nút X (Close)...")
                if not safe_click(els[0], delay=1.0):
                    driver.execute_script("arguments[0].click();", els[0])
                    time.sleep(1.0)

            # 2. Bấm "Trust this device" trên màn hình two_factor/remember_browser
            els = [e for e in driver.find_elements(By.XPATH, trust_xp) if e.is_displayed()]
            if els:
                logger.info("✅ Bấm nút 'Trust this device'...")
                if not safe_click(els[0], delay=2.0):
                    driver.execute_script("arguments[0].click();", els[0])
                    time.sleep(2.0)
                continue

            # 3. Bấm "OK" ở khung "Remember Password" nếu có
            els = [e for e in driver.find_elements(By.XPATH, ok_xp) if e.is_displayed()]
            if els:
                logger.info("✅ Bấm nút 'OK' (Remember Password)...")
                if not safe_click(els[0], delay=1.5):
                    driver.execute_script("arguments[0].click();", els[0])
                    time.sleep(1.5)

            # Nếu đã nhận c_user và đã vào trang chủ Facebook (không còn ở trang trung gian) -> thoát ngay!
            cur_u = (driver.current_url or "").lower()
            if _has_c_user_cookie(driver) and not any(
                k in cur_u for k in ("two_step_verification", "remember_browser", "checkpoint", "login")
            ):
                break

            time.sleep(0.6)
    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.debug(f"Lỗi khi xử lý prompt sau 2FA: {e}")
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass


def _enter_credentials(driver: WebDriver, fb_uid: str, fb_password: str) -> bool:
    """Nhập thông tin đăng nhập Facebook."""
    # Tìm ô email (dùng name vì Gemlogin có ID ngẫu nhiên)
    email_input = wait_for_element(
        driver, By.CSS_SELECTOR, "input[name='email']",
        timeout=config.ELEMENT_WAIT_TIMEOUT, condition="visible",
    )
    if not email_input:
        email_input = wait_for_element(
            driver, By.ID, "email",
            timeout=5, condition="visible",
        )
    if not email_input:
        logger.error("Không tìm thấy ô Email trên Facebook")
        return False

    if not safe_send_keys(email_input, fb_uid, delay=0.5):
        return False
    logger.info(f"Đã nhập Facebook UID: {fb_uid}")

    # Tìm ô password (dùng name vì Gemlogin có ID ngẫu nhiên)
    pass_input = wait_for_element(
        driver, By.CSS_SELECTOR, "input[name='pass']",
        timeout=config.ELEMENT_WAIT_TIMEOUT, condition="visible",
    )
    if not pass_input:
        pass_input = wait_for_element(
            driver, By.ID, "pass",
            timeout=5, condition="visible",
        )
    if not pass_input:
        logger.error("Không tìm thấy ô Password trên Facebook")
        return False

    if not safe_send_keys(pass_input, fb_password, delay=0.5):
        return False
    logger.info("Đã nhập Facebook Password")
    return True


def _click_login_button(driver: WebDriver) -> bool:
    """Click nút đăng nhập Facebook.
    Trên Gemlogin, nút Login là <input type='submit'> bị ẩn → dùng Enter key.
    """
    # Cách 1 (chính): Nhấn Enter trên ô password
    try:
        pass_input = driver.find_element(By.CSS_SELECTOR, "input[name='pass']")
        pass_input.send_keys(Keys.RETURN)
        time.sleep(3)
        logger.info("✅ Đã nhấn Enter để đăng nhập")
        return True
    except Exception as e:
        logger.warning(f"Enter trên password thất bại: {e}")

    # Cách 2: JavaScript click input[type=submit]
    try:
        submit = driver.find_element(By.CSS_SELECTOR, "input[type='submit']")
        driver.execute_script("arguments[0].click();", submit)
        time.sleep(3)
        logger.info("✅ Đã click Login (JavaScript)")
        return True
    except Exception as e:
        logger.warning(f"JS click submit thất bại: {e}")

    # Cách 3: Nhấn Enter trên ô email
    try:
        email_input = driver.find_element(By.CSS_SELECTOR, "input[name='email']")
        email_input.send_keys(Keys.RETURN)
        time.sleep(3)
        logger.info("✅ Đã nhấn Enter (email) để đăng nhập")
        return True
    except Exception:
        pass

    logger.error("Không thể đăng nhập Facebook")
    return False


def _switch_to_auth_app_if_needed(driver: WebDriver) -> bool:
    """
    Xử lý màn hình 'Check your notifications on another device' (Waiting for approval):
    1. Bấm nút 'Try another way' / 'Thử cách khác'.
    2. Trong popup 'Choose a way to confirm it's you', tích chọn 'Authentication app' / 'Ứng dụng xác thực'.
    3. Bấm 'Continue' / 'Tiếp tục' để chuyển sang màn hình nhập mã 6 số.
    """
    driver.implicitly_wait(0)
    try:
        # Kiểm tra xem đã có ô nhập mã 6 số hiển thị sẵn chưa
        existing_inputs = [
            e for e in driver.find_elements(
                By.CSS_SELECTOR,
                "#approvals_code, input[name='approvals_code'], input[autocomplete='one-time-code'], "
                "input[type='text']:not([name='email']), input[type='tel'], input[type='number']"
            )
            if e.is_displayed()
        ]
        body_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
        needs_switch = (
            not existing_inputs
            or "check your notifications on another device" in body_txt
            or "waiting for approval" in body_txt
            or "kiểm tra thông báo trên thiết bị khác" in body_txt
            or "đang chờ phê duyệt" in body_txt
            or "choose a way to confirm" in body_txt
            or "chọn cách xác nhận" in body_txt
        )
        if not needs_switch:
            return False

        # Bước 1: Nếu chưa mở hộp thoại chọn phương thức, bấm "Try another way" / "Thử cách khác"
        try_another_xpaths = [
            "//*[@role='button'][normalize-space()='Try another way' or normalize-space()='Thử cách khác' or normalize-space()='Choose another way']",
            "//span[normalize-space()='Try another way' or normalize-space()='Thử cách khác' or normalize-space()='Choose another way']/ancestor::*[@role='button' or self::button][1]",
            "//div[@role='button'][contains(., 'Try another way') or contains(., 'Thử cách khác')]",
            "//button[contains(., 'Try another way') or contains(., 'Thử cách khác')]",
        ]
        clicked_try_another = False
        for xp in try_another_xpaths:
            els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
            if els:
                logger.info("Phát hiện màn hình chờ phê duyệt thiết bị -> Bấm 'Try another way'...")
                if not safe_click(els[0], delay=2.0):
                    driver.execute_script("arguments[0].click();", els[0])
                    time.sleep(2.0)
                clicked_try_another = True
                break

        # Bước 2: Trong hộp thoại "Choose a way to confirm it's you", chọn "Authentication app" / "Ứng dụng xác thực"
        auth_app_xpaths = [
            "//div[@role='dialog']//*[normalize-space()='Authentication app' or normalize-space()='Ứng dụng xác thực']/ancestor::label[1]",
            "//div[@role='dialog']//*[normalize-space()='Authentication app' or normalize-space()='Ứng dụng xác thực']/ancestor::*[@role='radio' or @role='button' or @tabindex='0'][1]",
            "//div[@role='dialog']//*[contains(text(), 'Authentication app') or contains(text(), 'Ứng dụng xác thực') or contains(text(), 'authentication app')]",
            "//*[normalize-space()='Authentication app' or normalize-space()='Ứng dụng xác thực']/ancestor::label[1]",
            "//*[normalize-space()='Authentication app' or normalize-space()='Ứng dụng xác thực']",
        ]
        selected_auth_app = False
        for _ in range(3):
            for xp in auth_app_xpaths:
                els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
                if els:
                    logger.info("Chọn phương thức 'Authentication app' (Ứng dụng xác thực)...")
                    if not safe_click(els[0], delay=1.2):
                        driver.execute_script("arguments[0].click();", els[0])
                        time.sleep(1.2)
                    selected_auth_app = True
                    break
            if selected_auth_app:
                break
            if not clicked_try_another:
                break
            time.sleep(1.0)

        if not (clicked_try_another or selected_auth_app):
            return False

        # Bước 3: Bấm nút "Continue" / "Tiếp tục" trong hộp thoại
        continue_xpaths = [
            "//div[@role='dialog']//*[@role='button'][normalize-space()='Continue' or normalize-space()='Tiếp tục']",
            "//div[@role='dialog']//span[normalize-space()='Continue' or normalize-space()='Tiếp tục']/ancestor::*[@role='button' or self::button][1]",
            "//*[@role='button'][normalize-space()='Continue' or normalize-space()='Tiếp tục']",
            "//span[normalize-space()='Continue' or normalize-space()='Tiếp tục']/ancestor::*[@role='button' or self::button][1]",
        ]
        for xp in continue_xpaths:
            els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
            if els:
                logger.info("✅ Bấm 'Continue' để mở màn hình nhập mã 6 số Authentication app...")
                if not safe_click(els[0], delay=2.5):
                    driver.execute_script("arguments[0].click();", els[0])
                    time.sleep(2.5)
                return True
    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.debug(f"Lỗi khi chuyển sang Authentication app: {e}")
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return False


def _handle_2fa(driver: WebDriver, fb_2fa_secret: str) -> bool:
    """Xử lý trang nhập mã 2FA nếu Facebook yêu cầu (hỗ trợ cả giao diện cũ và giao diện two_step_verification/two_factor mới)."""
    time.sleep(3)

    # Nếu đã có cookie c_user và đang ở trang remember_browser hoặc trang chủ thì chỉ cần xử lý prompt sau 2FA
    if _has_c_user_cookie(driver) and "two_step_verification/two_factor" not in (driver.current_url or "").lower():
        _handle_post_2fa_prompts(driver)
        return True

    # Nếu Facebook hiện màn hình "Check your notifications on another device" -> bấm "Try another way" -> chọn "Authentication app" -> "Continue"
    _switch_to_auth_app_if_needed(driver)

    two_fa_css = (
        "#approvals_code, input[name='approvals_code'], input[autocomplete='one-time-code'], "
        "input[type='text']:not([name='email']), input[type='tel'], input[type='number']"
    )

    code_input = wait_for_element(driver, By.CSS_SELECTOR, two_fa_css, timeout=4, condition="visible")

    # Thử chuyển sang Authentication app lần nữa nếu lần đầu trang load chậm chưa hiện ô input
    if not code_input and _switch_to_auth_app_if_needed(driver):
        code_input = wait_for_element(driver, By.CSS_SELECTOR, two_fa_css, timeout=4, condition="visible")

    if not code_input:
        logger.info("Không phát hiện yêu cầu 2FA")
        return True

    if not fb_2fa_secret:
        logger.error("Facebook yêu cầu 2FA nhưng không có secret key")
        return False

    logger.info("Facebook yêu cầu mã 2FA, đang tạo mã...")

    # Chờ mã mới nếu sắp hết hạn
    remaining = get_remaining_seconds(fb_2fa_secret)
    if remaining < 5:
        logger.info(f"Mã 2FA sắp hết hạn ({remaining}s), chờ mã mới...")
        time.sleep(remaining + 1)

    code = generate_2fa_code(fb_2fa_secret)
    logger.info(f"Mã 2FA: {code}")

    if not safe_send_keys(code_input, code, delay=1.0):
        return False

    # Submit: hỗ trợ cả thẻ <div role="button">Continue</div> trên giao diện mới và button[type='submit'] cũ
    submit_xp = (
        "//*[@role='button'][normalize-space()='Continue' or normalize-space()='Tiếp tục' or normalize-space()='Submit'] | "
        "//span[normalize-space()='Continue' or normalize-space()='Tiếp tục']/ancestor::*[@role='button'][1] | "
        "//button[@type='submit' or @id='checkpointSubmitButton']"
    )
    submit_btn = wait_for_element(driver, By.XPATH, submit_xp, timeout=4, condition="clickable")
    if submit_btn:
        if not safe_click(submit_btn, delay=3.0):
            driver.execute_script("arguments[0].click();", submit_btn)
            time.sleep(3.0)
        logger.info("✅ Đã click 'Continue' submit mã 2FA")
    else:
        code_input.send_keys(Keys.RETURN)
        time.sleep(3.0)
        logger.info("Đã nhấn Enter để submit 2FA")

    # Xử lý các màn hình sau khi nhập 2FA: đóng popup X -> bấm 'Trust this device' -> bấm 'OK' (Remember Password)
    _handle_post_2fa_prompts(driver)
    return True


def _wait_for_fb_captcha(driver: WebDriver, timeout: int = None) -> None:
    """
    Kiểm tra và chờ người dùng giải captcha Facebook (mặc định 5 phút = 300s, quét 5s/lần).
    Nếu trang đang ở màn hình nhập mã 2FA (two_step_verification/two_factor) thì KHÔNG coi là captcha
    mà thoát ngay để hàm _handle_2fa tự động điền mã 6 số.
    """
    if timeout is None:
        timeout = getattr(config, "CAPTCHA_TIMEOUT", 300)
    check_interval = getattr(config, "CAPTCHA_CHECK_INTERVAL", 5)

    captcha_keywords = [
        "security check", "confirm your identity",
        "enter the characters", "select all images",
        "verify that you are human", "nhập các ký tự",
    ]

    try:
        # Nếu đang ở cảnh báo hành vi tự động (checkpoint/601051028565049) thì tự bấm 'Bỏ qua' trước
        if _dismiss_automated_behavior_warning(driver):
            time.sleep(1.5)

        # Nếu đang ở trang 2FA (nhập 6 số Authenticator) hoặc Remember Browser thì thoát ngay để _handle_2fa xử lý
        if _is_2fa_page(driver):
            return

        current_url = driver.current_url.lower()
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            body_text = ""

        has_captcha = False
        reason = ""

        # Kiểm tra URL (lưu ý: 'two_step_verification/authentication' hoặc 'flow=pre_authentication' mới là captcha)
        for kw in ["captcha", "/checkpoint", "security", "pre_authentication", "two_step_verification/authentication"]:
            if kw in current_url:
                has_captcha = True
                reason = f"URL chứa '{kw}'"
                break

        # Kiểm tra nội dung text hiển thị trên trang
        if not has_captcha and body_text:
            for kw in captcha_keywords:
                if kw in body_text:
                    has_captcha = True
                    reason = f"Text chứa '{kw}'"
                    break

        # Kiểm tra iframe captcha
        if not has_captcha:
            driver.implicitly_wait(0)
            try:
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                for iframe in iframes:
                    src = (iframe.get_attribute("src") or "").lower()
                    if ("captcha" in src or "recaptcha" in src or "hcaptcha" in src or "referer_frame" in src) and iframe.is_displayed():
                        has_captcha = True
                        reason = f"Iframe captcha ({src[:40]})"
                        break
            finally:
                driver.implicitly_wait(5)

        if not has_captcha:
            return  # Không có captcha → tiếp tục bình thường

        logger.warning(f"⚠️ Facebook yêu cầu xác minh/CAPTCHA ({reason})! URL: {driver.current_url}")
        logger.warning(f"⚠️ Vui lòng xử lý trên trình duyệt (chờ tối đa {timeout}s, quét {check_interval}s/lần)...")

        try:
            driver.execute_cdp_cmd("Page.bringToFront", {})
        except Exception:
            pass
        try:
            import winsound
            winsound.Beep(1000, 350)
            winsound.Beep(1300, 350)
        except Exception:
            pass

        # Chờ captcha được giải (quét mỗi check_interval giây)
        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(check_interval)
            check_browser_alive(driver)
            try:
                if _dismiss_automated_behavior_warning(driver):
                    time.sleep(1.5)

                # Nếu sau khi giải Captcha, Facebook chuyển sang màn hình nhập mã 2FA -> thoát ngay để _handle_2fa điền code!
                if _is_2fa_page(driver):
                    logger.info("✅ Đã vượt qua Captcha, chuyển sang màn hình nhập mã 2FA!")
                    time.sleep(1)
                    return

                new_url = driver.current_url.lower()

                if _has_c_user_cookie(driver) and not any(
                    kw in new_url for kw in ["captcha", "/checkpoint", "security", "pre_authentication", "login"]
                ):
                    logger.info("✅ Đã vượt qua bước xác minh/captcha (đã nhận c_user cookie)! Tiếp tục sang bước tiếp theo...")
                    time.sleep(1)
                    return

                still_captcha = False
                if any(kw in new_url for kw in ["captcha", "/checkpoint", "security", "pre_authentication", "two_step_verification/authentication"]):
                    still_captcha = True
                else:
                    try:
                        new_body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                    except Exception:
                        new_body_text = ""
                    if new_body_text:
                        for kw in captcha_keywords:
                            if kw in new_body_text:
                                still_captcha = True
                                break

                if not still_captcha:
                    # Chờ thêm 2s kiểm tra lại xem có phải đang redirect sang trang captcha tiếp theo không
                    time.sleep(2)
                    if _is_2fa_page(driver):
                        logger.info("✅ Đã vượt qua Captcha, chuyển sang màn hình nhập mã 2FA!")
                        return
                    recheck_url = driver.current_url.lower()
                    if any(kw in recheck_url for kw in ["captcha", "/checkpoint", "security", "pre_authentication", "two_step_verification/authentication"]):
                        continue
                    logger.info("✅ Captcha Facebook đã được giải! Tiếp tục sang bước tiếp theo...")
                    time.sleep(1)
                    return

                elapsed = int(time.time() - start_time)
                logger.info(f"⏳ Đang chờ giải captcha Facebook... ({elapsed}s/{timeout}s) | URL: {new_url[:60]}")

            except BrowserClosedError:
                raise
            except Exception as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e

        logger.warning(f"⏰ Hết thời gian chờ captcha ({timeout}s). Tiếp tục...")

    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.debug(f"Lỗi kiểm tra captcha: {e}")


def _import_facebook_cookie(driver: WebDriver, fb_cookie: str) -> str:
    """
    Đăng nhập Facebook bằng cách nạp trực tiếp chuỗi Cookie (cơ chế giống hệt extension 'Get Token Cookie'):
    - Xóa toàn bộ cookie facebook.com cũ.
    - Nạp từng cặp name=value từ chuỗi cookie ('c_user=...;xs=...;fr=...;datr=...;')
      vào domain '.facebook.com' trên toàn bộ các subdomain của Facebook qua CDP Network.setCookie & WebDriver.
    - Reload trang https://www.facebook.com và kiểm tra trạng thái đăng nhập.

    Returns:
        "SUCCESS": Đăng nhập thành công (có c_user và vào thẳng Facebook).
        "CHECKPOINT": Tài khoản bị khóa / checkpoint (FB die).
        "EXPIRED": Cookie không hợp lệ hoặc hết hạn (cần fallback sang UID + Password).
    """
    if not fb_cookie or "c_user=" not in fb_cookie:
        return "EXPIRED"

    # Nếu chuỗi truyền vào vô tình chứa dấu '|', tách phần có 'c_user=' (giống hàm g() trong popup.js)
    raw_cookie = fb_cookie.strip()
    if "|" in raw_cookie:
        for seg in raw_cookie.split("|"):
            if "c_user=" in seg:
                raw_cookie = seg.strip()
                break

    cookie_pairs = []
    for item in raw_cookie.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, val = item.split("=", 1)
        name, val = name.strip(), val.strip()
        if name and val:
            cookie_pairs.append((name, val))

    if not any(k == "c_user" for k, _ in cookie_pairs):
        return "EXPIRED"

    logger.info(f"🍪 Đang import Cookie Facebook ({len(cookie_pairs)} trường: {[k for k, _ in cookie_pairs]})...")

    fb_urls = [
        "https://www.facebook.com",
        "https://upload.facebook.com",
        "https://business.facebook.com",
        "https://web.facebook.com",
        "https://m.facebook.com",
        "https://mbasic.facebook.com",
        "https://developers.facebook.com",
        "https://mobile.facebook.com",
    ]

    # 1. Xóa sạch cookie facebook.com cũ (giống hàm b() trong popup.js của Get Token Cookie)
    try:
        driver.delete_all_cookies()
    except Exception:
        pass
    try:
        existing = driver.execute_cdp_cmd("Network.getCookies", {"urls": fb_urls}).get("cookies", [])
        for c in existing:
            if "facebook.com" in c.get("domain", ""):
                driver.execute_cdp_cmd(
                    "Network.deleteCookies",
                    {"name": c["name"], "domain": c["domain"], "path": c.get("path", "/")},
                )
    except Exception:
        pass

    # 2. Set từng cookie cho .facebook.com trên toàn bộ danh sách fb_urls (giống hàm g() trong popup.js)
    for name, value in cookie_pairs:
        cdp_ok = False
        for url in fb_urls:
            try:
                driver.execute_cdp_cmd(
                    "Network.setCookie",
                    {
                        "url": url,
                        "name": name,
                        "value": value,
                        "domain": ".facebook.com",
                        "path": "/",
                        "secure": True,
                    },
                )
                cdp_ok = True
            except Exception:
                pass

        if not cdp_ok:
            try:
                driver.add_cookie(
                    {
                        "name": name,
                        "value": value,
                        "domain": ".facebook.com",
                        "path": "/",
                        "secure": True,
                    }
                )
            except Exception as e:
                logger.debug(f"Không thể set cookie {name}: {e}")

    # 3. Reload lại trang Facebook để áp dụng cookie vừa import
    driver.get(config.FACEBOOK_URL)
    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
    time.sleep(2.5)

    # 4. Xử lý cảnh báo hành vi tự động ('Bỏ qua' / 'Dismiss') hoặc popup sau đăng nhập nếu có
    _dismiss_automated_behavior_warning(driver)
    _handle_post_2fa_prompts(driver)

    cur_url = (driver.current_url or "").lower()
    if "/checkpoint" in cur_url and not _has_c_user_cookie(driver):
        logger.warning("⚠️ Tài khoản Facebook bị checkpoint ngay sau khi import Cookie — FB die!")
        return "CHECKPOINT"

    if _check_already_logged_in(driver):
        logger.info("✅ Import Cookie Facebook thành công — Đã đăng nhập thẳng vào Facebook!")
        return "SUCCESS"

    logger.warning(f"⚠️ Import Cookie không giữ được phiên đăng nhập (URL: {driver.current_url})")
    return "EXPIRED"


def login_facebook(
    driver: WebDriver, fb_uid: str, fb_password: str,
    fb_2fa_secret: str = "", fb_cookie: str = "",
) -> bool:
    """
    Đăng nhập Facebook:
    - Ưu tiên 1: Kiểm tra profile đã login sẵn chưa.
    - Ưu tiên 2: Import trực tiếp Cookie Facebook (nếu trong data có sẵn c_user=...;xs=...).
    - Ưu tiên 3 (Fallback): Đăng nhập bằng UID + Password + 2FA nếu Cookie hết hạn hoặc không có.

    Returns:
        True nếu đăng nhập thành công.
    """
    logger.info(f"=== BƯỚC 1: Đăng nhập Facebook (UID: {fb_uid}) ===")

    try:
        # Mở trang Facebook kiểm tra đã login sẵn chưa
        driver.get(config.FACEBOOK_URL)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2.5)

        if _check_already_logged_in(driver):
            logger.info("✅ Facebook đã đăng nhập sẵn trên profile này!")
            return True

        # Ưu tiên đăng nhập nhanh bằng Cookie (cơ chế từ extension Get Token Cookie)
        if fb_cookie and "c_user=" in fb_cookie:
            cookie_status = _import_facebook_cookie(driver, fb_cookie)
            if cookie_status == "SUCCESS":
                return True
            if cookie_status == "CHECKPOINT":
                return False
            logger.info("Chuyển sang đăng nhập dự phòng bằng UID + Password...")

        # Mở trang đăng nhập Facebook
        logger.info("Đăng nhập bằng UID + Password...")
        driver.get("https://www.facebook.com/login/")
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(3)

        if _check_already_logged_in(driver):
            logger.info("✅ Facebook đã đăng nhập sẵn!")
            return True

        # Nếu vừa vào /login/ đã bị Facebook đẩy sang pre_authentication captcha trước khi hiện ô email
        if any(kw in driver.current_url.lower() for kw in ["two_step_verification", "pre_authentication", "captcha", "security"]):
            _wait_for_fb_captcha(driver)
            time.sleep(2)
            if _check_already_logged_in(driver):
                logger.info("✅ Đăng nhập Facebook thành công sau khi giải Captcha!")
                return True
            if "login" not in driver.current_url.lower():
                driver.get("https://www.facebook.com/login/")
                wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                time.sleep(2)

        # Nhập UID và Password
        if not _enter_credentials(driver, fb_uid, fb_password):
            raise LoginFailedError("Không thể nhập thông tin Facebook")

        # Nhấn Enter để login
        if not _click_login_button(driver):
            raise LoginFailedError("Không thể đăng nhập")

        time.sleep(5)

        # Chờ captcha (nếu FB yêu cầu) — người dùng giải bằng tay
        _wait_for_fb_captcha(driver)

        # Nếu sau khi giải Captcha pre_authentication, Facebook chuyển ngược về trang /login/ (yêu cầu nhập lại pass)
        url_path = (driver.current_url or "").lower().split("?")[0]
        if not _has_c_user_cookie(driver) and not _is_2fa_page(driver) and "/login" in url_path:
            logger.info("Facebook chuyển về trang Login sau khi giải Captcha pre_authentication -> Nhập lại thông tin đăng nhập...")
            time.sleep(2)
            if _enter_credentials(driver, fb_uid, fb_password):
                _click_login_button(driver)
                time.sleep(5)
                _wait_for_fb_captcha(driver)

        # Xử lý 2FA (nếu có)
        if fb_2fa_secret:
            if not _handle_2fa(driver, fb_2fa_secret):
                raise LoginFailedError("Xử lý 2FA thất bại")

        # Chờ captcha lần nữa (nếu chưa có c_user cookie)
        if not _has_c_user_cookie(driver):
            _wait_for_fb_captcha(driver)

        # Xác nhận có cookie c_user (chờ tối đa 10s nếu trang đang redirect hoàn tất login)
        for _ in range(5):
            if _has_c_user_cookie(driver):
                break
            time.sleep(2)

        # Kiểm tra nhanh cảnh báo hành vi tự động (nếu xuất hiện trễ sau khi vào trang chủ)
        _dismiss_automated_behavior_warning(driver)

        current_url = driver.current_url.lower()

        if "/checkpoint" in current_url and not _has_c_user_cookie(driver):
            logger.warning("Facebook yêu cầu checkpoint — FB die!")
            return False

        if _has_c_user_cookie(driver):
            logger.info("✅ Đăng nhập Facebook thành công (đã nhận c_user cookie)!")
            return True
        else:
            logger.error(f"❌ Đăng nhập Facebook thất bại — chưa có c_user cookie (URL: {driver.current_url})")
            return False

    except (BrowserClosedError, LoginFailedError):
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi đăng nhập Facebook: {e}")
        raise LoginFailedError(f"Lỗi đăng nhập Facebook: {e}")


