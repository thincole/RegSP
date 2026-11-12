"""
Bước 3/4/5: Đăng nhập/Đăng ký Shopee bằng Facebook.
Hỗ trợ nhiều vùng: Shopee.PH, Shopee MY, Shopee ID.
CHỈ chạy khi các bước trước đã thành công.
"""
import time
import random
import re
import json
import html
import urllib.request
import urllib.parse
from typing import Tuple

import requests
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains

import config
from utils.logger import get_logger
from utils.wait_helpers import (
    wait_for_element, wait_for_page_load, wait_for_new_window,
    safe_click, safe_send_keys,
)
from utils.error_handler import (
    LoginFailedError,
    BrowserClosedError,
    is_browser_closed_exception,
    check_browser_alive,
)

logger = get_logger()


def _is_shopee_phone_verification(driver: WebDriver) -> bool:
    """
    Kiểm tra xem Shopee có đang hiện màn hình 'Mobile Phone Verification'
    (URL chứa 'signup-bind-phone' / 'bind-phone' hoặc nội dung 'For account security, please add your phone number to sign up')
    hay không. Nếu có thì bỏ qua luồng này ngay lập tức.
    """
    check_browser_alive(driver)
    driver.implicitly_wait(0)
    try:
        cur_url = (driver.current_url or "").lower()
        if "signup-bind-phone" in cur_url or "bind-phone" in cur_url or "bind_phone" in cur_url:
            return True
        body_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
        if (
            "mobile phone verification" in body_txt
            or "please add your phone number to sign up" in body_txt
            or "send otp to phone number" in body_txt
        ):
            return True
    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return False


def _wait_for_captcha(driver: WebDriver, timeout: int = None) -> bool:
    """
    Phát hiện captcha Shopee và chờ extension GemCaptcha tự động giải (hoặc người dùng giải bằng tay).
    Captcha có text 'Verify to Continue', 'slide to complete the puzzle',
    hoặc URL chứa 'verify/captcha' / 'verify/traffic'.
    Nếu người dùng đóng trình duyệt hoặc xuất hiện 'Mobile Phone Verification' thì thoát ngay lập tức.
    """
    if timeout is None:
        timeout = getattr(config, "CAPTCHA_TIMEOUT", 300)
    check_interval = getattr(config, "CAPTCHA_CHECK_INTERVAL", 5)

    time.sleep(1)
    check_browser_alive(driver)
    if _is_shopee_phone_verification(driver):
        logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
        raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")

    def _is_captcha_present() -> Tuple[bool, str]:
        check_browser_alive(driver)
        if _is_shopee_phone_verification(driver):
            logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
            raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
        try:
            cur_url = (driver.current_url or "").lower()
            if "verify/captcha" in cur_url or "verify/traffic" in cur_url:
                return True, f"URL={cur_url[:60]}"
            txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
            for kw in ("verify to continue", "slide to complete", "please slide", "please try again later"):
                if kw in txt:
                    return True, kw
            has_dom_captcha = driver.execute_script(
                "return Boolean(document.querySelector('[id*=\"captchaMask\"], [id*=\"puzzleContainer\"], [id*=\"NEW_CAPTCHA\"]'));"
            )
            if has_dom_captcha:
                return True, "DOM=captchaMask/puzzleContainer"
        except (BrowserClosedError, LoginFailedError):
            raise
        except Exception as e:
            if is_browser_closed_exception(e):
                raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        return False, ""

    driver.implicitly_wait(0)
    try:
        captcha_found, matched_reason = _is_captcha_present()
        if not captcha_found:
            return True

        logger.warning(f"⚠️ CAPTCHA Shopee phát hiện! ({matched_reason}) -> Đang chờ GemCaptcha tự động giải...")

        start_time = time.time()
        alerted_manual = False
        last_gem_msg = ""

        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)
            # Trong 30s đầu quét nhanh 2s/lần để đợi extension GemCaptcha tự kéo giải
            poll_sleep = 2 if elapsed < 30 else check_interval
            time.sleep(poll_sleep)

            still_visible, reason = _is_captcha_present()
            if not still_visible:
                logger.info("✅ Captcha Shopee đã được giải thành công! Tiếp tục sang bước tiếp theo...")
                time.sleep(1)
                return True

            try:
                gem_msg = (
                    driver.execute_script(
                        "const el = document.getElementById('captcha-message'); return el ? el.innerText : '';"
                    )
                    or ""
                ).strip()
                if gem_msg and gem_msg != last_gem_msg:
                    last_gem_msg = gem_msg
                    logger.info(f"🤖 {gem_msg}")
            except Exception:
                gem_msg = ""

            elapsed = int(time.time() - start_time)
            if elapsed >= 30 and not alerted_manual:
                alerted_manual = True
                logger.warning("=" * 50)
                logger.warning(
                    f"⚠️ Captcha chưa qua sau 30s! Vui lòng kiểm tra trên trình duyệt (chờ tối đa {timeout}s)!"
                )
                logger.warning("=" * 50)
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

            if elapsed >= 30 or not gem_msg:
                logger.info(f"⏳ Đang chờ giải captcha... ({elapsed}s / {timeout}s) [{reason}]")

        logger.error(f"⏰ Hết thời gian chờ captcha ({timeout}s)")
        return False
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass

def _open_new_tab(driver: WebDriver, url: str) -> bool:
    """Mở tab mới và navigate tới URL."""
    try:
        # Lưu lại số tab hiện tại
        old_handles = set(driver.window_handles)

        # Cách 1: Dùng JavaScript
        driver.execute_script("window.open('about:blank', '_blank');")
        time.sleep(1)

        new_handles = set(driver.window_handles)
        new_tab = new_handles - old_handles

        if new_tab:
            driver.switch_to.window(new_tab.pop())
            driver.get(url)
            logger.info(f"Đã mở Tab mới và navigate tới: {url}")
        else:
            # Cách 2: Fallback — mở trực tiếp trên tab hiện tại
            logger.warning("Không mở được tab mới, mở trên tab hiện tại")
            driver.get(url)

        # Chờ trang Shopee load (SPA React cần thời gian render)
        time.sleep(5)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        # Chờ thêm cho React render xong
        time.sleep(5)

        return True
    except Exception as e:
        logger.error(f"Lỗi mở tab: {e}")
        return False


def _select_shopee_english_language(driver: WebDriver, region_name: str = "") -> bool:
    """
    Chỉ chạy khi thị trường là 'Shopee MY' hoặc 'Shopee ID' (shopee.com.my / shopee.co.id).
    Shopee.PH mặc định đã là tiếng Anh nên bỏ qua hoàn toàn.
    """
    if region_name == "Shopee.PH":
        return False
    if region_name and region_name not in ("Shopee MY", "Shopee ID"):
        return False

    driver.implicitly_wait(0)
    try:
        cur_url = (driver.current_url or "").lower()
        if "shopee.ph" in cur_url:
            return False
        if "shopee.com.my" not in cur_url and "shopee.co.id" not in cur_url:
            return False

        # 1. Hộp thoại popup chọn ngôn ngữ trên Shopee MY / ID (Pilih bahasa Anda / Select Your Language)
        lang_xpaths = [
            "//*[contains(normalize-space(), 'Pilih bahasa') or contains(normalize-space(), 'Pilih Bahasa') or contains(normalize-space(), 'Select Your Language')]"
            "/following::button[normalize-space()='English'][1]",
            "//*[contains(normalize-space(), 'Pilih bahasa') or contains(normalize-space(), 'Pilih Bahasa') or contains(normalize-space(), 'Select Your Language')]"
            "/following::*[normalize-space()='English' and not(*[normalize-space()='English']) and not(ancestor::footer)][1]",
            "//div[contains(@class, 'language-selection') or contains(@class, 'shopee-popup') or contains(@class, 'modal') or @role='dialog']"
            "//*[self::button or @role='button' or contains(@class, 'item') or contains(@class, 'btn')][normalize-space()='English']",
            "//button[normalize-space()='English' and not(ancestor::footer)]",
        ]
        for xp in lang_xpaths:
            btns = [b for b in driver.find_elements(By.XPATH, xp) if b.is_displayed()]
            if btns:
                btn = btns[0]
                logger.info(f"🌐 Phát hiện hộp thoại chọn ngôn ngữ trên {region_name or 'Shopee MY/ID'} -> Chọn 'English'...")
                if not safe_click(btn, delay=2.0):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(2.0)
                try:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.5)
                except Exception:
                    pass
                logger.info("✅ Đã chọn ngôn ngữ 'English' trên Shopee!")
                return True

        # 2. Dropdown ngôn ngữ trên thanh <header> trang Shopee Affiliate MY / ID (chỉ tìm trong header, không đụng link footer)
        if "affiliate.shopee.com.my" in cur_url or "affiliate.shopee.co.id" in cur_url:
            aff_lang_triggers = [
                e for e in driver.find_elements(
                    By.XPATH,
                    "//header//*[normalize-space()='Bahasa Indonesia' or normalize-space()='Bahasa Malaysia' "
                    "or normalize-space()='Bahasa Melayu'][not(*) and not(self::a[@href])]",
                )
                if e.is_displayed()
            ]
            if aff_lang_triggers:
                logger.info("🌐 Phát hiện ngôn ngữ Header Affiliate (MY/ID) chưa phải English -> Chuyển sang 'English'...")
                try:
                    driver.execute_script("arguments[0].click();", aff_lang_triggers[0])
                    time.sleep(1.0)
                    en_opts = [
                        e for e in driver.find_elements(
                            By.XPATH,
                            "//li[normalize-space()='English' and not(ancestor::footer)] "
                            "| //*[contains(@class, 'dropdown') or contains(@class, 'menu')]//*[normalize-space()='English' and not(self::a[@href])]",
                        )
                        if e.is_displayed()
                    ]
                    if en_opts:
                        driver.execute_script("arguments[0].click();", en_opts[0])
                        time.sleep(2.0)
                        logger.info("✅ Đã chuyển ngôn ngữ trang Affiliate sang 'English'!")
                        return True
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"Lỗi khi kiểm tra hộp thoại ngôn ngữ Shopee: {e}")
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return False


def _click_facebook_login_button(driver: WebDriver, region_name: str = "Shopee.PH") -> bool:
    """
    Tìm và click nút đăng nhập bằng Facebook trên trang Shopee.
    Shopee dùng React + dynamic class names nên phải tìm bằng text/XPath.
    """
    logger.info("Đang tìm nút 'Facebook' trên Shopee Login...")

    _wait_for_captcha(driver)
    if region_name in ("Shopee MY", "Shopee ID"):
        _select_shopee_english_language(driver, region_name)

    if _is_shopee_captcha_or_blocked(driver) or _check_shopee_logged_in_user(driver):
        return False

    # Chờ Facebook JS SDK (window.FB) khởi tạo xong trên trang buyer/login (tối đa 5s)
    for _ in range(10):
        if region_name in ("Shopee MY", "Shopee ID"):
            _select_shopee_english_language(driver, region_name)
        try:
            if driver.execute_script("return typeof window.FB !== 'undefined';"):
                break
        except Exception:
            pass
        time.sleep(0.5)

    if region_name in ("Shopee MY", "Shopee ID"):
        _select_shopee_english_language(driver, region_name)

    selectors = [
        # Ưu tiên thẻ <button> chứa 'Facebook' trước tiên
        (By.XPATH, "//button[contains(., 'Facebook')]"),
        (By.XPATH, "//*[contains(text(), 'Facebook')]"),
        (By.XPATH, "//div[contains(., 'Facebook') and not(descendant::div[contains(., 'Facebook')])]"),
        # Tìm bằng class chứa 'facebook'
        (By.CSS_SELECTOR, "[class*='facebook']"),
        (By.CSS_SELECTOR, "[class*='Facebook']"),
        # Tìm bằng aria/data attributes
        (By.CSS_SELECTOR, "[aria-label*='Facebook']"),
        (By.CSS_SELECTOR, "[data-net='facebook']"),
        # Tìm bằng img/svg icon Facebook
        (By.XPATH, "//img[contains(@src, 'facebook') or contains(@alt, 'Facebook')]/.."),
        (By.XPATH, "//img[contains(@src, 'fb')]/.."),
        # Class cố định (nếu có)
        (By.CSS_SELECTOR, ".social-login-btn--facebook"),
        (By.CSS_SELECTOR, ".social-login__btn--facebook"),
    ]

    for by, value in selectors:
        if _is_shopee_captcha_or_blocked(driver) or _check_shopee_logged_in_user(driver):
            return False
        try:
            btn = wait_for_element(driver, by, value, timeout=5, condition="clickable")
            if btn:
                btn_text = btn.text.strip()
                logger.info(f"Tìm thấy nút Facebook: '{btn_text}' (selector: {value})")
                # Di chuyển chuột tự nhiên qua form đăng nhập trước khi bấm nút Facebook
                try:
                    actions = ActionChains(driver)
                    body_el = driver.find_element(By.TAG_NAME, "body")
                    actions.move_to_element_with_offset(body_el, random.randint(-120, 120), random.randint(-80, 80))
                    actions.pause(random.uniform(0.25, 0.45))
                    actions.move_to_element_with_offset(btn, random.randint(-15, 15), random.randint(-6, 6))
                    actions.pause(random.uniform(0.35, 0.65))
                    actions.click()
                    actions.perform()
                    time.sleep(3.0)
                    logger.info("Đã click nút Facebook (ActionChains)")
                    return True
                except Exception:
                    if safe_click(btn, delay=3.0):
                        logger.info("Đã click nút Facebook")
                        return True
        except Exception:
            continue

    # Cuối cùng: thử tìm bất kỳ element nào có text Facebook và click
    try:
        elements = driver.find_elements(By.XPATH, "//*[text()='Facebook']")
        if not elements:
            elements = driver.find_elements(By.XPATH, "//*[contains(text(),'Facebook')]")
        for el in elements:
            if el.is_displayed():
                logger.info(f"Tìm thấy element text 'Facebook': tag={el.tag_name}")
                el.click()
                time.sleep(3)
                return True
    except Exception as e:
        logger.debug(f"Fallback search failed: {e}")

    logger.error("Không tìm thấy nút 'Facebook' trên trang Shopee")
    return False


def _click_fb_oauth_steps(driver: WebDriver, popup_handle: str) -> bool:
    """
    Xử lý tuần tự tất cả các bước trong popup Facebook OAuth (hỗ trợ Tiếng Việt, Tiếng Anh, ID/MY):
      1. 'Continue as [Name]' / 'Tiếp tục dưới tên [Name]' / 'Tiếp tục với tư cách [Name]' / 'Continue' / 'Tiếp tục'
      2. 'Next' / 'Tiếp' ('Bạn muốn sử dụng Trang nào cùng Shopee?')
      3. 'Done' / 'Xong' / 'Save' / 'Lưu' ('Shopee được phép làm gì?')
      4. 'OK' ('Bạn hiện đã liên kết Shopee với Facebook')
    """
    any_clicked = False
    empty_rounds = 0
    driver.implicitly_wait(0)
    try:
        for step_idx in range(8):
            if popup_handle not in driver.window_handles:
                break
            driver.switch_to.window(popup_handle)
            cur_u = (driver.current_url or "").lower()
            if "facebook.com" not in cur_u:
                break

            step_xpaths = [
                # Bước 1: Continue as / Tiếp tục dưới tên
                (
                    "//*[@role='button' or self::button]["
                    "contains(., 'Continue as') or contains(., 'Tiếp tục dưới tên') or contains(., 'Tiếp tục với tư cách') "
                    "or contains(., 'Lanjutkan sebagai') or contains(., 'Teruskan sebagai') "
                    "or contains(@aria-label, 'Continue as') or contains(@aria-label, 'Tiếp tục dưới tên') or contains(@aria-label, 'Tiếp tục')"
                    "] | //button[@name='__CONFIRM__']"
                ),
                (
                    "//span[contains(text(), 'Continue as') or contains(text(), 'Tiếp tục dưới tên') or contains(text(), 'Tiếp tục với tư cách')]"
                    "/ancestor::*[@role='button' or self::button][1]"
                ),
                # Bước 2: Next / Tiếp
                (
                    "//*[@role='button' or self::button]["
                    "@aria-label='Next' or @aria-label='Tiếp' or @aria-label='Selanjutnya' or @aria-label='Seterusnya' "
                    "or normalize-space()='Next' or normalize-space()='Tiếp' or normalize-space()='Selanjutnya' or normalize-space()='Seterusnya' "
                    "or .//span[normalize-space()='Next' or normalize-space()='Tiếp' or normalize-space()='Selanjutnya' or normalize-space()='Seterusnya']"
                    "]"
                ),
                # Bước 3: Done / Xong / Save / Lưu
                (
                    "//*[@role='button' or self::button]["
                    "@aria-label='Done' or @aria-label='Xong' or @aria-label='Save' or @aria-label='Lưu' or @aria-label='Selesai' "
                    "or normalize-space()='Done' or normalize-space()='Xong' or normalize-space()='Save' or normalize-space()='Lưu' or normalize-space()='Selesai' "
                    "or .//span[normalize-space()='Done' or normalize-space()='Xong' or normalize-space()='Save' or normalize-space()='Lưu' or normalize-space()='Selesai']"
                    "]"
                ),
                # Bước 4: OK
                (
                    "//*[@role='button' or self::button]["
                    "@aria-label='OK' or normalize-space()='OK' or .//span[normalize-space()='OK']"
                    "]"
                ),
                # Fallback: Continue / Tiếp tục (không bấm Cancel / Hủy / Tạo Trang / Quay lại)
                (
                    "//*[@role='button' or self::button][contains(., 'Continue') or contains(., 'Tiếp tục')]"
                    "[not(contains(., 'Cancel')) and not(contains(., 'Hủy')) and not(contains(., 'Back')) and not(contains(., 'Quay lại'))]"
                ),
            ]

            clicked_in_round = False
            for xp in step_xpaths:
                try:
                    els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
                    if els:
                        target_btn = els[0]
                        btn_label = (target_btn.text or target_btn.get_attribute("aria-label") or "FB OAuth button").strip()
                        logger.info(f"Click nút FB OAuth (bước {step_idx + 1}): '{btn_label}'")
                        if not safe_click(target_btn, delay=2.5):
                            driver.execute_script("arguments[0].click();", target_btn)
                            time.sleep(2.5)
                        any_clicked = True
                        clicked_in_round = True
                        empty_rounds = 0
                        break
                except Exception:
                    continue

            if not clicked_in_round:
                empty_rounds += 1
                time.sleep(1.5)
                if popup_handle not in driver.window_handles:
                    break
                if any_clicked and empty_rounds >= 2:
                    break
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return any_clicked


def _handle_facebook_popup(
    driver: WebDriver, fb_uid: str, fb_password: str,
    handles_before_click: set = None, shopee_handle: str = None,
    hotmail_email: str = "", hotmail_password: str = "",
    recovery_email: str = "", new_hotmail_pass: str = "",
) -> bool:
    """
    Xử lý popup Facebook OAuth (consent / đăng nhập / mở khóa 956 nếu xuất hiện trong popup).
    Popup hiện nút "Continue as [Name]" — cần click vào đó.
    """
    logger.info("Đang xử lý popup Facebook...")

    # Lưu handle Shopee
    if not shopee_handle:
        shopee_handle = driver.current_window_handle
    if not handles_before_click:
        handles_before_click = {shopee_handle}

    # Chờ popup MỚI xuất hiện (window chưa có trước khi click)
    fb_popup_handle = None
    for attempt_idx in range(15):
        time.sleep(1)
        current_handles = set(driver.window_handles)
        new_handles = current_handles - handles_before_click
        if new_handles:
            # Tìm popup Facebook trong các window mới
            for handle in new_handles:
                driver.switch_to.window(handle)
                current_url = driver.current_url
                logger.debug(f"Window mới: {current_url}")
                if "facebook.com" in current_url:
                    fb_popup_handle = handle
                    logger.info(f"Phát hiện popup Facebook MỚI: {current_url}")
                    break
            if fb_popup_handle:
                break
        elif attempt_idx in (4, 8):
            # Nếu sau 5s chưa mở popup (do SDK FB vừa mới nạp xong), bấm lại nút <button>Facebook</button>
            try:
                driver.switch_to.window(shopee_handle)
                fb_btns = [
                    b for b in driver.find_elements(By.XPATH, "//button[contains(., 'Facebook')]")
                    if b.is_displayed()
                ]
                if fb_btns:
                    logger.info("Popup FB chưa mở, click lại nút <button>Facebook</button>...")
                    safe_click(fb_btns[0], delay=1.0)
            except Exception:
                pass

    if fb_popup_handle:
        driver.switch_to.window(fb_popup_handle)
        time.sleep(3)

        # Nếu popup Facebook OAuth bị nhảy vào checkpoint 956 ('confirm this is your Meta Account to unlock it')
        if "828281030927956" in (driver.current_url or "").lower():
            from modules.facebook_login import _handle_checkpoint_956
            unlocked = _handle_checkpoint_956(
                driver, hotmail_email, hotmail_password, recovery_email, new_hotmail_pass
            )
            if unlocked:
                # Nếu sau khi mở khóa 956, popup chuyển về trang chủ FB thay vì màn hình OAuth consent -> đóng popup & bấm lại nút FB trên Shopee
                if fb_popup_handle in driver.window_handles:
                    driver.switch_to.window(fb_popup_handle)
                    u_after = (driver.current_url or "").lower()
                    if "oauth" not in u_after and "privacy/consent" not in u_after:
                        try:
                            driver.close()
                        except Exception:
                            pass
                        driver.switch_to.window(shopee_handle)
                        time.sleep(1.5)
                        handles_retry = set(driver.window_handles)
                        if _click_facebook_login_button(driver):
                            return _handle_facebook_popup(
                                driver, fb_uid, fb_password, handles_retry, shopee_handle,
                                hotmail_email, hotmail_password, recovery_email, new_hotmail_pass,
                            )

        # Kiểm tra cần nhập credentials không
        email_input = wait_for_element(
            driver, By.ID, "email", timeout=3, condition="visible",
        )
        if email_input:
            logger.info("Popup FB yêu cầu đăng nhập")
            safe_send_keys(email_input, fb_uid, delay=0.5)
            pass_input = wait_for_element(
                driver, By.ID, "pass", timeout=3, condition="visible",
            )
            if pass_input:
                safe_send_keys(pass_input, fb_password, delay=0.5)
            login_btn = wait_for_element(
                driver, By.CSS_SELECTOR,
                "button[name='login'], #loginbutton",
                timeout=3, condition="clickable",
            )
            if login_btn:
                safe_click(login_btn, delay=5.0)
                time.sleep(3)

        # Xử lý tuần tự các bước trong popup Facebook OAuth (Continue as / Tiếp tục dưới tên -> Tiếp -> Xong -> OK)
        _click_fb_oauth_steps(driver, fb_popup_handle)

        # Chờ popup tự đóng (tối đa 10s)
        for _ in range(10):
            if fb_popup_handle not in driver.window_handles:
                logger.info("Popup Facebook đã tự đóng.")
                break
            time.sleep(1)

        # Quay lại tab Shopee
        try:
            driver.switch_to.window(shopee_handle)
            logger.info("Đã quay lại tab Shopee")
        except Exception:
            # Popup đã đóng, switch về handle còn lại
            for h in driver.window_handles:
                driver.switch_to.window(h)
                if "shopee" in driver.current_url.lower():
                    break

    else:
        # Không có popup → có thể redirect flow hoặc tự động approve
        logger.info("Không có popup FB riêng, kiểm tra redirect...")
        time.sleep(5)

        # Quay lại tab Shopee
        time.sleep(3)
        handles = driver.window_handles
        for handle in handles:
            driver.switch_to.window(handle)
            if "shopee" in driver.current_url.lower():
                logger.info("Đã quay lại tab Shopee")
                break

    return True


def _get_existing_tempmail_ids(tempmail_address: str) -> set:
    """Lấy danh sách ID các email hiện có trên cả nguyenmaucole.io.vn và fviainboxes.com để tránh đọc lại code/link cũ."""
    addr = (tempmail_address or "").strip().lower()
    if not addr or "@" not in addr:
        return set()
    prefix, dom = addr.split("@", 1)
    seen = set()

    # 1. Snapshot nguyenmaucole.io.vn
    nmc_addr = f"{prefix}@nguyenmaucole.io.vn"
    nmc_url = f"https://tempmail-backend.thincole.workers.dev/api/emails?address={nmc_addr}"
    try:
        req = urllib.request.Request(nmc_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(data, list):
            for m in data:
                if isinstance(m, dict) and m.get("id") is not None:
                    seen.add(f"nmc:{m.get('id')}")
                    seen.add(m.get("id"))
    except Exception:
        pass

    # 2. Snapshot fviainboxes.com
    fvia_dom = dom if "fvia" in dom or "dropinboxes" in dom else "fviainboxes.com"
    try:
        resp = requests.get(
            "https://fviainboxes.com/messages",
            params={"username": prefix, "domain": fvia_dom},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        if resp.status_code == 200:
            for msg in (resp.json().get("result") or []):
                if isinstance(msg, dict) and msg.get("id") is not None:
                    seen.add(f"fvia:{msg.get('id')}")
    except Exception:
        pass

    return seen


def _check_nmc_for_shopee_otp(prefix: str, seen_ids: set) -> str:
    """Kiểm tra hộp thư nguyenmaucole.io.vn tìm mã OTP 6 chữ số của Shopee."""
    nmc_addr = f"{prefix}@nguyenmaucole.io.vn"
    api_url = f"https://tempmail-backend.thincole.workers.dev/api/emails?address={nmc_addr}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(data, list):
            for m in data:
                if not isinstance(m, dict):
                    continue
                msg_id = m.get("id")
                if msg_id in seen_ids or f"nmc:{msg_id}" in seen_ids:
                    continue
                subj = (m.get("subject") or "").lower()
                sender = (m.get("sender") or "").lower()
                if "shopee" in subj or "shopee" in sender or "verification" in subj or "kode" in subj:
                    body_html = m.get("body_html") or ""
                    text = re.sub(r"<[^>]+>", " ", body_html)
                    text = re.sub(r"\s+", " ", text)
                    codes = re.findall(r"\b(\d{6})\b", text)
                    if codes:
                        logger.info(f"✅ Đã lấy được mã OTP Shopee từ {nmc_addr}: {codes[0]}")
                        return codes[0]
                seen_ids.add(f"nmc:{msg_id}")
    except Exception as e:
        logger.debug(f"Lỗi gọi API nguyenmaucole.io.vn: {e}")
    return ""


def _check_fvia_for_shopee_otp(prefix: str, fvia_dom: str, seen_ids: set) -> Tuple[str, bool]:
    """
    Kiểm tra hộp thư fviainboxes.com tìm mã OTP 6 chữ số của Shopee.
    Trả về (otp_code, need_browser_turnstile).
    """
    full_addr = f"{prefix}@{fvia_dom}"
    try:
        resp = requests.get(
            "https://fviainboxes.com/messages",
            params={"username": prefix, "domain": fvia_dom},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 403:
            return "", True
        if resp.status_code == 200:
            messages = resp.json().get("result") or []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id", ""))
                if not msg_id or f"fvia:{msg_id}" in seen_ids or msg_id in seen_ids:
                    continue
                subj = str(msg.get("subject") or "").lower()
                sender = str(msg.get("from") or msg.get("sender") or "").lower()
                if "shopee" in subj or "shopee" in sender or "verification" in subj or "kode" in subj or "code" in subj:
                    body_resp = requests.get(
                        "https://fviainboxes.com/message",
                        params={"username": prefix, "domain": fvia_dom, "id": msg_id},
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
                    text = re.sub(r"<[^>]+>", " ", body_html)
                    text = re.sub(r"\s+", " ", text)
                    codes = re.findall(r"\b(\d{6})\b", text + " " + subj)
                    if codes:
                        logger.info(f"✅ Đã lấy được mã OTP Shopee từ {full_addr}: {codes[0]}")
                        return codes[0], False
                seen_ids.add(f"fvia:{msg_id}")
    except Exception as e:
        logger.debug(f"Lỗi gọi API fviainboxes.com: {e}")
    return "", False


def _fetch_fvia_browser_content(driver: WebDriver, prefix: str, fvia_dom: str, mode: str = "otp") -> str:
    """Fallback mở tab https://fviainboxes.com/ trên trình duyệt để lấy mã OTP hoặc link Shopee khi API bật Turnstile."""
    logger.info(f"Mở tab https://fviainboxes.com/ trên trình duyệt để lấy {mode.upper()} Shopee cho {prefix}@{fvia_dom}...")
    main_handle = driver.current_window_handle
    try:
        driver.switch_to.new_window("tab")
        driver.get("https://fviainboxes.com/")
        wait_for_page_load(driver, timeout=15)
        time.sleep(2)

        user_inp = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "input#username, input[name='username'], input[placeholder*='username']",
            timeout=10,
            condition="visible",
        )
        if user_inp:
            safe_send_keys(user_inp, prefix, delay=0.5)
            try:
                domain_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "button[id^='headlessui-listbox-button'], button[aria-haspopup='listbox']",
                    timeout=3,
                    condition="clickable",
                )
                if domain_btn and fvia_dom not in (domain_btn.text or "").lower():
                    if not safe_click(domain_btn, delay=0.8):
                        driver.execute_script("arguments[0].click();", domain_btn)
                        time.sleep(0.8)
                    opt = wait_for_element(
                        driver,
                        By.XPATH,
                        f"//li[contains(normalize-space(), '{fvia_dom}')] | //*[@role='option'][contains(normalize-space(), '{fvia_dom}')]",
                        timeout=3,
                        condition="clickable",
                    )
                    if opt:
                        if not safe_click(opt, delay=0.8):
                            driver.execute_script("arguments[0].click();", opt)
                            time.sleep(0.8)
            except Exception:
                pass

            get_btn = wait_for_element(driver, By.XPATH, "//button[contains(., 'Get Email')]", timeout=5, condition="clickable")
            if get_btn:
                safe_click(get_btn, delay=3.0)

            for _ in range(10):
                mail_items = [
                    el for el in driver.find_elements(By.CSS_SELECTOR, "ul li.group, main ul li")
                    if el.is_displayed() and ("shopee" in (el.text or "").lower() or "verification" in (el.text or "").lower())
                ]
                target_item = mail_items[0] if mail_items else wait_for_element(
                    driver, By.CSS_SELECTOR, "ul li.group, main ul li", timeout=3, condition="clickable"
                )
                if target_item:
                    safe_click(target_item, delay=2.0)
                    iframe_el = wait_for_element(
                        driver, By.CSS_SELECTOR, "iframe[title='Email content'], article iframe, iframe",
                        timeout=5, condition="presence",
                    )
                    if iframe_el:
                        srcdoc = iframe_el.get_attribute("srcdoc") or ""
                        if not srcdoc:
                            try:
                                driver.switch_to.frame(iframe_el)
                                srcdoc = driver.page_source or driver.find_element(By.TAG_NAME, "body").text
                                driver.switch_to.default_content()
                            except Exception:
                                driver.switch_to.default_content()
                        if mode == "link":
                            links = re.findall(r'href=["\'](https?://[^"\']+)["\']', srcdoc)
                            for raw_link in links:
                                link = html.unescape(raw_link)
                                if "shp.ee/dlink" in link or "verify/email-link" in link or "dlink" in link:
                                    logger.info(f"✅ Đã lấy được link xác nhận Shopee từ UI fviainboxes.com: {link}")
                                    return link
                        else:
                            text_clean = re.sub(r"<[^>]+>", " ", srcdoc)
                            codes = re.findall(r"\b(\d{6})\b", text_clean)
                            if codes:
                                logger.info(f"✅ Đã lấy được mã OTP Shopee từ UI fviainboxes.com: {codes[0]}")
                                return codes[0]
                refresh_btn = wait_for_element(driver, By.XPATH, "//button[contains(., 'Get Email')]", timeout=2, condition="clickable")
                if refresh_btn:
                    safe_click(refresh_btn, delay=3.0)
                else:
                    time.sleep(3)
    except Exception as e:
        logger.warning(f"Fallback UI fviainboxes.com lỗi: {e}")
    finally:
        try:
            if driver.current_window_handle != main_handle and len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(main_handle)
        except Exception:
            pass
    return ""


def _fetch_shopee_email_otp(
    driver: WebDriver,
    tempmail_address: str,
    seen_ids: set = None,
    timeout: int = 180,
) -> str:
    """
    Lấy mã OTP 6 chữ số của Shopee từ hộp thư chuyển tiếp (@nguyenmaucole.io.vn hoặc @fviainboxes.com).
    Tự động kiểm tra cả domain chính lẫn domain phụ để tránh trượt mã nếu mail được forward sang bên còn lại.
    """
    addr = tempmail_address.strip().lower()
    prefix, dom = addr.split("@", 1) if "@" in addr else (addr, "nguyenmaucole.io.vn")
    is_fvia_primary = "fvia" in dom or "dropinboxes" in dom
    fvia_dom = dom if is_fvia_primary else "fviainboxes.com"
    if seen_ids is None:
        seen_ids = set()

    logger.info(f"Đang lấy mã OTP Shopee từ {addr} (tối đa {timeout}s)...")
    start_time = time.time()
    fvia_blocked = False

    while time.time() - start_time < timeout:
        check_browser_alive(driver)
        if is_fvia_primary:
            if not fvia_blocked:
                code, blocked = _check_fvia_for_shopee_otp(prefix, fvia_dom, seen_ids)
                if code:
                    return code
                if blocked:
                    fvia_blocked = True
                    # Chờ 8s cho thư kịp chuyển tiếp tới rồi mở UI fviainboxes.com lấy trực tiếp
                    if time.time() - start_time >= 8:
                        ui_code = _fetch_fvia_browser_content(driver, prefix, fvia_dom, mode="otp")
                        if ui_code:
                            return ui_code
            code_alt = _check_nmc_for_shopee_otp(prefix, seen_ids)
            if code_alt:
                return code_alt
        else:
            code = _check_nmc_for_shopee_otp(prefix, seen_ids)
            if code:
                return code
            if not fvia_blocked:
                code_alt, blocked = _check_fvia_for_shopee_otp(prefix, fvia_dom, seen_ids)
                if code_alt:
                    return code_alt
                if blocked:
                    fvia_blocked = True

        elapsed = int(time.time() - start_time)
        if elapsed > 0 and elapsed % 9 == 0:
            logger.info(f"Đang chờ email OTP Shopee gửi về {addr}... ({elapsed}s/{timeout}s)")
        time.sleep(3)

    if is_fvia_primary:
        return _fetch_fvia_browser_content(driver, prefix, fvia_dom, mode="otp")

    # Fallback: mở tab https://mail.nguyenmaucole.io.vn trên trình duyệt
    logger.info("Thử mở tab https://mail.nguyenmaucole.io.vn trên trình duyệt để lấy code...")
    main_handle = driver.current_window_handle
    try:
        driver.switch_to.new_window("tab")
        driver.get("https://mail.nguyenmaucole.io.vn")
        time.sleep(4)
        inp = wait_for_element(driver, By.CSS_SELECTOR, "input[type='text'], input", timeout=5, condition="visible")
        if inp:
            inp.clear()
            inp.send_keys(prefix)
            btns = driver.find_elements(By.TAG_NAME, "button")
            for b in btns:
                if b.is_displayed():
                    b.click()
                    break
            time.sleep(4)
            body_text = driver.find_element(By.TAG_NAME, "body").text
            codes = re.findall(r"\b(\d{6})\b", body_text)
            if codes:
                logger.info(f"✅ Đã lấy được mã OTP Shopee từ UI trình duyệt: {codes[0]}")
                return codes[0]
    except Exception as e:
        logger.warning(f"Fallback UI mail.nguyenmaucole.io.vn lỗi: {e}")
    finally:
        try:
            if driver.current_window_handle != main_handle and len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(main_handle)
        except Exception:
            pass

    return ""


def _get_affiliate_account_setting(driver: WebDriver, affiliate_url: str) -> Tuple[str, str]:
    """
    Truy cập https://affiliate.shopee.ph/account_setting và lấy Username (Cột F) + Affiliate ID (Cột G).
    """
    setting_url = f"{affiliate_url.rstrip('/')}/account_setting"
    logger.info(f"Truy cập trang Account Setting: {setting_url}")
    driver.implicitly_wait(0)
    try:
        driver.get(setting_url)
        time.sleep(3)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)

        username = ""
        affiliate_id = ""
        for attempt in range(12):
            try:
                if "account_setting" not in (driver.current_url or "").lower() and attempt == 4:
                    driver.get(setting_url)
                    time.sleep(3)

                if not username:
                    u_els = driver.find_elements(
                        By.XPATH,
                        "//*[normalize-space()='Username']/following::input[1] | //label[contains(normalize-space(), 'Username')]/following::input[1]",
                    )
                    for el in u_els:
                        val = (el.get_attribute("value") or driver.execute_script("return arguments[0].value || '';", el) or "").strip()
                        if val:
                            username = val
                            break

                if not affiliate_id:
                    a_els = driver.find_elements(
                        By.XPATH,
                        "//*[normalize-space()='Affiliate ID']/following::input[1] | //label[contains(normalize-space(), 'Affiliate ID')]/following::input[1]",
                    )
                    for el in a_els:
                        val = (el.get_attribute("value") or driver.execute_script("return arguments[0].value || '';", el) or "").strip()
                        if val:
                            affiliate_id = val
                            break

                if username and affiliate_id:
                    break
            except Exception:
                pass
            time.sleep(1.5)

        logger.info(f"✅ Account Setting -> Username: '{username}', Affiliate ID: '{affiliate_id}'")
        return username, affiliate_id
    except Exception as e:
        logger.error(f"Lỗi khi lấy thông tin từ {setting_url}: {e}")
        return "", ""
    finally:
        driver.implicitly_wait(5)


def _check_nmc_for_shopee_link(prefix: str, seen_ids: set) -> str:
    """Kiểm tra hộp thư nguyenmaucole.io.vn tìm link xác nhận Shopee."""
    nmc_addr = f"{prefix}@nguyenmaucole.io.vn"
    api_url = f"https://tempmail-backend.thincole.workers.dev/api/emails?address={nmc_addr}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(data, list):
            for m in data:
                if not isinstance(m, dict):
                    continue
                msg_id = m.get("id")
                if msg_id in seen_ids or f"nmc:{msg_id}" in seen_ids:
                    continue
                subj = (m.get("subject") or "").lower()
                sender = (m.get("sender") or "").lower()
                if "shopee" in subj or "shopee" in sender or "verification" in subj or "password" in subj or "verifikasi" in subj:
                    body_html = m.get("body_html") or ""
                    links = re.findall(r'href=["\'](https?://[^"\']+)["\']', body_html)
                    for raw_link in links:
                        link = html.unescape(raw_link)
                        if "shp.ee/dlink" in link or "verify/email-link" in link or "dlink" in link:
                            logger.info(f"✅ Đã lấy được link xác nhận Shopee từ {nmc_addr}: {link}")
                            return link
                seen_ids.add(f"nmc:{msg_id}")
    except Exception as e:
        logger.debug(f"Lỗi gọi API nguyenmaucole.io.vn tìm link: {e}")
    return ""


def _check_fvia_for_shopee_link(prefix: str, fvia_dom: str, seen_ids: set) -> Tuple[str, bool]:
    """Kiểm tra hộp thư fviainboxes.com tìm link xác nhận Shopee."""
    full_addr = f"{prefix}@{fvia_dom}"
    try:
        resp = requests.get(
            "https://fviainboxes.com/messages",
            params={"username": prefix, "domain": fvia_dom},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 403:
            return "", True
        if resp.status_code == 200:
            messages = resp.json().get("result") or []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id", ""))
                if not msg_id or f"fvia:{msg_id}" in seen_ids or msg_id in seen_ids:
                    continue
                subj = str(msg.get("subject") or "").lower()
                sender = str(msg.get("from") or msg.get("sender") or "").lower()
                if "shopee" in subj or "shopee" in sender or "verification" in subj or "password" in subj or "verifikasi" in subj:
                    body_resp = requests.get(
                        "https://fviainboxes.com/message",
                        params={"username": prefix, "domain": fvia_dom, "id": msg_id},
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
                    links = re.findall(r'href=["\'](https?://[^"\']+)["\']', body_html)
                    for raw_link in links:
                        link = html.unescape(raw_link)
                        if "shp.ee/dlink" in link or "verify/email-link" in link or "dlink" in link:
                            logger.info(f"✅ Đã lấy được link xác nhận Shopee từ {full_addr}: {link}")
                            return link, False
                seen_ids.add(f"fvia:{msg_id}")
    except Exception as e:
        logger.debug(f"Lỗi gọi API fviainboxes.com tìm link: {e}")
    return "", False


def _fetch_shopee_email_link(
    driver: WebDriver,
    tempmail_address: str,
    seen_ids: set = None,
    timeout: int = 180,
) -> str:
    """
    Lấy link xác nhận (Verify by Email Link) của Shopee từ @nguyenmaucole.io.vn hoặc @fviainboxes.com.
    Ví dụ: <a href="https://ph.shp.ee/dlink/..." ...>here</a> to confirm.
    """
    addr = tempmail_address.strip().lower()
    prefix, dom = addr.split("@", 1) if "@" in addr else (addr, "nguyenmaucole.io.vn")
    is_fvia_primary = "fvia" in dom or "dropinboxes" in dom
    fvia_dom = dom if is_fvia_primary else "fviainboxes.com"
    if seen_ids is None:
        seen_ids = set()

    logger.info(f"Đang chờ link xác nhận Shopee gửi về {addr} (tối đa {timeout}s)...")
    start_time = time.time()
    fvia_blocked = False

    while time.time() - start_time < timeout:
        check_browser_alive(driver)
        if is_fvia_primary:
            if not fvia_blocked:
                link, blocked = _check_fvia_for_shopee_link(prefix, fvia_dom, seen_ids)
                if link:
                    return link
                if blocked:
                    fvia_blocked = True
                    if time.time() - start_time >= 8:
                        ui_link = _fetch_fvia_browser_content(driver, prefix, fvia_dom, mode="link")
                        if ui_link:
                            return ui_link
            link_alt = _check_nmc_for_shopee_link(prefix, seen_ids)
            if link_alt:
                return link_alt
        else:
            link = _check_nmc_for_shopee_link(prefix, seen_ids)
            if link:
                return link
            if not fvia_blocked:
                link_alt, blocked = _check_fvia_for_shopee_link(prefix, fvia_dom, seen_ids)
                if link_alt:
                    return link_alt
                if blocked:
                    fvia_blocked = True

        elapsed = int(time.time() - start_time)
        if elapsed > 0 and elapsed % 9 == 0:
            logger.info(f"Đang chờ email chứa link xác nhận Shopee tại {addr}... ({elapsed}s/{timeout}s)")
        time.sleep(3)

    if is_fvia_primary:
        return _fetch_fvia_browser_content(driver, prefix, fvia_dom, mode="link")

    return ""


def _enable_shopee_antibot_bypass(driver: WebDriver) -> None:
    """
    Chặn các request nền không cần thiết trên thanh điều hướng & trang chủ Shopee
    (flash_sale, search_prefills, search_suggestion, cart/mini, recommend, homepage, banner)
    vốn bị Shopee trả về mã lỗi 90309999 (app_key=FlashSale.PC / Cart.PC / Search, scene=crawler_item)
    và tự động ép chuyển hướng toàn trang sang /verify/traffic -> /verify/captcha.
    """
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*api/v4/flash_sale/*",
                    "*api/v2/flash_sale/*",
                    "*api/v4/search/search_prefills*",
                    "*api/v4/search/search_suggestion*",
                    "*api/v4/search/*",
                    "*api/v4/cart/mini*",
                    "*api/v4/pages/get_popular_search*",
                    "*api/v4/pages/get_category_tree*",
                    "*api/v4/recommend/*",
                    "*api/v4/homepage/*",
                    "*api/v4/banner/*",
                ]
            },
        )
    except Exception:
        pass


def _switch_to_shopee_tab(driver: WebDriver) -> None:
    """Chuyển về tab Shopee đang mở (nếu có), hoặc mở tab mới nếu tab hiện tại là Facebook/Hotmail."""
    try:
        cur_u = (driver.current_url or "").lower()
        if "shopee." in cur_u:
            return
        for h in driver.window_handles:
            try:
                driver.switch_to.window(h)
                u = (driver.current_url or "").lower()
                if "shopee." in u:
                    return
            except Exception:
                continue
        if any(k in cur_u for k in ("facebook.com", "live.com", "microsoft.com", "office.com")):
            try:
                driver.switch_to.new_window("tab")
            except Exception:
                driver.execute_script("window.open('about:blank', '_blank');")
                time.sleep(0.5)
                driver.switch_to.window(driver.window_handles[-1])
    except Exception:
        pass


def change_shopee_password(
    driver: WebDriver,
    shopee_url: str,
    new_shopee_password: str,
    hotmail_email: str,
    forwarding_domain: str = "nguyenmaucole.io.vn",
) -> bool:
    """
    Bước 6: Đổi mật khẩu Shopee:
    1. Truy cập https://shopee.ph/user/account/password (hoặc domain tương ứng).
    2. Nếu hiện Security check -> click 'Verify by Email Link'.
    3. Lấy link xác nhận từ <prefix>@<forwarding_domain> -> mở link trong tab mới để approve.
    4. Quay lại tab chính (tự chuyển về /user/account/password), điền mật khẩu mới Shopee vào
       ô New Password + Confirm Password -> click Confirm.
    """
    if not new_shopee_password:
        new_shopee_password = getattr(config, "NEW_SHOPEE_PASSWORD", "Khoa01@c")

    # Lấy base domain từ shopee_url (VD: https://shopee.ph/buyer/login -> https://shopee.ph)
    base_domain = "https://shopee.ph"
    m_dom = re.match(r"^(https?://[^/]+)", shopee_url.strip())
    if m_dom:
        base_domain = m_dom.group(1)

    password_url = f"{base_domain}/user/account/password"
    logger.info(f"=== Đặt mật khẩu mới Shopee ({password_url}) ===")

    try:
        _switch_to_shopee_tab(driver)
        _enable_shopee_antibot_bypass(driver)

        email_prefix = (hotmail_email or "").split("@")[0].strip().lower()
        fwd_dom = (forwarding_domain or "nguyenmaucole.io.vn").strip().lstrip("@").lower()
        tempmail_address = f"{email_prefix}@{fwd_dom}" if email_prefix else ""
        seen_ids = _get_existing_tempmail_ids(tempmail_address) if tempmail_address else set()

        is_my_or_id = "shopee.com.my" in base_domain.lower() or "shopee.co.id" in base_domain.lower()
        try:
            driver.get(password_url)
        except TimeoutException:
            logger.warning(f"Trang {password_url} tải chậm quá timeout -> dừng nạp phụ và tiếp tục kiểm tra...")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        time.sleep(5)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        if is_my_or_id:
            _select_shopee_english_language(driver)
        _wait_for_captcha(driver)
        if is_my_or_id:
            _select_shopee_english_language(driver)

        # Kiểm tra nút "Verify by Email Link" trên trang Security check (/verify/ivs)
        verify_link_selectors = [
            (By.XPATH, "//button[contains(., 'Verify by Email Link')]"),
            (By.XPATH, "//*[contains(text(), 'Verify by Email Link')]"),
        ]
        clicked_verify_link = False
        for by, value in verify_link_selectors:
            try:
                vbtn = wait_for_element(driver, by, value, timeout=12, condition="clickable")
                if vbtn:
                    seen_ids = _get_existing_tempmail_ids(tempmail_address) if tempmail_address else set()
                    safe_click(vbtn, delay=3.0)
                    logger.info("✅ Đã click 'Verify by Email Link'")
                    clicked_verify_link = True
                    break
            except Exception:
                continue

        if not clicked_verify_link:
            try:
                body_txt = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
                if "Verify by Password" in body_txt and "Verify by Email Link" not in body_txt:
                    logger.info("ℹ️ Tài khoản Shopee đã được đặt mật khẩu từ trước (hiện Verify by Password)!")
                    return True
            except Exception:
                pass

        if clicked_verify_link and tempmail_address:
            confirm_link = _fetch_shopee_email_link(driver, tempmail_address, seen_ids=seen_ids, timeout=180)
            if confirm_link:
                main_handle = driver.current_window_handle
                try:
                    driver.switch_to.new_window("tab")
                    _enable_shopee_antibot_bypass(driver)
                    logger.info(f"Mở link xác nhận Shopee trên tab mới: {confirm_link}")
                    driver.get(confirm_link)
                    time.sleep(5)
                    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                    _wait_for_captcha(driver)
                    time.sleep(3)
                except Exception as e:
                    logger.warning(f"Lỗi khi mở tab xác nhận link Shopee: {e}")
                finally:
                    try:
                        if driver.current_window_handle != main_handle and len(driver.window_handles) > 1:
                            driver.close()
                        driver.switch_to.window(main_handle)
                    except Exception:
                        pass

                logger.info("Đã xác nhận link Email! Chờ trang chính tự chuyển về Set Password...")
                for _ in range(10):
                    _wait_for_captcha(driver)
                    cur_u = (driver.current_url or "").lower()
                    if "user/account/password" in cur_u and "verify/" not in cur_u:
                        break
                    driver.implicitly_wait(0)
                    try:
                        if any(
                            el.is_displayed()
                            for el in driver.find_elements(By.CSS_SELECTOR, "input[name='newPassword'], input[type='password']")
                        ):
                            break
                    except Exception:
                        pass
                    finally:
                        driver.implicitly_wait(5)
                    time.sleep(2)

                cur_u = (driver.current_url or "").lower()
                if "user/account/password" not in cur_u or "verify/" in cur_u:
                    driver.get(password_url)
                    time.sleep(4)
                    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                    _wait_for_captcha(driver)
            else:
                logger.error(f"Không lấy được link xác nhận Shopee từ {tempmail_address}")
                return False

        _wait_for_captcha(driver)
        # Điền mật khẩu mới Shopee vào New Password & Confirm Password
        wait_for_element(
            driver, By.CSS_SELECTOR, "input[name='newPassword'], input[type='password']", timeout=12, condition="visible"
        )
        pwd_els = [el for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if el.is_displayed()]
        if len(pwd_els) >= 2:
            inp1, inp2 = pwd_els[0], pwd_els[1]
        else:
            inp1 = wait_for_element(
                driver, By.CSS_SELECTOR, "input[name='newPassword'], input[type='password']", timeout=5, condition="visible"
            )
            inp2 = wait_for_element(
                driver, By.CSS_SELECTOR, "input[name='newPasswordRepeat']", timeout=3, condition="visible"
            )
        if not inp1 or not inp2:
            logger.error("Không tìm thấy ô nhập New Password trên Shopee")
            return False

        inp1.click()
        inp1.clear()
        safe_send_keys(inp1, new_shopee_password, delay=0.5)

        inp2.click()
        inp2.clear()
        safe_send_keys(inp2, new_shopee_password, delay=0.5)
        logger.info("✅ Đã điền mật khẩu mới Shopee vào 2 ô")
        time.sleep(1)

        confirm_btn_selectors = [
            (By.XPATH, "//button[normalize-space()='confirm' or normalize-space()='Confirm']"),
            (By.CSS_SELECTOR, "button.btn-solid-primary.btn--m"),
            (By.XPATH, "//button[contains(., 'Confirm') or contains(., 'confirm')]"),
        ]
        for by, value in confirm_btn_selectors:
            try:
                cbtn = wait_for_element(driver, by, value, timeout=5, condition="clickable")
                if cbtn:
                    safe_click(cbtn, delay=4.0)
                    logger.info("✅ Đã click 'Confirm' lưu mật khẩu mới Shopee thành công!")
                    return True
            except Exception:
                continue

        logger.error("Không click được nút Confirm lưu mật khẩu Shopee")
        return False
    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi khi đặt mật khẩu mới Shopee: {e}")
        return False


def _register_affiliate(
    driver: WebDriver, affiliate_url: str, fb_profile_url: str,
    fb_uid: str, fb_password: str, region_name: str,
    hotmail_email: str = "",
    forwarding_domain: str = "nguyenmaucole.io.vn",
) -> Tuple[str, str]:
    """
    Đăng ký Shopee Affiliate:
    1. Truy cập trang affiliate
    2. Click "+ Add" Facebook → xử lý popup FB OAuth (Continue as... → Next)
    3. Điền link FB vào hộp thoại "Add Facebook" → click Confirm
    4. Điền số điện thoại → click "Verify & Submit"
    5. Chọn "Verify with Email" → click "Confirm"
    6. Lấy mã OTP từ <prefix>@<forwarding_domain> → điền mã → click "Next"
    7. Vào /account_setting lấy Username và Affiliate ID.
    """
    logger.info(f"=== Đăng ký Affiliate {region_name} ===")

    try:
        _enable_shopee_antibot_bypass(driver)
        # 1. Truy cập trang affiliate
        logger.info(f"Truy cập ngay trang Affiliate: {affiliate_url}")
        try:
            driver.get(affiliate_url)
        except TimeoutException:
            logger.warning(f"Trang Affiliate ({affiliate_url}) tải chậm quá timeout -> dừng nạp tài nguyên phụ và tiếp tục...")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        time.sleep(4)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(2)
        if region_name in ("Shopee MY", "Shopee ID"):
            _select_shopee_english_language(driver, region_name)
        logger.info(f"Đã mở trang Affiliate: {driver.current_url}")

        def _click_enter_if_present() -> bool:
            enter_selectors = [
                (By.CSS_SELECTOR, "a.home-btn"),
                (By.XPATH, "//a[normalize-space()='Enter' or normalize-space()='ENTER']"),
                (By.XPATH, "//button[normalize-space()='Enter' or normalize-space()='ENTER']"),
                (By.XPATH, "//*[normalize-space()='Enter'][contains(@class, 'btn') or @role='button']"),
            ]
            driver.implicitly_wait(0)
            try:
                for by, val in enter_selectors:
                    els = [e for e in driver.find_elements(by, val) if e.is_displayed()]
                    if els:
                        el = els[0]
                        logger.info(f"Tìm thấy nút 'Enter' trên trang Affiliate ({val}) -> Click...")
                        if not safe_click(el, delay=3.0):
                            driver.execute_script("arguments[0].click();", el)
                            time.sleep(3.0)
                        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                        time.sleep(2)
                        return True
            except Exception as e:
                logger.debug(f"Lỗi khi kiểm tra nút Enter: {e}")
            finally:
                driver.implicitly_wait(5)
            return False

        # Nếu hiện nút "Enter" ở trang giới thiệu Affiliate thì bấm "Enter"
        _click_enter_if_present()
        if region_name in ("Shopee MY", "Shopee ID"):
            _select_shopee_english_language(driver, region_name)

        if _is_shopee_phone_verification(driver):
            logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
            raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")

        # Nếu truy cập Affiliate (hoặc sau khi bấm Enter) bị redirect về buyer/login, thực hiện login FB ngay tại đây
        if "buyer/login" in (driver.current_url or "").lower():
            logger.info("Trang Affiliate yêu cầu đăng nhập Shopee -> Thực hiện login bằng Facebook...")
            handles_before_login = set(driver.window_handles)
            login_handle = driver.current_window_handle
            if _click_facebook_login_button(driver, region_name):
                _handle_facebook_popup(driver, fb_uid, fb_password, handles_before_login, login_handle)
                time.sleep(2)
                if _is_shopee_phone_verification(driver):
                    logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' sau khi login FB -> Bỏ qua luồng này!")
                    raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
                _wait_for_captcha(driver)
                time.sleep(2)
                if _is_shopee_phone_verification(driver):
                    logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' sau khi giải Captcha -> Bỏ qua luồng này!")
                    raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
                if "affiliate.shopee" not in (driver.current_url or "").lower():
                    driver.get(affiliate_url)
                    time.sleep(4)
                    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                    time.sleep(2)
                # Nếu quay lại Affiliate mà vẫn còn nút Enter thì bấm tiếp
                _click_enter_if_present()

        # Kiểm tra xác minh SĐT hoặc captcha
        if _is_shopee_phone_verification(driver):
            logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
            raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
        _wait_for_captcha(driver)

        # Kiểm tra nếu tài khoản đã đăng ký Affiliate rồi (ví dụ step=3 hoặc dashboard)
        body_text = ""
        try:
            body_text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        except Exception:
            pass

        cur_aff_url = (driver.current_url or "").lower()
        if (
            "registration submitted successfully" in body_text.lower()
            or "step=3" in cur_aff_url
            or "/dashboard" in cur_aff_url
            or "/offer" in cur_aff_url
        ):
            logger.info("Tài khoản đã hoàn tất đăng ký Affiliate trước đó! Chuyển sang lấy Account Setting...")
            return _get_affiliate_account_setting(driver, affiliate_url)

        # Lưu handles TRƯỚC khi click "+ Add" Facebook vì popup FB OAuth sẽ mở ngay khi click!
        handles_before = set(driver.window_handles)
        affiliate_handle = driver.current_window_handle

        # 2. Click "+ Add" bên cạnh Facebook
        logger.info("Đang tìm nút '+ Add' Facebook...")
        fb_add_clicked = False

        fb_add_selectors = [
            (By.XPATH, "//*[normalize-space()='Facebook']/following::*[normalize-space()='Add'][1]"),
            (By.XPATH, "//div[contains(., 'Facebook') and not(contains(., 'Instagram'))]//*[contains(text(), 'Add')]"),
            (By.XPATH, "//*[contains(text(), 'Facebook')]/ancestor::div[1]//*[contains(text(), 'Add')]"),
            (By.XPATH, "//*[contains(text(), 'Facebook')]/following::*[contains(text(), 'Add')][1]"),
            (By.XPATH, "//span[contains(text(), 'Facebook')]/ancestor::div[contains(@class, 'platform')]//span[contains(text(), 'Add')]"),
        ]

        for by, value in fb_add_selectors:
            try:
                btn = wait_for_element(driver, by, value, timeout=5, condition="clickable")
                if btn:
                    logger.info(f"Tìm thấy nút + Add Facebook: '{btn.text}' (selector: {value})")
                    safe_click(btn, delay=2.0)
                    fb_add_clicked = True
                    break
            except Exception:
                continue

        if not fb_add_clicked:
            try:
                add_buttons = driver.find_elements(By.XPATH, "//*[contains(text(), 'Add')]")
                for btn in add_buttons:
                    parent_text = btn.find_element(By.XPATH, "..").text
                    if "Facebook" in parent_text:
                        btn.click()
                        fb_add_clicked = True
                        logger.info("Clicked + Add Facebook (fallback)")
                        break
            except Exception:
                pass

        if not fb_add_clicked:
            logger.error("Không tìm thấy nút + Add Facebook")
            return "", ""

        # 3. Xử lý popup FB OAuth MỞ RA NGAY SAU KHI CLICK "+ Add" Facebook
        fb_popup = None
        for _ in range(15):
            time.sleep(1)
            new_handles = set(driver.window_handles) - handles_before
            for h in new_handles:
                driver.switch_to.window(h)
                if "facebook.com" in driver.current_url:
                    fb_popup = h
                    break
            if fb_popup:
                break

        if fb_popup:
            logger.info(f"Phát hiện popup FB OAuth trên trang Affiliate: {driver.current_url[:80]}")
            driver.switch_to.window(fb_popup)
            time.sleep(3)

            # Xử lý đầy đủ 4 bước trên popup FB OAuth (hỗ trợ cả Tiếng Việt & Tiếng Anh):
            # 1. "Tiếp tục dưới tên [Name]" / "Continue as [Name]"
            # 2. "Tiếp" / "Next" ("Bạn muốn sử dụng Trang nào cùng Shopee?")
            # 3. "Xong" / "Done" / "Save" / "Lưu" ("Shopee được phép làm gì?")
            # 4. "OK" ("Bạn hiện đã liên kết Shopee với Facebook")
            _click_fb_oauth_steps(driver, fb_popup)

            # Chờ popup tự đóng sau khi bấm OK (tối đa 6s), nếu vẫn mở thì đóng
            for _ in range(6):
                if fb_popup not in driver.window_handles:
                    break
                time.sleep(1)

            try:
                if fb_popup in driver.window_handles and len(driver.window_handles) > 1:
                    driver.switch_to.window(fb_popup)
                    driver.close()
            except Exception:
                pass

            try:
                driver.switch_to.window(affiliate_handle)
                logger.info("Đã quay lại trang Affiliate")
            except Exception:
                for h in driver.window_handles:
                    driver.switch_to.window(h)
                    if "affiliate" in driver.current_url.lower():
                        break
        else:
            logger.info("Không có popup FB OAuth mới (có thể đã tự xác nhận)")

        time.sleep(2)

        # 4. Dialog "Add Facebook" trên trang Affiliate → điền link FB → click Confirm
        logger.info(f"Điền link FB profile vào hộp thoại Add Facebook: {fb_profile_url}")

        url_input_selectors = [
            (By.CSS_SELECTOR, ".ant-modal textarea.ant-input"),
            (By.CSS_SELECTOR, "textarea[placeholder*='profile URL']"),
            (By.XPATH, "//textarea[contains(@placeholder, 'profile')]"),
            (By.XPATH, "//input[contains(@placeholder, 'profile URL')]"),
            (By.XPATH, "//input[contains(@placeholder, 'paste')]"),
            (By.CSS_SELECTOR, ".ant-modal input.ant-input"),
        ]

        url_filled = False
        for by, value in url_input_selectors:
            try:
                inp = wait_for_element(driver, by, value, timeout=3, condition="visible")
                if inp:
                    inp.clear()
                    safe_send_keys(inp, fb_profile_url, delay=0.5)
                    logger.info("✅ Đã điền link FB profile")
                    url_filled = True
                    break
            except Exception:
                continue

        if not url_filled:
            logger.error("Không tìm thấy ô nhập URL Facebook trong hộp thoại Add Facebook")
            return "", ""

        time.sleep(1)

        # Click "Confirm" trong modal Add Facebook
        confirm_selectors = [
            (By.CSS_SELECTOR, ".ant-modal button.ant-btn-primary"),
            (By.XPATH, "//div[contains(@class, 'ant-modal')]//button[contains(., 'Confirm')]"),
            (By.XPATH, "//button[contains(., 'Confirm')]"),
            (By.XPATH, "//span[contains(text(), 'Confirm')]"),
        ]
        for by, value in confirm_selectors:
            try:
                btn = wait_for_element(driver, by, value, timeout=5, condition="clickable")
                if btn:
                    safe_click(btn, delay=2.0)
                    logger.info("✅ Đã click Confirm hộp thoại Add Facebook")
                    break
            except Exception:
                continue

        time.sleep(2)

        # 5. Điền số điện thoại theo đầu số di động Philippines (bỏ số 0 ở đầu vì sau +63: 3 số đầu mạng + 7 số ngẫu nhiên = 10 chữ số)
        ph_prefixes = [
            # Globe / TM
            "905", "906", "915", "916", "917", "926", "927", "935", "936",
            "945", "955", "956", "965", "966", "967", "975", "977", "995", "997",
            # Smart / TNT / Sun
            "907", "908", "909", "910", "912", "918", "919", "920", "921", "922",
            "923", "928", "929", "930", "938", "939", "946", "947", "948", "949",
            "950", "951", "961", "968", "969", "970", "998", "999",
            # DITO
            "895", "896", "897", "898", "991", "992", "993", "994",
        ]
        chosen_prefix = random.choice(ph_prefixes)
        phone_number = chosen_prefix + "".join([str(random.randint(0, 9)) for _ in range(7)])
        logger.info(f"Điền số điện thoại (+63 {chosen_prefix}xxxxxxx): {phone_number}")

        phone_selectors = [
            (By.CSS_SELECTOR, "input[placeholder*='phone number']"),
            (By.CSS_SELECTOR, "input[placeholder*='phone']"),
            (By.XPATH, "//input[contains(@placeholder, 'phone')]"),
            (By.XPATH, "//*[contains(text(), 'Contact Phone')]/following::input[1]"),
        ]

        phone_filled = False
        for by, value in phone_selectors:
            try:
                inp = wait_for_element(driver, by, value, timeout=3, condition="visible")
                if inp:
                    inp.clear()
                    safe_send_keys(inp, phone_number, delay=0.3)
                    logger.info(f"✅ Đã điền SĐT: {phone_number}")
                    phone_filled = True
                    break
            except Exception:
                continue

        if not phone_filled:
            logger.warning("Không tìm thấy ô nhập số điện thoại")

        # Kiểm tra ô Contact Email
        contact_email = hotmail_email
        try:
            email_inp = wait_for_element(
                driver, By.CSS_SELECTOR, "input[placeholder*='email address'], input[placeholder*='email']",
                timeout=2, condition="visible"
            )
            if email_inp:
                current_email_val = (email_inp.get_attribute("value") or "").strip()
                if current_email_val:
                    contact_email = current_email_val
                elif hotmail_email:
                    email_inp.clear()
                    safe_send_keys(email_inp, hotmail_email, delay=0.3)
                    contact_email = hotmail_email
        except Exception:
            pass

        # Đảm bảo checkbox điều khoản đã được tick
        try:
            cbs = driver.find_elements(By.CSS_SELECTOR, ".ant-checkbox")
            for cb in cbs:
                if cb.is_displayed() and "ant-checkbox-checked" not in (cb.get_attribute("class") or ""):
                    cb.click()
                    time.sleep(0.5)
        except Exception:
            pass

        time.sleep(1)

        # 6. Click "Verify & Submit" (hoặc "Send Verification Code")
        submit_selectors = [
            (By.XPATH, "//button[contains(., 'Verify & Submit')]"),
            (By.CSS_SELECTOR, "button.mkt-btn.ant-btn-primary"),
            (By.XPATH, "//button[contains(., 'Send Verification Code')]"),
            (By.XPATH, "//span[contains(text(), 'Verify & Submit')]"),
        ]

        for by, value in submit_selectors:
            try:
                btn = wait_for_element(driver, by, value, timeout=5, condition="clickable")
                if btn:
                    safe_click(btn, delay=2.5)
                    logger.info(f"✅ Đã click '{btn.text.strip()}'")
                    break
            except Exception:
                continue

        # 7. Xử lý popup "Select verification method" → chọn "Verify with Email" → click "Confirm"
        email_prefix = (contact_email or hotmail_email).split("@")[0].strip().lower()
        fwd_dom = (forwarding_domain or "nguyenmaucole.io.vn").strip().lstrip("@").lower()
        tempmail_address = f"{email_prefix}@{fwd_dom}" if email_prefix else ""
        seen_ids = _get_existing_tempmail_ids(tempmail_address) if tempmail_address else set()

        verify_email_selectors = [
            (By.XPATH, "//button[contains(., 'Verify with Email')]"),
            (By.XPATH, "//*[contains(@class, 'contact-verification-method-popup__option')][contains(., 'Email')]"),
            (By.XPATH, "//*[contains(text(), 'Verify with Email')]"),
        ]
        email_method_clicked = False
        for by, value in verify_email_selectors:
            try:
                opt = wait_for_element(driver, by, value, timeout=5, condition="clickable")
                if opt:
                    safe_click(opt, delay=1.0)
                    logger.info("✅ Đã chọn 'Verify with Email'")
                    email_method_clicked = True
                    break
            except Exception:
                continue

        if email_method_clicked:
            confirm_method_selectors = [
                (By.CSS_SELECTOR, "button.contact-verification-method-popup__confirm"),
                (By.XPATH, "//div[contains(@class, 'ant-modal')]//button[contains(@class, 'ant-btn-primary') and contains(., 'Confirm')]"),
                (By.CSS_SELECTOR, ".ant-modal button.ant-btn-primary"),
            ]
            for by, value in confirm_method_selectors:
                try:
                    cbtn = wait_for_element(driver, by, value, timeout=5, condition="clickable")
                    if cbtn:
                        safe_click(cbtn, delay=3.0)
                        logger.info("✅ Đã click 'Confirm' chọn xác minh bằng Email")
                        break
                except Exception:
                    continue

        # 8. Trang "Enter Verification Code" (shopee.ph/verify/email) → lấy code từ mail.nguyenmaucole.io.vn
        time.sleep(2)
        _wait_for_captcha(driver)

        otp_inp = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "input[autocomplete='one-time-code'], input[maxlength='6'], input[type='tel']",
            timeout=10,
            condition="visible",
        )
        if otp_inp and tempmail_address:
            otp_code = _fetch_shopee_email_otp(driver, tempmail_address, seen_ids=seen_ids, timeout=180)
            if otp_code:
                otp_inp.click()
                otp_inp.clear()
                otp_inp.send_keys(otp_code)
                logger.info(f"✅ Đã điền mã OTP Shopee Affiliate: {otp_code}")
                time.sleep(1.5)

                next_btn_selectors = [
                    (By.XPATH, "//button[normalize-space()='Next' or normalize-space()='NEXT']"),
                    (By.XPATH, "//button[contains(., 'Next') or contains(., 'NEXT')]"),
                ]
                for by, value in next_btn_selectors:
                    try:
                        nbtn = wait_for_element(driver, by, value, timeout=5, condition="clickable")
                        if nbtn:
                            safe_click(nbtn, delay=3.0)
                            logger.info("✅ Đã click 'NEXT' xác minh Email Shopee Affiliate!")
                            break
                    except Exception:
                        continue
            else:
                logger.error(f"Không lấy được mã OTP Shopee Affiliate từ {tempmail_address}")
                return "", ""

        # 9. Chờ hiện thông báo "Registration Submitted Successfully!" rồi mới chuyển qua lấy Username + Affiliate ID
        logger.info("Đang chờ thông báo 'Registration Submitted Successfully!'...")
        reg_success = False
        wait_start = time.time()
        max_wait_success = getattr(config, "CAPTCHA_TIMEOUT", 300)

        driver.implicitly_wait(0)
        last_log_Bucket = 0
        try:
            while time.time() - wait_start < max_wait_success:
                check_browser_alive(driver)
                if _is_shopee_phone_verification(driver):
                    logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
                    raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
                try:
                    cur_u = (driver.current_url or "").lower()
                    body_txt = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
                    body_low = body_txt.lower()

                    if (
                        "registration submitted successfully" in body_low
                        or "thank you for signing up to our affiliate program" in body_low
                        or "learn more about shopee affiliates" in body_low
                        or "step=3" in cur_u
                        or "/dashboard" in cur_u
                    ):
                        logger.info("✅ Đã hiện thông báo 'Registration Submitted Successfully!' — Đăng ký Affiliate thành công!")
                        reg_success = True
                        break
                except (BrowserClosedError, LoginFailedError):
                    raise
                except Exception as e:
                    if is_browser_closed_exception(e):
                        raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e

                elapsed = int(time.time() - wait_start)
                if elapsed // 10 > last_log_Bucket:
                    last_log_Bucket = elapsed // 10
                    logger.info(f"⏳ Đang chờ màn hình 'Registration Submitted Successfully!'... ({elapsed}s/{max_wait_success}s)")
                time.sleep(2)
        finally:
            try:
                driver.implicitly_wait(5)
            except Exception:
                pass

        if not reg_success:
            logger.error("❌ Chưa thấy thông báo 'Registration Submitted Successfully!' sau khi chờ. Dừng lấy Account Setting.")
            return "", ""

        time.sleep(2)
        logger.info("=== Đang chuyển qua lấy thông tin Username + Affiliate ID tại Account Setting ===")

        # 10. Truy cập /account_setting để lấy Username (Cột F) và Affiliate ID (Cột G)
        return _get_affiliate_account_setting(driver, affiliate_url)

    except (BrowserClosedError, LoginFailedError):
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi đăng ký Affiliate: {e}")
        return "", ""


def _check_shopee_logged_in_user(driver: WebDriver) -> str:
    """
    Kiểm tra xem trang Shopee hiện tại đã đăng nhập sẵn chưa (có username ở góc phải trên cùng
    hoặc đã tự redirect khỏi /buyer/login về trang chủ).
    Trả về username nếu đã đăng nhập, hoặc chuỗi rỗng "" nếu chưa.
    """
    driver.implicitly_wait(0)
    try:
        # 1. Tìm trực tiếp phần tử hiển thị username ở góc phải header Shopee
        user_selectors = [
            (By.CSS_SELECTOR, ".navbar__username"),
            (By.CSS_SELECTOR, ".navbar__link--account"),
            (By.CSS_SELECTOR, "a[href*='/user/account']"),
            (By.XPATH, "//header//*[contains(@class, 'navbar__username')]"),
        ]
        for by, val in user_selectors:
            elems = driver.find_elements(by, val)
            for el in elems:
                try:
                    txt = (el.text or "").strip()
                    if txt and txt.lower() not in ("log in", "login", "sign up", "đăng nhập", "đăng ký"):
                        return txt
                except Exception:
                    continue

        # 2. Nếu truy cập /buyer/login mà tự động redirect về trang chủ (không còn ở buyer/login và không có nút Log In trên header)
        cur_url = (driver.current_url or "").lower()
        if "buyer/login" not in cur_url and "verify/" not in cur_url and "shopee." in cur_url:
            login_links = driver.find_elements(
                By.XPATH,
                "//header//a[normalize-space()='Log In' or normalize-space()='Login' or normalize-space()='Sign Up']",
            )
            visible_login = [el for el in login_links if el.is_displayed()]
            if not visible_login:
                for c in driver.get_cookies():
                    if c.get("name") in ("SPC_U", "SPC_ST") and c.get("value"):
                        return f"uid_{c.get('value')}"
    except Exception as e:
        logger.debug(f"Lỗi khi kiểm tra trạng thái đã login Shopee: {e}")
    finally:
        driver.implicitly_wait(5)
    return ""


def _is_shopee_captcha_or_blocked(driver: WebDriver) -> bool:
    """
    Kiểm tra xem trang Shopee hiện tại có đang ở màn hình chặn traffic ('verify/traffic')
    hoặc thông báo 'Please Try Again Later' / 'Verification can't be completed' hay không.
    (Riêng 'verify/captcha' có hình kéo mảnh ghép sẽ để GemCaptcha tự động giải qua _wait_for_captcha).
    """
    driver.implicitly_wait(0)
    try:
        cur_url = (driver.current_url or "").lower()
        if "verify/traffic" in cur_url:
            return True
        body_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
        if "please try again later" in body_txt or "verification can't be completed" in body_txt:
            return True
    except Exception:
        pass
    finally:
        driver.implicitly_wait(5)
    return False


def login_shopee_account_via_facebook(
    driver: WebDriver,
    fb_uid: str,
    fb_password: str,
    shopee_url: str,
    region_name: str = "Shopee.PH",
    hotmail_email: str = "",
    hotmail_password: str = "",
    recovery_email: str = "",
    new_hotmail_pass: str = "",
) -> bool:
    """
    Bước 2: Đăng nhập tài khoản Shopee (PH / MY / ID) bằng Facebook.
    Chỉ thực hiện đăng nhập tài khoản Shopee (chưa đăng ký Affiliate).

    Returns:
        True nếu đăng nhập tài khoản Shopee thành công.
    """
    logger.info(f"=== BƯỚC 2: Login tài khoản {region_name} bằng Facebook (URL: {shopee_url}) ===")

    try:
        _enable_shopee_antibot_bypass(driver)

        affiliate_url = config.SHOPEE_AFFILIATE_URLS.get(region_name, "")
        target_login_url = shopee_url
        if affiliate_url and "next=" not in shopee_url:
            sep = "&" if "?" in shopee_url else "?"
            target_login_url = f"{shopee_url}{sep}next={urllib.parse.quote(affiliate_url, safe='')}"

        logger.info(f"Mở tab mới và chuyển tới {target_login_url}...")
        try:
            driver.switch_to.new_window("tab")
        except Exception:
            driver.execute_script("window.open('about:blank', '_blank');")
            time.sleep(0.5)
            driver.switch_to.window(driver.window_handles[-1])
        _enable_shopee_antibot_bypass(driver)
        driver.get(target_login_url)

        existing_user = ""
        already_logged_in = False
        need_affiliate_fallback = False
        need_lang_select = region_name in ("Shopee MY", "Shopee ID")
        for poll_idx in range(12):  # Tối đa 6s (12 * 0.5s)
            time.sleep(0.5)
            check_browser_alive(driver)
            if need_lang_select:
                _select_shopee_english_language(driver, region_name)
            if _is_shopee_phone_verification(driver):
                logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
                raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
            existing_user = _check_shopee_logged_in_user(driver)
            if existing_user:
                logger.info(
                    f"✅ Phát hiện nhanh đã đăng nhập sẵn {region_name} (user: '{existing_user}')!"
                )
                already_logged_in = True
                break
            if _is_shopee_captcha_or_blocked(driver):
                logger.info(
                    f"⚠️ Phát hiện trang Captcha / 'Please Try Again Later' ({driver.current_url[:60]}) "
                    f"-> Thử đăng nhập qua trang Affiliate..."
                )
                need_affiliate_fallback = True
                break
            # Với Shopee MY/ID: chờ ít nhất 2s (poll_idx >= 4) để hộp thoại chọn ngôn ngữ kịp render; Shopee.PH kiểm tra ngay
            if not need_lang_select or poll_idx >= 4:
                driver.implicitly_wait(0)
                try:
                    fb_btns = driver.find_elements(By.XPATH, "//button[contains(., 'Facebook')]")
                    if fb_btns and any(b.is_displayed() for b in fb_btns):
                        if need_lang_select:
                            _select_shopee_english_language(driver, region_name)
                        break
                except Exception:
                    pass
                finally:
                    try:
                        driver.implicitly_wait(5)
                    except Exception:
                        pass

        if already_logged_in:
            return True

        if not need_affiliate_fallback:
            handles_before_click = set(driver.window_handles)
            shopee_handle = driver.current_window_handle

            if not _click_facebook_login_button(driver, region_name):
                if _is_shopee_phone_verification(driver):
                    logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
                    raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
                existing_user = _check_shopee_logged_in_user(driver)
                if existing_user:
                    logger.info(f"✅ Đã đăng nhập sẵn {region_name} (username: '{existing_user}')!")
                    return True
                elif _is_shopee_captcha_or_blocked(driver):
                    logger.info(
                        f"⚠️ Phát hiện trang Captcha / 'Please Try Again Later' ({driver.current_url[:60]}) "
                        f"-> Thử đăng nhập qua trang Affiliate..."
                    )
                    need_affiliate_fallback = True
                else:
                    raise LoginFailedError(f"Không tìm thấy nút Facebook trên {region_name}")
            else:
                if _is_shopee_phone_verification(driver):
                    logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
                    raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
                # Nếu sau khi bấm nút Facebook mà Shopee bật Captcha kéo hình trước khi mở popup FB -> chờ GemCaptcha giải
                if not (set(driver.window_handles) - handles_before_click):
                    _wait_for_captcha(driver)
                    if not (set(driver.window_handles) - handles_before_click) and "buyer/login" in (driver.current_url or "").lower():
                        _click_facebook_login_button(driver, region_name)
                if _is_shopee_captcha_or_blocked(driver):
                    logger.info(
                        "⚠️ Phát hiện 'Please Try Again Later' sau khi bấm FB -> Thử đăng nhập qua trang Affiliate..."
                    )
                    need_affiliate_fallback = True
                else:
                    if not _handle_facebook_popup(
                        driver, fb_uid, fb_password, handles_before_click, shopee_handle,
                        hotmail_email, hotmail_password, recovery_email, new_hotmail_pass,
                    ):
                        raise LoginFailedError(f"Xử lý popup Facebook thất bại ({region_name})")

        if need_affiliate_fallback and affiliate_url:
            logger.info(f"Truy cập trang Affiliate để thực hiện login FB: {affiliate_url}")
            driver.get(affiliate_url)
            time.sleep(4)
            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
            time.sleep(2)
            # Click nút Enter nếu có
            driver.implicitly_wait(0)
            try:
                enter_els = [
                    e for e in driver.find_elements(
                        By.XPATH,
                        "//a[contains(@class,'home-btn') or normalize-space()='Enter' or normalize-space()='ENTER'] | //button[normalize-space()='Enter' or normalize-space()='ENTER']",
                    )
                    if e.is_displayed()
                ]
                if enter_els:
                    if not safe_click(enter_els[0], delay=3.0):
                        driver.execute_script("arguments[0].click();", enter_els[0])
                        time.sleep(3.0)
                    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
                    time.sleep(2)
            except Exception:
                pass
            finally:
                driver.implicitly_wait(5)

            if _is_shopee_phone_verification(driver):
                logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
                raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")

            if "buyer/login" in (driver.current_url or "").lower():
                handles_before_login = set(driver.window_handles)
                login_handle = driver.current_window_handle
                if _click_facebook_login_button(driver, region_name):
                    _handle_facebook_popup(
                        driver, fb_uid, fb_password, handles_before_login, login_handle,
                        hotmail_email, hotmail_password, recovery_email, new_hotmail_pass,
                    )

        time.sleep(2)
        if _is_shopee_phone_verification(driver):
            logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' sau khi login FB -> Bỏ qua luồng này!")
            raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
        _wait_for_captcha(driver)

        for _ in range(10):
            check_browser_alive(driver)
            if _is_shopee_phone_verification(driver):
                logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
                raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")
            cur_u = (driver.current_url or "").lower()
            if "buyer/login" not in cur_u or _check_shopee_logged_in_user(driver):
                break
            _wait_for_captcha(driver)
            time.sleep(1)

        if _is_shopee_phone_verification(driver):
            logger.warning("⚠️ Phát hiện màn hình 'Mobile Phone Verification' trên Shopee -> Bỏ qua luồng này!")
            raise LoginFailedError("Shopee yêu cầu xác minh SĐT (Mobile Phone Verification)")

        current_url = driver.current_url or ""
        if "buyer/login" not in current_url.lower() or _check_shopee_logged_in_user(driver):
            logger.info(f"✅ Bước 2: Đăng nhập tài khoản {region_name} bằng Facebook thành công! URL: {current_url}")
            return True

        logger.error(f"❌ Bước 2: Vẫn ở trang login {region_name}. URL: {current_url}")
        return False

    except (BrowserClosedError, LoginFailedError):
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi login {region_name}: {e}")
        raise LoginFailedError(f"Lỗi login {region_name}: {e}")


def register_shopee_affiliate(
    driver: WebDriver,
    fb_uid: str,
    fb_password: str,
    region_name: str = "Shopee.PH",
    hotmail_email: str = "",
    forwarding_domain: str = "nguyenmaucole.io.vn",
) -> Tuple[bool, str, str]:
    """
    Bước 5: Đăng ký Shopee Affiliate theo thị trường đã chọn ở Bước 2.

    Returns:
        Tuple (success, username, affiliate_id)
    """
    logger.info(f"=== BƯỚC 5: Reg Aff Shopee ({region_name}) ===")
    try:
        _switch_to_shopee_tab(driver)
        _enable_shopee_antibot_bypass(driver)

        fb_profile_url = f"https://www.facebook.com/profile.php?id={fb_uid}"
        logger.info(f"FB Profile URL: {fb_profile_url}")

        affiliate_url = config.SHOPEE_AFFILIATE_URLS.get(region_name, "")
        if not affiliate_url:
            logger.error(f"Không tìm thấy URL Affiliate cho thị trường {region_name}")
            return False, "", ""

        username, affiliate_id = _register_affiliate(
            driver,
            affiliate_url,
            fb_profile_url,
            fb_uid,
            fb_password,
            region_name,
            hotmail_email,
            forwarding_domain,
        )
        if username or affiliate_id:
            return True, username, affiliate_id
        return False, "", ""

    except (BrowserClosedError, LoginFailedError):
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi đăng ký Affiliate {region_name}: {e}")
        raise LoginFailedError(f"Lỗi đăng ký Affiliate {region_name}: {e}")


def login_shopee_via_facebook(
    driver: WebDriver,
    fb_uid: str,
    fb_password: str,
    shopee_url: str,
    region_name: str = "Shopee",
    new_shopee_password: str = "",
    hotmail_email: str = "",
    forwarding_domain: str = "nguyenmaucole.io.vn",
) -> Tuple[bool, str, str]:
    """
    Hàm tương thích gộp cả Login Shopee bằng Facebook + Đăng ký Shopee Affiliate.
    """
    ok = login_shopee_account_via_facebook(driver, fb_uid, fb_password, shopee_url, region_name)
    if not ok:
        return False, "", ""
    return register_shopee_affiliate(driver, fb_uid, fb_password, region_name, hotmail_email, forwarding_domain)
