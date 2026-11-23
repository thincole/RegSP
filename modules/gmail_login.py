"""
Đăng nhập Gmail và cài đặt Email Forwarding về @nguyenmaucole.io.vn (hoặc @fviainboxes.com) cho Tab 'Reg Gmail'.
"""
import html
import json
import re
import time
import unicodedata
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import Select

import config
from utils.error_handler import (
    BrowserClosedError,
    LoginFailedError,
    check_browser_alive,
    is_browser_closed_exception,
)
from utils.logger import get_logger
from modules.hupsms_api import HupSmsClient, HupSmsError
from modules.totp_generator import generate_2fa_code, get_remaining_seconds
from utils.wait_helpers import safe_click, safe_send_keys, wait_for_element, wait_for_page_load

logger = get_logger()

TEMPMAIL_DOMAIN = "nguyenmaucole.io.vn"
TEMPMAIL_API_URL = "https://tempmail-backend.thincole.workers.dev/api/emails"
FVIA_DOMAIN = "fviainboxes.com"
FVIA_MESSAGES_API = "https://fviainboxes.com/messages"
FVIA_MESSAGE_API = "https://fviainboxes.com/message"

GMAIL_INBOX_URL = "https://mail.google.com/mail/u/0/#inbox"
GMAIL_FWD_SETTINGS_URL = "https://mail.google.com/mail/u/0/#settings/fwdandpop"
GOOGLE_LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin?service=mail&passive=true"
    "&continue=https%3A%2F%2Fmail.google.com%2Fmail%2Fu%2F0%2F"
)


def _safe_get(driver: WebDriver, url: str) -> None:
    """Mở URL an toàn, nếu quá timeout trang phụ thì dừng nạp tài nguyên phụ và tiếp tục."""
    try:
        driver.get(url)
    except TimeoutException:
        logger.warning(f"Trang {url[:65]} tải chậm quá timeout -> dừng nạp phụ và tiếp tục...")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass


def _is_gmail_inbox_loaded(driver: WebDriver, expected_email: str = "") -> bool:
    """Kiểm tra trình duyệt đã vào hộp thư Gmail (mail.google.com) chưa."""
    driver.implicitly_wait(0)
    try:
        cur_u = (driver.current_url or "").lower()
        if "mail.google.com/mail" not in cur_u:
            return False
        if any(k in cur_u for k in ("accounts.google.com", "workspace.google.com", "/intl/")):
            return False

        # Nếu có expected_email, kiểm tra xem profile có đang login nhầm Gmail khác không
        if expected_email:
            exp_low = expected_email.strip().lower()
            title_low = (driver.title or "").lower()
            if "@" in title_low and exp_low not in title_low:
                # Kiểm tra kỹ qua nút tài khoản Google ở góc phải
                acc_els = driver.find_elements(By.CSS_SELECTOR, "a[aria-label*='@'], [data-email]")
                for el in acc_els:
                    lbl = ((el.get_attribute("aria-label") or "") + " " + (el.get_attribute("data-email") or "")).lower()
                    if "@" in lbl and exp_low not in lbl:
                        return False
        return True
    except Exception:
        return False
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass


def _dismiss_gmail_popups(driver: WebDriver) -> None:
    """Đóng các popup giới thiệu tính năng thông minh / thông báo lần đầu vào Gmail."""
    driver.implicitly_wait(0)
    try:
        popup_xpaths = [
            "//div[@role='dialog']//button[normalize-space()='Got it' or normalize-space()='OK' or normalize-space()='Đã hiểu' or normalize-space()='Đóng' or normalize-space()='Next' or normalize-space()='Done']",
            "//button[@aria-label='Close' or @aria-label='Đóng'][ancestor::div[@role='dialog']]",
        ]
        for xp in popup_xpaths:
            btns = [
                b for b in driver.find_elements(By.XPATH, xp)
                if b.is_displayed() and "forwarding" not in (b.text or "").lower()
            ]
            if btns:
                if not safe_click(btns[0], delay=1.0):
                    driver.execute_script("arguments[0].click();", btns[0])
                    time.sleep(1.0)
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass


def _normalize_prompt_text(text: str) -> str:
    """Chuẩn hóa dấu nháy cong Unicode và chữ thường để so khớp thông báo Google chính xác."""
    return (
        (text or "")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u02bc", "'")
        .lower()
    )


_GOOGLE_PHONE_REJECT_KEYWORDS = (
    "this phone number cannot be used",
    "phone number cannot be used",
    "cannot be used for verification",
    "used too many times",
    "too many failed attempts",
    "too many attempts",
    "try a different phone number",
    "try a different number",
    "there was a problem verifying",
    "not a valid phone number",
    "enter a valid phone number",
    "format isn't right",
    "so dien thoai nay khong the dung",
    "khong the su dung so dien thoai",
    "duoc su dung qua nhieu lan",
    "thu mot so khac",
    "so dien thoai khong hop le",
    "khong dung dinh dang",
)

_GOOGLE_OTP_REJECT_KEYWORDS = (
    "wrong code",
    "invalid code",
    "try again with another code",
    "code is incorrect",
    "code has expired",
    "ma khong dung",
    "khong chinh xac",
    "ma da het han",
)


def _has_visible_google_captcha(driver: WebDriver, body_norm_low: str) -> bool:
    """Kiểm tra xem trang Google hiện tại có đang bắt giải Captcha hiển thị hay không."""
    if any(
        k in body_norm_low
        for k in ("captcha", "type the text you hear or see", "nhập văn bản bạn nghe hoặc thấy", "nhap van ban ban nghe hoac thay")
    ):
        return True
    try:
        for img in driver.find_elements(By.CSS_SELECTOR, "img#captchaimg, img[src*='Captcha' i]"):
            if img.is_displayed():
                return True
        for frame in driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']"):
            if frame.is_displayed():
                size = frame.size or {}
                if size.get("height", 0) > 60 and size.get("width", 0) > 60:
                    return True
    except Exception:
        pass
    return False


def _detect_google_manual_verification(cur_url_low: str, body_norm_low: str) -> Optional[str]:
    """
    Nhận diện màn hình Google yêu cầu xác minh thủ công (SĐT / danh tính / Captcha).
    Trả về mô tả lý do nếu đang bị chặn, hoặc None nếu không phải.
    """
    if "captcha" in body_norm_low:
        return "Google yêu cầu giải Captcha"
    if (
        "/signin/challenge/iap" in cur_url_low
        or "enter a phone number" in body_norm_low
        or "nhập số điện thoại" in body_norm_low
        or "nhap so dien thoai" in body_norm_low
    ):
        return "Google yêu cầu xác minh SĐT (Verify it's you)"
    if (
        "verify it's you" in body_norm_low
        or "xac minh danh tinh" in body_norm_low
        or "xác minh danh tính" in body_norm_low
        or "/signin/challenge/dp" in cur_url_low
        or "/signin/challenge/ipp" in cur_url_low
        or "/signin/challenge/sq" in cur_url_low
        or "/signin/challenge/selection" in cur_url_low
    ):
        return "Google yêu cầu xác minh danh tính (Verify it's you)"
    return None


def _is_skippable_google_phone_nudge(driver: WebDriver, cur_url_low: str) -> bool:
    """Tránh thuê SIM ở màn hình gds/myaccount chỉ gợi ý thêm SĐT khôi phục có nút Bỏ qua."""
    if "gds.google.com" in cur_url_low:
        return True
    try:
        skip_els = driver.find_elements(
            By.XPATH,
            "//button[normalize-space()='Not now' or normalize-space()='Để sau' or normalize-space()='Skip' or normalize-space()='Bỏ qua' or normalize-space()='Cancel' or normalize-space()='Hủy' or .//span[normalize-space()='Not now' or normalize-space()='Để sau' or normalize-space()='Skip' or normalize-space()='Bỏ qua']]"
            " | //a[normalize-space()='Not now' or normalize-space()='Để sau' or normalize-space()='Skip' or normalize-space()='Bỏ qua']",
        )
        return any(el.is_displayed() for el in skip_els)
    except Exception:
        return False


def _find_visible_phone_input(driver: WebDriver):
    """Tìm ô nhập số điện thoại Google (tránh nhầm với ô nhập OTP/2FA)."""
    for el in driver.find_elements(
        By.CSS_SELECTOR,
        "input#phoneNumberId, input[name='phoneNumber'], input[type='tel'], input[aria-label*='Phone' i], input[aria-label*='điện thoại' i]",
    ):
        try:
            if not el.is_displayed():
                continue
            eid = (el.get_attribute("id") or "").lower()
            ename = (el.get_attribute("name") or "").lower()
            ac = (el.get_attribute("autocomplete") or "").lower()
            if eid in ("idvanyphonepin", "totppin") or ename in ("idvanyphonepin", "pin", "totppin", "code") or ac == "one-time-code":
                continue
            return el
        except Exception:
            continue
    return None


def _find_visible_phone_otp_input(driver: WebDriver, cur_url_low: str = "", body_norm_low: str = ""):
    """Tìm ô nhập mã SMS OTP trên Google."""
    for el in driver.find_elements(
        By.CSS_SELECTOR,
        "input#idvAnyPhonePin, input[name='idvAnyPhonePin'], input[name='Pin'], input[name='code'], input[autocomplete='one-time-code']",
    ):
        try:
            if not el.is_displayed():
                continue
            eid = (el.get_attribute("id") or "").lower()
            ename = (el.get_attribute("name") or "").lower()
            if eid == "totppin" or ename == "totppin":
                continue
            return el
        except Exception:
            continue

    if any(k in cur_url_low for k in ("idvanyphoneverify", "challenge/iap")) and any(
        k in body_norm_low
        for k in ("enter the code", "enter code", "nhập mã", "nhap ma", "sent a text", "vừa gửi", "vua gui", "6-digit", "6 chữ số")
    ):
        for el in driver.find_elements(By.CSS_SELECTOR, "input[type='tel'], input[type='text']"):
            try:
                if not el.is_displayed():
                    continue
                eid = (el.get_attribute("id") or "").lower()
                ename = (el.get_attribute("name") or "").lower()
                if eid == "phonenumberid" or ename == "phonenumber":
                    continue
                return el
            except Exception:
                continue
    return None


def _select_sms_delivery_option_if_present(driver: WebDriver) -> None:
    """Chọn 'Text message' / 'Tin nhắn văn bản' nếu màn hình có radio Text vs Call."""
    try:
        radios = driver.find_elements(
            By.XPATH,
            "//input[@type='radio'] | //*[@role='radio'] | //label[.//input[@type='radio']]",
        )
        for rad in radios:
            if not rad.is_displayed():
                continue
            txt = _normalize_prompt_text(
                (rad.text or "") + " " + (rad.get_attribute("aria-label") or "") + " " + (rad.get_attribute("value") or "")
            )
            if any(k in txt for k in ("text", "sms", "tin nhan", "tin nhắn")) and not any(
                k in txt for k in ("call", "voice", "cuoc goi", "cuộc gọi")
            ):
                checked = (rad.get_attribute("aria-checked") or "").lower() == "true"
                if not checked:
                    try:
                        if rad.tag_name.lower() == "input" and rad.is_selected():
                            return
                    except Exception:
                        pass
                    if not safe_click(rad, delay=0.5):
                        driver.execute_script("arguments[0].click();", rad)
                        time.sleep(0.5)
                return
    except Exception:
        pass


def _select_vn_country_and_enter_phone(driver: WebDriver, phone_inp, phone_e164: str, phone_local: str) -> None:
    """Chọn mã vùng Việt Nam (+84) nếu có dropdown, rồi điền số điện thoại sạch."""
    value_to_type = phone_e164

    # 1. Nếu là <select> chuẩn
    try:
        for sel_el in driver.find_elements(By.CSS_SELECTOR, "select"):
            if not sel_el.is_displayed():
                continue
            opts = sel_el.find_elements(By.TAG_NAME, "option")
            for opt in opts:
                ov = (opt.get_attribute("value") or "").strip().upper()
                ot = _normalize_prompt_text(opt.text or "")
                if ov == "VN" or "+84" in ot or "vietnam" in ot or "viet nam" in ot:
                    Select(sel_el).select_by_value(opt.get_attribute("value"))
                    time.sleep(0.4)
                    value_to_type = phone_local
                    break
            break
    except Exception:
        pass

    # 2. Nhập số vào ô input
    try:
        phone_inp.click()
    except Exception:
        pass
    try:
        phone_inp.send_keys(Keys.CONTROL, "a")
        phone_inp.send_keys(Keys.BACKSPACE)
    except Exception:
        pass
    safe_send_keys(phone_inp, value_to_type, delay=0.6)

    # 3. Kiểm tra Material combobox nếu chưa tự chuyển sang +84 / Vietnam
    if value_to_type == phone_e164:
        try:
            combos = [c for c in driver.find_elements(By.CSS_SELECTOR, "[role='combobox']") if c.is_displayed()]
            if combos:
                combo = combos[0]
                combo_meta = _normalize_prompt_text(
                    (combo.text or "") + " " + (combo.get_attribute("aria-label") or "")
                )
                if not any(k in combo_meta for k in ("vietnam", "viet nam", "+84")):
                    if safe_click(combo, delay=0.6):
                        vn_opts = [
                            o for o in driver.find_elements(
                                By.XPATH,
                                "//*[@role='option' and (@data-value='vn' or @data-value='VN' or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'vietnam') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'vietnam') or contains(., '+84') or contains(., 'Việt Nam'))]"
                                " | //li[@data-value='vn' or @data-value='VN']",
                            )
                            if o.is_displayed()
                        ]
                        if vn_opts:
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", vn_opts[0])
                            if not safe_click(vn_opts[0], delay=0.5):
                                driver.execute_script("arguments[0].click();", vn_opts[0])
                                time.sleep(0.5)
                            phone_inp.send_keys(Keys.CONTROL, "a")
                            phone_inp.send_keys(Keys.BACKSPACE)
                            safe_send_keys(phone_inp, phone_local, delay=0.5)
                        else:
                            phone_inp.send_keys(Keys.ESCAPE)
                            time.sleep(0.3)
        except Exception:
            pass


def _click_google_phone_submit(driver: WebDriver, input_el=None) -> None:
    """Bấm nút Next / Send / Get code / Verify trên màn hình xác minh SĐT Google."""
    btns = [
        b for b in driver.find_elements(
            By.CSS_SELECTOR,
            "#idvanyphonecollectNext button, #idvanyphonecollectNext, "
            "#idvanyphoneverifyNext button, #idvanyphoneverifyNext, "
            "#next button, #next, button[type='submit'], input[type='submit']",
        )
        if b.is_displayed()
    ]
    if not btns:
        btns = [
            b for b in driver.find_elements(By.XPATH, "//button | //div[@role='button']")
            if b.is_displayed()
            and any(
                _normalize_prompt_text(b.text or "") == k or k in _normalize_prompt_text(b.text or "")
                for k in ("next", "tiếp theo", "tiếp", "send", "gửi", "get code", "nhận mã", "verify", "xác minh")
            )
        ]
    if btns:
        if not safe_click(btns[0], delay=1.5):
            driver.execute_script("arguments[0].click();", btns[0])
            time.sleep(1.5)
    elif input_el is not None:
        input_el.send_keys(Keys.RETURN)
        time.sleep(1.5)


def _extract_google_phone_error(driver: WebDriver, body_low: str, keywords: Tuple[str, ...]) -> str:
    """Tìm thông báo lỗi Google từ chối SĐT hoặc sai mã OTP."""
    try:
        for alert in driver.find_elements(By.CSS_SELECTOR, "[role='alert'], [aria-live='assertive'], .Ekjuhf, .o6cuMc"):
            if not alert.is_displayed():
                continue
            txt = (alert.text or "").strip()
            txt_norm = _normalize_prompt_text(txt)
            if txt_norm and any(k in txt_norm for k in keywords):
                return txt
    except Exception:
        pass

    for k in keywords:
        if k in body_low:
            return k
    return ""


def _try_enter_phone_challenge_from_selection(driver: WebDriver, cur_url_low: str) -> bool:
    """Nếu đang ở màn hình chọn phương thức verify, click tùy chọn nhập SĐT mới (không click SĐT cũ bị che)."""
    if "/signin/challenge/selection" not in cur_url_low:
        return False
    try:
        tiles = driver.find_elements(
            By.XPATH,
            "//*[@data-challengetype='9']"
            " | //li[.//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'enter a phone number') or contains(., 'nhập số điện thoại')]]"
            " | //div[@role='link' or @role='button'][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'enter a phone number') or contains(., 'nhập số điện thoại')]",
        )
        for t in tiles:
            if not t.is_displayed():
                continue
            meta = _normalize_prompt_text(t.text or "")
            if "•" in (t.text or "") or "*" in (t.text or ""):
                continue
            if meta:
                logger.info("Chọn xác minh bằng tùy chọn 'Enter a phone number'...")
                if not safe_click(t, delay=2.0):
                    driver.execute_script("arguments[0].click();", t)
                    time.sleep(2.0)
                return True
    except Exception:
        pass
    return False


def _notify_verified_phone(
    on_phone_verified: Optional[Callable[[str], None]],
    phone_number: str,
) -> None:
    """Gọi callback lưu SĐT đã dùng verify Gmail (bắt lỗi an toàn để không làm gián đoạn luồng login)."""
    phone_clean = (phone_number or "").strip()
    if not phone_clean or on_phone_verified is None:
        return
    try:
        on_phone_verified(phone_clean)
    except Exception as e:
        logger.warning(f"⚠️ Lỗi callback lưu SĐT verify Gmail ({phone_clean}): {e}")


def _verify_google_sms_challenge(
    driver: WebDriver,
    hupsms_api_key: str,
    hupsms_service_id: int = 0,
    hupsms_network: str = "",
    hupsms_prefix: str = "",
    sms_timeout: int = 180,
    max_attempts: int = 2,
    on_phone_verified: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Tự động thuê SIM VN qua HupSMS, nhập số lên màn hình Google và điền mã OTP SMS.
    Trả về True nếu xử lý xong bước verify SĐT; False nếu màn hình không phải nhập SĐT mới (hoặc bị Captcha).
    """
    api_key_clean = (hupsms_api_key or "").strip()
    if not api_key_clean:
        return False

    cur_u = (driver.current_url or "").lower()
    body_low = _normalize_prompt_text(driver.execute_script("return document.body ? document.body.innerText : '';") or "")

    if _has_visible_google_captcha(driver, body_low):
        return False
    if _is_skippable_google_phone_nudge(driver, cur_u):
        return False

    phone_inp = _find_visible_phone_input(driver)
    if phone_inp is None and _try_enter_phone_challenge_from_selection(driver, cur_u):
        cur_u = (driver.current_url or "").lower()
        body_low = _normalize_prompt_text(driver.execute_script("return document.body ? document.body.innerText : '';") or "")
        if _has_visible_google_captcha(driver, body_low):
            return False
        phone_inp = _find_visible_phone_input(driver)

    if phone_inp is None:
        return False

    client = HupSmsClient(api_key=api_key_clean)
    attempts = max(1, int(max_attempts or 1))
    last_error = "Không xác minh được SĐT Gmail qua HupSMS"

    for attempt in range(1, attempts + 1):
        check_browser_alive(driver)
        phone_inp = _find_visible_phone_input(driver)
        if phone_inp is None:
            raise LoginFailedError(last_error)

        try:
            order = client.rent_google_number(
                service_id=int(hupsms_service_id or 0),
                network=hupsms_network,
                prefix=hupsms_prefix,
                stop_check=lambda: check_browser_alive(driver),
            )
        except HupSmsError as e:
            raise LoginFailedError(f"HupSMS không thuê được SIM VN: {e}") from e

        order_id = str(order.get("orderId", "")).strip()
        phone_local = str(order.get("phoneLocal") or order.get("phone") or "").strip()
        phone_e164 = str(order.get("phoneE164") or phone_local).strip()
        logger.info(
            f"📱 [HupSMS {attempt}/{attempts}] Đã thuê {phone_local} "
            f"({order.get('serviceName', 'Google/Gmail')} - {order.get('server', '?')}, "
            f"{order.get('price', '?')}đ, order={order_id})"
        )

        otp_screen_ready = False
        phone_rejected_reason = ""
        order_completed = False
        try:
            _select_sms_delivery_option_if_present(driver)
            _select_vn_country_and_enter_phone(driver, phone_inp, phone_e164, phone_local)
            _click_google_phone_submit(driver, phone_inp)

            for _ in range(12):
                time.sleep(1.0)
                check_browser_alive(driver)
                u_now = (driver.current_url or "").lower()
                b_now = _normalize_prompt_text(
                    driver.execute_script("return document.body ? document.body.innerText : '';") or ""
                )
                if _has_visible_google_captcha(driver, b_now):
                    client.cancel_order(order_id, ignore_errors=True)
                    raise LoginFailedError("Google yêu cầu giải Captcha sau khi nhập SĐT")
                if _find_visible_phone_otp_input(driver, u_now, b_now) is not None:
                    otp_screen_ready = True
                    break
                if any(k in u_now for k in ("mail.google.com/mail", "speedbump", "gds.google.com", "myaccount.google.com", "passkeyenrollment")):
                    logger.info(f"✅ Google tự qua bước xác minh SĐT với {phone_local}!")
                    _notify_verified_phone(on_phone_verified, phone_local)
                    return True
                err_txt = _extract_google_phone_error(driver, b_now, _GOOGLE_PHONE_REJECT_KEYWORDS)
                if err_txt:
                    phone_rejected_reason = err_txt
                    break

            if not otp_screen_ready:
                client.cancel_order(order_id, ignore_errors=True)
                last_error = (
                    f"Google từ chối số {phone_local}: {phone_rejected_reason}"
                    if phone_rejected_reason
                    else f"Google không chuyển sang bước nhập OTP cho số {phone_local}"
                )
                logger.warning(f"⚠️ {last_error}")
                if attempt < attempts:
                    continue
                raise LoginFailedError(last_error)

            logger.info(f"⏳ Đang chờ HupSMS nhận OTP cho {phone_local} (order={order_id}, timeout={sms_timeout}s)...")
            otp_data = client.wait_for_otp(
                order_id,
                timeout=sms_timeout,
                poll_interval=4.0,
                auto_cancel_on_timeout=True,
                stop_check=lambda: check_browser_alive(driver),
            )
            order_completed = True
            otp_code = str(otp_data.get("code", "")).strip()
            logger.info(f"📩 Nhận được OTP SMS từ {phone_local}: {otp_code}")

            otp_inp = _find_visible_phone_otp_input(
                driver,
                (driver.current_url or "").lower(),
                _normalize_prompt_text(driver.execute_script("return document.body ? document.body.innerText : '';") or ""),
            )
            if otp_inp is None:
                raise LoginFailedError("Không tìm thấy ô nhập mã OTP trên Google sau khi nhận SMS")

            try:
                otp_inp.click()
                otp_inp.send_keys(Keys.CONTROL, "a")
                otp_inp.send_keys(Keys.BACKSPACE)
            except Exception:
                pass
            safe_send_keys(otp_inp, otp_code, delay=0.8)
            _click_google_phone_submit(driver, otp_inp)

            for _ in range(12):
                time.sleep(1.0)
                check_browser_alive(driver)
                u_after = (driver.current_url or "").lower()
                b_after = _normalize_prompt_text(
                    driver.execute_script("return document.body ? document.body.innerText : '';") or ""
                )
                otp_err = _extract_google_phone_error(driver, b_after, _GOOGLE_OTP_REJECT_KEYWORDS)
                if otp_err:
                    raise LoginFailedError(f"Google từ chối mã OTP {otp_code} ({otp_err})")
                if "/signin/challenge/iap" not in u_after and _find_visible_phone_otp_input(driver, u_after, b_after) is None:
                    logger.info(f"✅ Xác minh SĐT Gmail thành công bằng HupSMS ({phone_local})!")
                    _notify_verified_phone(on_phone_verified, phone_local)
                    return True
                if any(k in u_after for k in ("mail.google.com/mail", "speedbump", "gds.google.com", "myaccount.google.com", "passkeyenrollment")):
                    logger.info(f"✅ Xác minh SĐT Gmail thành công bằng HupSMS ({phone_local})!")
                    _notify_verified_phone(on_phone_verified, phone_local)
                    return True

            if _find_visible_phone_otp_input(driver, (driver.current_url or "").lower(), "") is None:
                _notify_verified_phone(on_phone_verified, phone_local)
                return True
            raise LoginFailedError(f"Đã nhập OTP {otp_code} nhưng Google vẫn giữ ở màn hình xác minh SĐT")

        except (BrowserClosedError, LoginFailedError):
            if not order_completed:
                client.cancel_order(order_id, ignore_errors=True)
            raise
        except HupSmsError as e:
            if e.code not in ("OTP_TIMEOUT", "ORDER_EXPIRED", "ORDER_CANCELLED"):
                client.cancel_order(order_id, ignore_errors=True)
            last_error = f"HupSMS lỗi với số {phone_local}: {e}"
            logger.warning(f"⚠️ {last_error}")
            if attempt < attempts and _find_visible_phone_input(driver) is not None:
                continue
            raise LoginFailedError(last_error) from e
        except Exception as e:
            if not order_completed:
                client.cancel_order(order_id, ignore_errors=True)
            if is_browser_closed_exception(e):
                raise BrowserClosedError(str(e)) from e
            raise LoginFailedError(f"Lỗi xác minh SĐT Gmail qua HupSMS: {e}") from e

    raise LoginFailedError(last_error)


def _handle_google_post_login_prompts(
    driver: WebDriver,
    gmail_email: str = "",
    recovery_email: str = "",
    twofa_secret: str = "",
    gmail_password: str = "",
    hupsms_api_key: Optional[str] = None,
    hupsms_service_id: Optional[int] = None,
    hupsms_network: Optional[str] = None,
    hupsms_prefix: Optional[str] = None,
    on_phone_verified: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Xử lý các bước xác minh Mật khẩu / 2FA / Email khôi phục / SMS SĐT (HupSMS) / Passkey / Speedbump sau khi đăng nhập Google.
    """
    sms_key = (getattr(config, "HUPSMS_API_KEY", "") if hupsms_api_key is None else hupsms_api_key) or ""
    sms_sid = int(getattr(config, "HUPSMS_SERVICE_ID", 0) if hupsms_service_id is None else hupsms_service_id or 0)
    sms_net = (getattr(config, "HUPSMS_NETWORK", "") if hupsms_network is None else hupsms_network) or ""
    sms_pre = (getattr(config, "HUPSMS_PREFIX", "") if hupsms_prefix is None else hupsms_prefix) or ""
    sms_timeout = int(getattr(config, "HUPSMS_SMS_TIMEOUT", 180) or 180)
    sms_retries = int(getattr(config, "HUPSMS_MAX_RETRIES", 2) or 2)

    for _ in range(8):
        check_browser_alive(driver)
        time.sleep(2)
        cur_u = (driver.current_url or "").lower()
        if "mail.google.com/mail" in cur_u and "accounts.google.com" not in cur_u:
            return

        driver.implicitly_wait(0)
        try:
            body_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "")
            body_low = _normalize_prompt_text(body_txt)

            # 0. Nếu gặp lại màn hình nhập mật khẩu (vd sau bước challenge trước đó)
            if gmail_password:
                pwd_inps = [
                    e for e in driver.find_elements(By.CSS_SELECTOR, "input[name='Passwd'], #password input[type='password'], input[type='password']")
                    if e.is_displayed()
                ]
                if pwd_inps:
                    logger.info("Điền mật khẩu Google ở bước tiếp theo...")
                    safe_send_keys(pwd_inps[0], gmail_password, delay=0.8)
                    pwd_next = [
                        b for b in driver.find_elements(By.CSS_SELECTOR, "#passwordNext button, #passwordNext")
                        if b.is_displayed()
                    ]
                    if pwd_next:
                        if not safe_click(pwd_next[0], delay=2.5):
                            driver.execute_script("arguments[0].click();", pwd_next[0])
                            time.sleep(2.5)
                    else:
                        pwd_inps[0].send_keys(Keys.RETURN)
                        time.sleep(2.5)
                    continue

            # 1. Ô nhập mã 2FA Authenticator (totpPin)
            totp_inps = [
                e for e in driver.find_elements(By.CSS_SELECTOR, "input#totpPin, input[name='totpPin'], input[type='tel'][autocomplete='one-time-code']")
                if e.is_displayed()
            ]
            if totp_inps and twofa_secret:
                rem = get_remaining_seconds(twofa_secret)
                if rem < 5:
                    time.sleep(rem + 1)
                code = generate_2fa_code(twofa_secret)
                logger.info(f"Điền mã 2FA Google: {code}")
                safe_send_keys(totp_inps[0], code, delay=0.8)
                next_btns = [
                    b for b in driver.find_elements(By.CSS_SELECTOR, "#totpNext button, #totpNext, button[type='button']")
                    if b.is_displayed() and any(k in (b.text or "").lower() for k in ("next", "tiếp"))
                ]
                if next_btns:
                    safe_click(next_btns[0], delay=2.5)
                else:
                    totp_inps[0].send_keys(Keys.RETURN)
                    time.sleep(2.5)
                continue

            # 2. Màn hình chọn phương thức xác minh ("Confirm your recovery email" / "Xác nhận email khôi phục của bạn")
            if recovery_email and ("confirm your recovery email" in body_low or "xác nhận email khôi phục" in body_low):
                rec_tiles = [
                    e for e in driver.find_elements(
                        By.XPATH,
                        "//*[@data-challengetype='12'] | //li[.//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'recovery email') or contains(., 'email khôi phục')]]",
                    )
                    if e.is_displayed()
                ]
                if rec_tiles:
                    logger.info("Chọn xác minh bằng 'Confirm your recovery email'...")
                    if not safe_click(rec_tiles[0], delay=2.5):
                        driver.execute_script("arguments[0].click();", rec_tiles[0])
                        time.sleep(2.5)

                rec_inps = [
                    e for e in driver.find_elements(
                        By.CSS_SELECTOR,
                        "input#knowledge-preregistered-email-response, input[name='knowledgePreregisteredEmailResponse'], input[type='email']",
                    )
                    if e.is_displayed()
                ]
                if rec_inps:
                    logger.info(f"Điền email khôi phục Gmail: {recovery_email}")
                    safe_send_keys(rec_inps[0], recovery_email, delay=0.8)
                    rec_inps[0].send_keys(Keys.RETURN)
                    time.sleep(3)
                    continue

            # 3. Màn hình điều khoản / speedbump ("I understand" / "Tôi hiểu" / "I agree" / "Tôi đồng ý")
            if any(k in cur_u for k in ("speedbump", "gaplustos", "consent")) or "i understand" in body_low or "tôi hiểu" in body_low:
                agree_btns = [
                    b for b in driver.find_elements(
                        By.XPATH,
                        "//button[contains(., 'I understand') or contains(., 'Tôi hiểu') or contains(., 'I agree') or contains(., 'Tôi đồng ý') or contains(., 'Accept')]"
                        " | //input[@type='submit' and (@value='I understand' or @value='Tôi hiểu' or @id='confirm')]",
                    )
                    if b.is_displayed()
                ]
                if agree_btns:
                    logger.info("Bấm xác nhận điều khoản Google ('I understand' / 'Accept')...")
                    if not safe_click(agree_btns[0], delay=2.5):
                        driver.execute_script("arguments[0].click();", agree_btns[0])
                        time.sleep(2.5)
                    continue

            # 4. Bỏ qua lời nhắc tạo Passkey / thêm SĐT / cập nhật bảo mật ("Not now" / "Để sau" / "Skip" / "Bỏ qua" / "Cancel")
            if any(k in cur_u for k in ("gds.google.com", "myaccount.google.com", "signin/v2/passkeyenrollment", "speedbump")) or any(
                k in body_low for k in ("passkey", "khóa truy cập", "simplify your sign-in", "protect your account", "recovery", "home address")
            ):
                skip_btns = [
                    b for b in driver.find_elements(
                        By.XPATH,
                        "//button[normalize-space()='Not now' or normalize-space()='Để sau' or normalize-space()='Skip' or normalize-space()='Bỏ qua' or normalize-space()='Cancel' or normalize-space()='Hủy' or .//span[normalize-space()='Not now' or normalize-space()='Để sau' or normalize-space()='Skip' or normalize-space()='Bỏ qua']]"
                        " | //a[normalize-space()='Not now' or normalize-space()='Để sau' or normalize-space()='Skip' or normalize-space()='Bỏ qua']",
                    )
                    if b.is_displayed()
                ]
                if skip_btns:
                    logger.info("Bỏ qua màn hình nhắc Passkey / Bảo mật Google ('Not now' / 'Skip')...")
                    if not safe_click(skip_btns[0], delay=2.5):
                        driver.execute_script("arguments[0].click();", skip_btns[0])
                        time.sleep(2.5)
                    continue

                if "myaccount.google.com" in cur_u:
                    _safe_get(driver, GMAIL_INBOX_URL)
                    time.sleep(3)
                    return

            # 5. Nếu gặp xác minh SĐT / Captcha / danh tính Google
            manual_reason = _detect_google_manual_verification(cur_u, body_low)
            if manual_reason or _find_visible_phone_input(driver) is not None:
                if sms_key.strip() and _verify_google_sms_challenge(
                    driver,
                    hupsms_api_key=sms_key,
                    hupsms_service_id=sms_sid,
                    hupsms_network=sms_net,
                    hupsms_prefix=sms_pre,
                    sms_timeout=sms_timeout,
                    max_attempts=sms_retries,
                    on_phone_verified=on_phone_verified,
                ):
                    continue

                reason_msg = manual_reason or "Google yêu cầu xác minh SĐT (Verify it's you)"
                wait_cfg = max(0, int(getattr(config, "CAPTCHA_TIMEOUT", 60)))
                max_wait = min(wait_cfg, 60)
                logger.info(f"⏳ Phát hiện {reason_msg}, đang chờ tối đa {max_wait}s để hoàn tất...")
                resolved = False
                waited = 0
                manual_entered_phone = ""
                while waited < max_wait:
                    try:
                        p_el = _find_visible_phone_input(driver)
                        if p_el is not None:
                            p_val = (p_el.get_attribute("value") or "").strip()
                            if len(re.sub(r"\D", "", p_val)) >= 8:
                                manual_entered_phone = p_val
                    except Exception:
                        pass
                    time.sleep(3)
                    waited += 3
                    check_browser_alive(driver)
                    u_now = (driver.current_url or "").lower()
                    b_now = _normalize_prompt_text(
                        driver.execute_script("return document.body ? document.body.innerText : '';") or ""
                    )
                    if any(k in u_now for k in ("mail.google.com/mail", "myaccount.google.com", "gds.google.com", "speedbump")):
                        resolved = True
                        break
                    if not _detect_google_manual_verification(u_now, b_now):
                        resolved = True
                        break
                if resolved:
                    if manual_entered_phone:
                        _notify_verified_phone(on_phone_verified, manual_entered_phone)
                    continue
                raise LoginFailedError(reason_msg)
        except (BrowserClosedError, LoginFailedError):
            raise
        except Exception as e:
            if is_browser_closed_exception(e):
                raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        finally:
            try:
                driver.implicitly_wait(5)
            except Exception:
                pass


def login_gmail(
    driver: WebDriver,
    gmail_email: str,
    gmail_password: str,
    recovery_email: str = "",
    twofa_secret: str = "",
    hupsms_api_key: Optional[str] = None,
    hupsms_service_id: Optional[int] = None,
    hupsms_network: Optional[str] = None,
    hupsms_prefix: Optional[str] = None,
    on_phone_verified: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Bước 1a (Tab Reg Gmail): Đăng nhập tài khoản Gmail trên trình duyệt Gemlogin.
    """
    gmail_clean = (gmail_email or "").strip()
    logger.info(f"=== BƯỚC 1: Đăng nhập Gmail ({gmail_clean}) ===")

    try:
        _safe_get(driver, GMAIL_INBOX_URL)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(3)

        if _is_gmail_inbox_loaded(driver, gmail_clean):
            _dismiss_gmail_popups(driver)
            logger.info(f"✅ Gmail ({gmail_clean}) đã đăng nhập sẵn trong profile!")
            return True

        cur_u = (driver.current_url or "").lower()
        if "accounts.google.com" not in cur_u:
            _safe_get(driver, GOOGLE_LOGIN_URL)
            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
            time.sleep(2.5)

        if _is_gmail_inbox_loaded(driver, gmail_clean):
            _dismiss_gmail_popups(driver)
            logger.info(f"✅ Gmail ({gmail_clean}) đã đăng nhập sẵn sau khi chuyển trang!")
            return True

        # Kiểm tra màn hình 'Choose an account' (Chọn tài khoản)
        clicked_existing_tile = False
        driver.implicitly_wait(0)
        try:
            if gmail_clean:
                tiles = [
                    el for el in driver.find_elements(
                        By.XPATH,
                        f"//*[@data-identifier='{gmail_clean.lower()}' or @data-email='{gmail_clean.lower()}']"
                        f" | //li[.//*[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{gmail_clean.lower()}')]]",
                    )
                    if el.is_displayed()
                ]
                if tiles:
                    logger.info(f"Chọn tài khoản '{gmail_clean}' trên màn hình Choose an account...")
                    if not safe_click(tiles[0], delay=2.5):
                        driver.execute_script("arguments[0].click();", tiles[0])
                        time.sleep(2.5)
                    clicked_existing_tile = True
                else:
                    use_other_btns = [
                        el for el in driver.find_elements(
                            By.XPATH,
                            "//*[contains(normalize-space(), 'Use another account') or contains(normalize-space(), 'Sử dụng một tài khoản khác')]",
                        )
                        if el.is_displayed()
                    ]
                    email_vis = [
                        el for el in driver.find_elements(By.CSS_SELECTOR, "input#identifierId, input[type='email']")
                        if el.is_displayed()
                    ]
                    if use_other_btns and not email_vis:
                        if not safe_click(use_other_btns[-1], delay=2.0):
                            driver.execute_script("arguments[0].click();", use_other_btns[-1])
                            time.sleep(2.0)
        except Exception:
            pass
        finally:
            driver.implicitly_wait(5)

        if _is_gmail_inbox_loaded(driver, gmail_clean):
            _dismiss_gmail_popups(driver)
            logger.info(f"✅ Đăng nhập Gmail ({gmail_clean}) thành công từ phiên có sẵn!")
            return True

        # Điền địa chỉ Gmail nếu chưa qua bước chọn tài khoản
        if not clicked_existing_tile:
            email_inp = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "input#identifierId, input[type='email'], input[name='identifier']",
                timeout=10,
                condition="visible",
            )
            if not email_inp:
                raise LoginFailedError("Không tìm thấy ô nhập địa chỉ Gmail")

            safe_click(email_inp, delay=0.2)
            safe_send_keys(email_inp, gmail_clean, delay=0.8)
            next_btn = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#identifierNext button, #identifierNext",
                timeout=4,
                condition="clickable",
            )
            if next_btn:
                if not safe_click(next_btn, delay=2.5):
                    driver.execute_script("arguments[0].click();", next_btn)
                    time.sleep(2.5)
            else:
                email_inp.send_keys(Keys.RETURN)
                time.sleep(2.5)

        # Kiểm tra tài khoản không tồn tại hoặc bị khóa ngay sau bước nhập email
        try:
            b_after_email = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
            if "couldn't find your google account" in b_after_email or "không tìm thấy tài khoản google" in b_after_email:
                raise LoginFailedError(f"Tài khoản Gmail không tồn tại: {gmail_clean}")
        except LoginFailedError:
            raise
        except Exception:
            pass

        # Điền mật khẩu Gmail
        pwd_inp = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "input[name='Passwd'], #password input[type='password'], input[type='password']",
            timeout=12,
            condition="visible",
        )
        if pwd_inp:
            if not gmail_password:
                raise LoginFailedError("Gmail yêu cầu nhập mật khẩu nhưng dữ liệu thiếu password")
            safe_click(pwd_inp, delay=0.2)
            safe_send_keys(pwd_inp, gmail_password, delay=0.8)
            pwd_next = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "#passwordNext button, #passwordNext",
                timeout=4,
                condition="clickable",
            )
            if pwd_next:
                if not safe_click(pwd_next, delay=3.0):
                    driver.execute_script("arguments[0].click();", pwd_next)
                    time.sleep(3.0)
            else:
                pwd_inp.send_keys(Keys.RETURN)
                time.sleep(3.0)

        # Kiểm tra sai mật khẩu hoặc tài khoản bị vô hiệu hóa
        try:
            b_after_pwd = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
            if (
                "wrong password" in b_after_pwd
                or "sai mật khẩu" in b_after_pwd
                or "mật khẩu không chính xác" in b_after_pwd
            ):
                raise LoginFailedError("Sai mật khẩu Gmail")
            if (
                "account disabled" in b_after_pwd
                or "your account has been disabled" in b_after_pwd
                or "tài khoản đã bị vô hiệu hóa" in b_after_pwd
            ):
                raise LoginFailedError("Tài khoản Gmail bị vô hiệu hóa (Gmail die)")
        except LoginFailedError:
            raise
        except Exception:
            pass

        _handle_google_post_login_prompts(
            driver,
            gmail_clean,
            recovery_email,
            twofa_secret,
            gmail_password=gmail_password,
            hupsms_api_key=hupsms_api_key,
            hupsms_service_id=hupsms_service_id,
            hupsms_network=hupsms_network,
            hupsms_prefix=hupsms_prefix,
            on_phone_verified=on_phone_verified,
        )

        if not _is_gmail_inbox_loaded(driver):
            _safe_get(driver, GMAIL_INBOX_URL)
            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
            time.sleep(3)
            _handle_google_post_login_prompts(
                driver,
                gmail_clean,
                recovery_email,
                twofa_secret,
                gmail_password=gmail_password,
                hupsms_api_key=hupsms_api_key,
                hupsms_service_id=hupsms_service_id,
                hupsms_network=hupsms_network,
                hupsms_prefix=hupsms_prefix,
                on_phone_verified=on_phone_verified,
            )

        if _is_gmail_inbox_loaded(driver):
            _dismiss_gmail_popups(driver)
            logger.info(f"✅ Đăng nhập Gmail ({gmail_clean}) thành công!")
            return True

        logger.error(f"❌ Chưa vào được hộp thư Gmail. URL hiện tại: {driver.current_url}")
        return False

    except (BrowserClosedError, LoginFailedError):
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi đăng nhập Gmail: {e}")
        raise LoginFailedError(f"Lỗi đăng nhập Gmail: {e}")


def _snapshot_forwarding_inbox_ids(forward_email: str) -> set:
    """Chụp danh sách ID email hiện có trên nguyenmaucole.io.vn và fviainboxes.com trước khi bấm gửi mã."""
    addr = forward_email.strip().lower()
    prefix, dom = addr.split("@", 1)
    seen = set()

    try:
        nmc_url = f"{TEMPMAIL_API_URL}?address={prefix}@{TEMPMAIL_DOMAIN}"
        req = urllib.request.Request(nmc_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(data, list):
            for m in data:
                if isinstance(m, dict) and m.get("id") is not None:
                    seen.add(f"nmc:{m.get('id')}")
    except Exception:
        pass

    if "fvia" in dom or "dropinboxes" in dom:
        try:
            r = requests.get(
                FVIA_MESSAGES_API,
                params={"username": prefix, "domain": dom},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if r.status_code == 200:
                for msg in (r.json().get("result") or []):
                    if isinstance(msg, dict) and msg.get("id") is not None:
                        seen.add(f"fvia:{msg.get('id')}")
        except Exception:
            pass

    return seen


def _extract_gmail_fwd_code_and_link(subject: str, body_html: str) -> Tuple[str, str]:
    """Trích xuất mã xác nhận / OTP (6-12 chữ số) và link xác nhận (vf-...) từ thư Google Forwarding."""
    code = ""
    vf_link = ""

    # 1. Tìm mã trong tiêu đề dạng (#123456789) hoặc (123456789) hoặc #123456789
    m_subj = re.search(r"\(#?(\d{6,12})\)", subject or "")
    if not m_subj:
        m_subj = re.search(r"#(\d{6,12})\b", subject or "")
    if m_subj:
        code = m_subj.group(1)

    # 2. Tìm link xác nhận https://mail-settings.google.com/mail/vf-... hoặc isolated.mail.google.com/mail/vf-...
    cleaned_html = re.sub(r"<wbr\s*/?>", "", body_html or "", flags=re.IGNORECASE)
    href_links = re.findall(r'href=["\'](https?://[^"\']+)["\']', cleaned_html)
    plain_links = re.findall(r"(https?://[^\s<>\"']+)", cleaned_html)
    for rl in href_links + plain_links:
        clean_l = html.unescape(rl).strip().rstrip(".,;)")
        if (
            "/mail/vf-" in clean_l
            or "mail-settings.google.com/mail/vf" in clean_l
            or "isolated.mail.google.com/mail/vf" in clean_l
        ):
            vf_link = clean_l
            break

    # 3. Tìm mã xác nhận / OTP theo từ khóa trong nội dung thư (tránh bắt nhầm số trong URL/ID khi thư chỉ gửi link vf-)
    if not code:
        text_no_urls = re.sub(r"https?://\S+", " ", cleaned_html)
        text_clean = html.unescape(re.sub(r"<[^>]+>", " ", text_no_urls))
        text_clean = re.sub(r"\s+", " ", text_clean).strip()
        m_body = re.search(
            r"(?:Confirmation\s*code|Verification\s*code|Mã\s*xác\s*nhận|Mã\s*xác\s*minh|mã\s*OTP)[^0-9]{0,40}(\d{6,12})\b",
            text_clean,
            flags=re.IGNORECASE,
        )
        if not m_body:
            m_body = re.search(
                r"(?:code\s*(?:is|:)|mã[^0-9]{0,15}(?:là|:))[^0-9]{0,20}(\d{6,12})\b",
                text_clean,
                flags=re.IGNORECASE,
            )
        if m_body:
            code = m_body.group(1)

    return code, vf_link


def _fetch_nmc_fwd_browser(driver: WebDriver, forward_email: str) -> Tuple[str, str]:
    """Fallback mở web mail.nguyenmaucole.io.vn trên tab phụ để lấy OTP/link xác nhận Gmail Forwarding."""
    prefix = forward_email.split("@")[0].strip().lower()
    main_handle = driver.current_window_handle
    logger.info(f"Thử mở mail.nguyenmaucole.io.vn trên trình duyệt để lấy OTP/link cho {forward_email}...")
    try:
        driver.switch_to.new_window("tab")
        _safe_get(driver, "https://mail.nguyenmaucole.io.vn/")
        wait_for_page_load(driver, timeout=15)
        time.sleep(1.5)

        prefix_inp = wait_for_element(driver, By.ID, "email-prefix", timeout=6, condition="visible")
        if prefix_inp:
            safe_send_keys(prefix_inp, prefix, delay=0.4)
            for _ in range(5):
                try:
                    check_btn = wait_for_element(
                        driver, By.XPATH, "//button[contains(text(), 'Kiểm tra Thư')]", timeout=5, condition="clickable"
                    )
                    if check_btn:
                        if not safe_click(check_btn, delay=3.0):
                            driver.execute_script("arguments[0].click();", check_btn)
                            time.sleep(3.0)
                except Exception:
                    time.sleep(1.5)

                for item_idx in range(5):
                    try:
                        mail_items = [
                            el for el in driver.find_elements(By.CSS_SELECTOR, ".mail-item")
                            if el.is_displayed()
                        ]
                        if item_idx >= len(mail_items):
                            break
                        item = mail_items[item_idx]
                        subj_txt = item.text or ""
                        if not safe_click(item, delay=1.2):
                            driver.execute_script("arguments[0].click();", item)
                            time.sleep(1.2)
                        view_body = wait_for_element(driver, By.ID, "view-body", timeout=4, condition="visible")
                        if view_body:
                            combined_body = (view_body.get_attribute("innerHTML") or "") + "\n" + (view_body.text or "")
                            code, vf_link = _extract_gmail_fwd_code_and_link(subj_txt, combined_body)
                            if code or vf_link:
                                logger.info(
                                    f"✅ Lấy được xác nhận Gmail Forwarding trên web mail.nguyenmaucole.io.vn "
                                    f"(code={code or 'N/A'}, link={'CÓ' if vf_link else 'KHÔNG'})"
                                )
                                return code, vf_link
                    except Exception:
                        time.sleep(0.5)
                        continue
                time.sleep(2.0)
    except Exception as e:
        logger.debug(f"Fallback web mail.nguyenmaucole.io.vn lỗi: {e}")
    finally:
        try:
            if driver.current_window_handle != main_handle and len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(main_handle)
        except Exception:
            pass
    return "", ""


def _fetch_fvia_fwd_browser(driver: WebDriver, forward_email: str) -> Tuple[str, str]:
    """Fallback mở web fviainboxes.com trên tab phụ để lấy OTP/link xác nhận Gmail Forwarding."""
    prefix, dom = forward_email.strip().lower().split("@", 1)
    main_handle = driver.current_window_handle
    logger.info(f"Mở fviainboxes.com trên trình duyệt để lấy OTP Gmail Forwarding cho {forward_email}...")
    try:
        driver.switch_to.new_window("tab")
        _safe_get(driver, "https://fviainboxes.com/")
        wait_for_page_load(driver, timeout=15)
        time.sleep(2.0)

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
                if domain_btn and dom not in (domain_btn.text or "").lower():
                    if not safe_click(domain_btn, delay=0.8):
                        driver.execute_script("arguments[0].click();", domain_btn)
                        time.sleep(0.8)
                    opt = wait_for_element(
                        driver,
                        By.XPATH,
                        f"//li[contains(normalize-space(), '{dom}')] | //*[@role='option'][contains(normalize-space(), '{dom}')]",
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
                    if el.is_displayed()
                    and any(k in (el.text or "").lower() for k in ("forwarding", "chuyển tiếp", "google", "gmail"))
                ]
                target_item = mail_items[0] if mail_items else wait_for_element(
                    driver, By.CSS_SELECTOR, "ul li.group, main ul li", timeout=3, condition="clickable"
                )
                if target_item:
                    subj_txt = target_item.text or ""
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
                        code, vf_link = _extract_gmail_fwd_code_and_link(subj_txt, srcdoc)
                        if code or vf_link:
                            logger.info(f"✅ Lấy được OTP/link Gmail Forwarding trên web fviainboxes.com (code={code or 'N/A'})")
                            return code, vf_link
                refresh_btn = wait_for_element(driver, By.XPATH, "//button[contains(., 'Get Email')]", timeout=2, condition="clickable")
                if refresh_btn:
                    safe_click(refresh_btn, delay=3.0)
                else:
                    time.sleep(3.0)
    except Exception as e:
        logger.debug(f"Fallback web fviainboxes.com lỗi: {e}")
    finally:
        try:
            if driver.current_window_handle != main_handle and len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(main_handle)
        except Exception:
            pass
    return "", ""


def _fetch_gmail_forwarding_confirmation(
    driver: WebDriver,
    forward_email: str,
    seen_ids: set,
    timeout: int = 120,
) -> Tuple[str, str]:
    """
    Chờ thư xác nhận Forwarding từ Google (forwarding-noreply@google.com) gửi về forward_email.
    Trả về (confirmation_code, confirmation_link).
    """
    addr = forward_email.strip().lower()
    prefix, dom = addr.split("@", 1)
    is_fvia = "fvia" in dom or "dropinboxes" in dom
    logger.info(f"Đang chờ OTP / thư xác nhận Gmail Forwarding gửi về {addr} (tối đa {timeout}s)...")

    start_time = time.time()
    tried_browser_fallback = False
    while time.time() - start_time < timeout:
        check_browser_alive(driver)

        if not is_fvia:
            try:
                api_url = f"{TEMPMAIL_API_URL}?address={addr}"
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if isinstance(data, list):
                    for m in data:
                        if not isinstance(m, dict):
                            continue
                        mid = m.get("id")
                        if f"nmc:{mid}" in seen_ids:
                            continue
                        subj = str(m.get("subject") or "")
                        sender = str(m.get("sender") or m.get("from") or "")
                        body_html = (
                            str(m.get("body_html") or "")
                            + "\n"
                            + str(m.get("body_text") or "")
                            + "\n"
                            + str(m.get("text") or "")
                        )
                        combined_low = (subj + " " + sender + " " + body_html[:400]).lower()
                        if any(k in combined_low for k in ("forwarding", "chuyển tiếp", "google", "gmail", "xác nhận")):
                            code, vf_link = _extract_gmail_fwd_code_and_link(subj, body_html)
                            if code or vf_link:
                                logger.info(f"✅ Đã nhận OTP/thư xác nhận Gmail Forwarding (code={code or 'N/A'})")
                                return code, vf_link
                        seen_ids.add(f"nmc:{mid}")
            except Exception as e:
                logger.debug(f"Lỗi đọc API nguyenmaucole.io.vn: {e}")

            if not tried_browser_fallback and (time.time() - start_time) >= 40:
                tried_browser_fallback = True
                code, vf_link = _fetch_nmc_fwd_browser(driver, addr)
                if code or vf_link:
                    return code, vf_link
        else:
            need_fvia_browser = False
            try:
                r = requests.get(
                    FVIA_MESSAGES_API,
                    params={"username": prefix, "domain": dom},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10,
                )
                if r.status_code == 403:
                    need_fvia_browser = True
                elif r.status_code == 200:
                    for msg in (r.json().get("result") or []):
                        mid = str(msg.get("id", ""))
                        if not mid or f"fvia:{mid}" in seen_ids:
                            continue
                        subj = str(msg.get("subject") or "")
                        sender = str(msg.get("from") or msg.get("sender") or "")
                        if any(k in (subj + " " + sender).lower() for k in ("forwarding", "chuyển tiếp", "google", "gmail", "xác nhận")):
                            br = requests.get(
                                FVIA_MESSAGE_API,
                                params={"username": prefix, "domain": dom, "id": mid},
                                headers={"User-Agent": "Mozilla/5.0"},
                                timeout=10,
                            )
                            body_html = ""
                            if br.status_code == 200:
                                try:
                                    rj = br.json()
                                    body_html = rj if isinstance(rj, str) else str(rj.get("result", "") or "")
                                except Exception:
                                    body_html = br.text
                            code, vf_link = _extract_gmail_fwd_code_and_link(subj, body_html)
                            if code or vf_link:
                                logger.info(f"✅ Đã nhận OTP/thư xác nhận Gmail Forwarding trên {dom} (code={code or 'N/A'})")
                                return code, vf_link
                        seen_ids.add(f"fvia:{mid}")
            except Exception as e:
                logger.debug(f"Lỗi đọc API fviainboxes.com: {e}")

            if (need_fvia_browser or (time.time() - start_time) >= 35) and not tried_browser_fallback:
                tried_browser_fallback = True
                code, vf_link = _fetch_fvia_fwd_browser(driver, addr)
                if code or vf_link:
                    return code, vf_link

        elapsed = int(time.time() - start_time)
        if elapsed > 0 and elapsed % 9 == 0:
            logger.info(f"⏳ Đang chờ OTP xác nhận Gmail Forwarding tại {addr}... ({elapsed}s/{timeout}s)")
        time.sleep(3)

    return "", ""


def _find_gmail_dialog_input(driver: WebDriver, for_otp: bool = False):
    """
    Tìm ô <input> bên trong hộp thoại Gmail (hỗ trợ cả Material 3 div[role='dialog'] mới lẫn div.Kj-JD[role='alertdialog'] cũ).
    """
    driver.implicitly_wait(0)
    try:
        candidates = driver.find_elements(
            By.CSS_SELECTOR,
            "div[role='dialog'] input, div[role='alertdialog'] input, div[aria-modal='true'] input, div.Kj-JD input",
        )
        for el in candidates:
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                t = (el.get_attribute("type") or "text").lower()
                if t in ("hidden", "checkbox", "radio", "submit", "button", "file"):
                    continue
                nm = (el.get_attribute("name") or "").lower()
                if nm == "q":
                    continue
                if for_otp:
                    act = (el.get_attribute("act") or "").lower()
                    aria = (el.get_attribute("aria-label") or "").lower()
                    if "email" in aria and "mã" not in aria and "code" not in aria and "otp" not in aria:
                        continue
                    if act == "verifytext":
                        return el
                return el
            except Exception:
                continue

        # Fallback: nếu hộp thoại đang mở và cursor đang focus sẵn vào ô input (như trong ảnh chụp màn hình Gmail mới)
        if not for_otp:
            active_el = driver.execute_script(
                "const a = document.activeElement;"
                "if (!a || a.tagName !== 'INPUT') return null;"
                "if ((a.name || '').toLowerCase() === 'q') return null;"
                "const dlg = a.closest('[role=\"dialog\"], [role=\"alertdialog\"], [aria-modal=\"true\"], .Kj-JD');"
                "return dlg ? a : null;"
            )
            if active_el:
                return active_el
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return None


def _click_gmail_dialog_button(driver: WebDriver, labels: tuple, name_attrs: tuple = ()) -> bool:
    """Tìm và bấm nút trong hộp thoại Gmail (role='dialog' / role='alertdialog' / aria-modal='true' / Kj-JD)."""
    driver.implicitly_wait(0)
    try:
        for nm in name_attrs:
            btns = [
                b for b in driver.find_elements(
                    By.CSS_SELECTOR,
                    f"div[role='dialog'] button[name='{nm}'], div[role='alertdialog'] button[name='{nm}'], "
                    f"div[aria-modal='true'] button[name='{nm}'], div.Kj-JD button[name='{nm}']",
                )
                if b.is_displayed() and b.is_enabled()
            ]
            if btns:
                if not safe_click(btns[0], delay=1.8):
                    driver.execute_script("arguments[0].click();", btns[0])
                    time.sleep(1.8)
                return True

        dlg_buttons = driver.find_elements(
            By.CSS_SELECTOR,
            "div[role='dialog'] button, div[role='alertdialog'] button, div[aria-modal='true'] button, div.Kj-JD button, "
            "div[role='dialog'] [role='button'], div[aria-modal='true'] [role='button'], "
            "div[role='dialog'] input[type='button'], div[role='dialog'] input[type='submit']",
        )
        labels_low = tuple(lbl.lower() for lbl in labels)
        for btn in dlg_buttons:
            try:
                if not btn.is_displayed() or not btn.is_enabled():
                    continue
                txt = re.sub(
                    r"\s+",
                    " ",
                    (btn.text or "") or (btn.get_attribute("value") or "") or (btn.get_attribute("aria-label") or ""),
                ).strip().lower()
                if txt in labels_low:
                    if not safe_click(btn, delay=1.8):
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.8)
                    return True
            except Exception:
                continue

        # Fallback: contains match (bỏ qua Hủy / Cancel / Back)
        for btn in dlg_buttons:
            try:
                if not btn.is_displayed() or not btn.is_enabled():
                    continue
                txt = ((btn.text or "") + " " + (btn.get_attribute("value") or "")).strip().lower()
                if any(bad in txt for bad in ("hủy", "cancel", "back", "quay lại")):
                    continue
                if any(lbl in txt for lbl in labels_low):
                    if not safe_click(btn, delay=1.8):
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.8)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return False


def _find_fwd_verify_code_input(driver: WebDriver):
    """
    Tìm ô nhập OTP / Mã xác nhận trên trang #settings/fwdandpop (hoặc trong dialog xác nhận).
    """
    driver.implicitly_wait(0)
    try:
        # 1. Input chuẩn của Gmail Settings (#settings/fwdandpop)
        for el in driver.find_elements(By.CSS_SELECTOR, "input[act='verifyText']"):
            if el.is_displayed() and el.is_enabled():
                return el

        # 2. Input nằm cạnh label 'Mã xác nhận' / 'Confirmation code' hoặc nút 'Xác minh' / 'Verify'
        row_inputs = driver.find_elements(
            By.XPATH,
            "//tr[.//*[contains(., 'Mã xác nhận') or contains(., 'Confirmation code') or contains(., 'Xác minh') or contains(., 'Verify')]]"
            "//input[@type='text' or @type='tel' or @type='number' or not(@type)]",
        )
        for el in row_inputs:
            if el.is_displayed() and el.is_enabled() and (el.get_attribute("name") or "").lower() != "q":
                return el

        # 3. Input nằm trong dialog (nếu Gmail hỏi OTP trực tiếp trong popup dialog)
        dlg_inp = _find_gmail_dialog_input(driver, for_otp=True)
        if dlg_inp:
            return dlg_inp
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return None


def _click_fwd_verify_button(driver: WebDriver, near_input=None) -> bool:
    """Bấm nút 'Xác minh' / 'Verify' ngay cạnh ô OTP hoặc trong dialog."""
    driver.implicitly_wait(0)
    try:
        # 1. Nút chuẩn Gmail Settings: input[act='verify']
        for btn in driver.find_elements(By.CSS_SELECTOR, "input[act='verify']"):
            if btn.is_displayed() and btn.is_enabled():
                if not safe_click(btn, delay=2.5):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(2.5)
                return True

        # 2. Nút cùng hàng / cùng khối với ô OTP
        if near_input is not None:
            try:
                container = near_input.find_element(By.XPATH, "./ancestor::*[self::tr or self::div[@role='dialog'] or self::td][1]")
                btns = container.find_elements(
                    By.XPATH,
                    ".//input[@type='button' or @type='submit'] | .//button",
                )
                for b in btns:
                    if not b.is_displayed() or not b.is_enabled():
                        continue
                    txt = ((b.text or "") + " " + (b.get_attribute("value") or "")).strip().lower()
                    if any(k in txt for k in ("xác minh", "verify", "xác nhận", "confirm", "tiếp theo", "next")):
                        if not safe_click(b, delay=2.5):
                            driver.execute_script("arguments[0].click();", b)
                            time.sleep(2.5)
                        return True
            except Exception:
                pass

        # 3. Fallback tìm nút 'Xác minh' / 'Verify' toàn trang
        global_btns = driver.find_elements(
            By.XPATH,
            "//input[(@type='button' or @type='submit') and (@value='Xác minh' or @value='Verify')]"
            " | //button[normalize-space()='Xác minh' or normalize-space()='Verify' or .//span[normalize-space()='Xác minh' or normalize-space()='Verify']]",
        )
        for b in global_btns:
            if b.is_displayed() and b.is_enabled():
                if not safe_click(b, delay=2.5):
                    driver.execute_script("arguments[0].click();", b)
                    time.sleep(2.5)
                return True
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return False


def _get_forwarding_select_emails(driver: WebDriver) -> list:
    """
    Lấy danh sách các địa chỉ email ĐÃ XÁC MINH từ giao diện #settings/fwdandpop:
      - Các <option> trong dropdown chọn email chuyển tiếp
      - Giá trị <input> / HTML trong hàng radio 'Chuyển tiếp bản sao...' (khi chỉ có 1 địa chỉ, Gmail ẩn dropdown)
      - Banner xác nhận đang bật ('Bạn đang chuyển tiếp email của mình đến ...')
    """
    emails = []
    email_pat = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

    def _add(addr: str) -> None:
        raw = unicodedata.normalize("NFC", (addr or "")).strip().lower()
        if any(raw.startswith(p) for p in ("xóa ", "xoá ", "remove ", "delete ")):
            return
        m = email_pat.search(raw)
        if not m:
            return
        a = m.group(0).strip().rstrip(".,;")
        if a not in emails:
            emails.append(a)

    driver.implicitly_wait(0)
    try:
        radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio'][name='sx_em'][value='1']")
        search_selects = []
        if radios:
            try:
                row_tr = radios[0].find_element(By.XPATH, "./ancestor::tr[1]")
                search_selects = row_tr.find_elements(By.TAG_NAME, "select")
                for inp in row_tr.find_elements(By.TAG_NAME, "input"):
                    if (inp.get_attribute("type") or "").lower() != "radio":
                        _add(inp.get_attribute("value") or "")
                # Chỉ quét HTML ngoài thẻ <select> để tránh nhận nhầm <option>Xóa unverified@...</option>
                row_html = row_tr.get_attribute("innerHTML") or ""
                row_html_no_sel = re.sub(r"<select\b[^>]*>[\s\S]*?</select>", " ", row_html, flags=re.IGNORECASE)
                for found in email_pat.findall(row_html_no_sel):
                    _add(found)
            except Exception:
                pass
        if not search_selects:
            search_selects = driver.find_elements(By.TAG_NAME, "select")

        for sel_el in search_selects:
            for opt in sel_el.find_elements(By.TAG_NAME, "option"):
                txt = unicodedata.normalize("NFC", (opt.text or "")).strip().lower()
                val = unicodedata.normalize("NFC", (opt.get_attribute("value") or "")).strip().lower()
                if any(k in txt for k in ("xóa ", "xoá ", "remove ", "delete ")):
                    continue
                for cand in (txt, val):
                    if "@" in cand:
                        _add(cand)

        body_raw = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        body_nfc = unicodedata.normalize("NFC", body_raw).lower()
        for m in re.finditer(
            r"(?:đang chuyển tiếp email của mình đến|forwarding your email to|"
            r"chuyển tiếp bản sao của thư đến tới|forwarding a copy of incoming mail to)"
            r"\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
            body_nfc,
        ):
            _add(m.group(1))
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return emails


def _find_existing_forwarding_row_email(
    driver: WebDriver,
    target_domains: Tuple[str, ...],
    exclude_email: str = "",
) -> str:
    """
    Kiểm tra hàng 'Chuyển tiếp bản sao của thư đến tới:' trên #settings/fwdandpop.
    Nếu đã có sẵn email thuộc domain mục tiêu (vd: @nguyenmaucole.io.vn), trả về địa chỉ đó.
    """
    doms = tuple(dict.fromkeys(d.strip().lstrip("@").lower() for d in target_domains if (d or "").strip()))
    if not doms:
        return ""
    exclude_low = (exclude_email or "").strip().lower()
    email_pat = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

    candidates = []

    def _push(raw_str: str) -> None:
        norm = unicodedata.normalize("NFC", (raw_str or "")).strip().lower()
        if not norm:
            return
        if any(norm.startswith(p) for p in ("xóa ", "xoá ", "remove ", "delete ")):
            return
        if any(k in norm for k in ("đang chờ", "in use", "pending")):
            return
        for m in email_pat.finditer(norm):
            addr = m.group(0).strip().rstrip(".,;")
            if addr and addr != exclude_low and addr not in candidates:
                candidates.append(addr)

    driver.implicitly_wait(0)
    try:
        radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio'][name='sx_em'][value='1']")
        row_tr = None
        search_selects = []
        if radios:
            target_radio = next((r for r in radios if r.is_displayed()), radios[0])
            try:
                row_tr = target_radio.find_element(By.XPATH, "./ancestor::tr[1]")
            except Exception:
                row_tr = None

        # Ưu tiên 1: Giá trị đang hiện trực tiếp trong ô <input> hoặc <option> đang chọn của hàng 'Chuyển tiếp bản sao...'
        if row_tr is not None:
            for inp in row_tr.find_elements(By.TAG_NAME, "input"):
                if (inp.get_attribute("type") or "").lower() != "radio":
                    _push(inp.get_attribute("value") or "")
            search_selects = row_tr.find_elements(By.TAG_NAME, "select")
            for sel_el in search_selects:
                try:
                    sel_opt = Select(sel_el).first_selected_option
                    _push((sel_opt.text or "") + " " + (sel_opt.get_attribute("value") or ""))
                except Exception:
                    pass

        # Ưu tiên 2: Banner đang chuyển tiếp & nội dung text/HTML ngoài <select> trong hàng 'Chuyển tiếp bản sao...'
        body_raw = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        body_nfc = unicodedata.normalize("NFC", body_raw).lower()
        for m in re.finditer(
            r"(?:đang chuyển tiếp email của mình đến|forwarding your email to|"
            r"chuyển tiếp bản sao của thư đến tới|forwarding a copy of incoming mail to)"
            r"\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
            body_nfc,
        ):
            _push(m.group(1))

        if row_tr is not None:
            _push(row_tr.text or "")
            try:
                row_html = row_tr.get_attribute("innerHTML") or ""
                row_html_no_sel = re.sub(r"<select\b[^>]*>[\s\S]*?</select>", " ", row_html, flags=re.IGNORECASE)
                _push(row_html_no_sel)
            except Exception:
                pass

        # Ưu tiên 3: Các <option> hợp lệ khác nằm trong <select> của hàng chuyển tiếp
        if not search_selects:
            search_selects = driver.find_elements(By.TAG_NAME, "select")
        for sel_el in search_selects:
            for opt in sel_el.find_elements(By.TAG_NAME, "option"):
                txt = unicodedata.normalize("NFC", (opt.text or "")).strip().lower()
                val = unicodedata.normalize("NFC", (opt.get_attribute("value") or "")).strip().lower()
                if any(k in txt for k in ("xóa ", "xoá ", "remove ", "delete ", "đang chờ", "pending", "in use")):
                    continue
                _push(txt)
                _push(val)
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass

    for dom in doms:
        suffix = f"@{dom}"
        for addr in candidates:
            if addr.endswith(suffix):
                return addr
    return ""


def _handle_gmail_forwarding_popups(
    driver: WebDriver,
    main_handle: str,
    handles_before: set,
    gmail_password: str = "",
    twofa_secret: str = "",
) -> None:
    """
    Xử lý các bước sau khi bấm 'Tiếp theo' / 'Next' trong dialog thêm Gmail Forwarding:
      - Cửa sổ popup 'Confirm forwarding address' ('Xác nhận địa chỉ chuyển tiếp') -> bấm 'Proceed' / 'Tiếp tục'
      - Cửa sổ 'Verify it's you' ('Xác minh đó là bạn') trên accounts.google.com -> nhập Mật khẩu / 2FA / Continue
      - Hộp thoại thông báo 'Mã xác nhận đã được gửi...' / 'A confirmation code has been sent...' -> bấm 'OK' / 'Đã hiểu'
    """
    clicked_ok_alert = False
    seen_pop_sig = set()
    for attempt_idx in range(28):
        time.sleep(1.0)
        check_browser_alive(driver)
        new_handles = [h for h in driver.window_handles if h not in handles_before and h != main_handle]

        if not new_handles:
            # Kiểm tra trên tab chính Gmail (hộp thoại Material hoặc alertdialog hoặc ô verifyText đã hiện)
            try:
                if main_handle in driver.window_handles:
                    driver.switch_to.window(main_handle)
                    if attempt_idx >= 3 and _find_fwd_verify_code_input(driver) is not None and not _click_gmail_dialog_button(
                        driver, ("ok", "đã hiểu", "got it", "proceed", "tiếp tục"), name_attrs=("ok",)
                    ):
                        return

                    driver.implicitly_wait(0)
                    dlg_text = ""
                    try:
                        dlgs = [
                            d for d in driver.find_elements(
                                By.CSS_SELECTOR,
                                "div[role='dialog'], div[role='alertdialog'], div[aria-modal='true'], div.Kj-JD",
                            )
                            if d.is_displayed()
                        ]
                        if dlgs:
                            dlg_text = (dlgs[-1].text or "").lower()
                    finally:
                        driver.implicitly_wait(5)

                    if dlg_text:
                        # 1. Nếu hộp thoại trên trang yêu cầu Proceed / Tiếp tục (Confirm forwarding)
                        if any(k in dlg_text for k in ("forwarding mail", "chuyển tiếp thư", "confirm forwarding", "xác nhận địa chỉ")):
                            if _click_gmail_dialog_button(driver, ("proceed", "tiếp tục", "confirm", "xác nhận")):
                                logger.info("Đã bấm 'Tiếp tục'/'Proceed' trên hộp thoại xác nhận Forwarding")
                                continue

                        # 2. Nếu hộp thoại báo đã gửi mã / đường liên kết xác nhận -> bấm OK
                        if any(
                            k in dlg_text
                            for k in (
                                "confirmation code",
                                "confirmation link",
                                "mã xác nhận",
                                "đường liên kết xác nhận",
                                "has been sent",
                                "đã được gửi",
                                "verify permission",
                                "xác minh quyền",
                            )
                        ):
                            if _click_gmail_dialog_button(driver, ("ok", "đã hiểu", "got it", "đóng", "close"), name_attrs=("ok",)):
                                logger.info("Đã bấm 'OK' trên hộp thoại thông báo đã gửi OTP/link xác nhận Gmail Forwarding")
                                clicked_ok_alert = True
                                return

                        # 3. Fallback nút OK trực tiếp (tránh đóng nhầm hộp thoại nhập email nếu vẫn còn trên màn hình)
                        if "hãy nhập" not in dlg_text and "enter a new forwarding" not in dlg_text:
                            if _click_gmail_dialog_button(driver, ("ok", "đã hiểu"), name_attrs=("ok",)):
                                logger.info("Đã đóng hộp thoại 'OK' trên Gmail")
                                clicked_ok_alert = True
                                return
            except Exception:
                pass

            if clicked_ok_alert:
                return
            continue

        for pop_h in list(new_handles):
            if pop_h not in driver.window_handles:
                continue
            try:
                driver.switch_to.window(pop_h)
                pop_url = (driver.current_url or "").lower()
                pop_title = (driver.title or "").strip()
                sig = (pop_h, pop_url.split("?", 1)[0], pop_title)
                if sig not in seen_pop_sig:
                    seen_pop_sig.add(sig)
                    logger.info(f"Đang xử lý popup Gmail Forwarding: {pop_url.split('?', 1)[0]} | title={pop_title!r}")
                driver.implicitly_wait(0)
                body_pop = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()

                # 1. Popup xác thực lại tài khoản Google ('Verify it's you' trên accounts.google.com)
                if "accounts.google.com" in pop_url:
                    pwd_els = [
                        e for e in driver.find_elements(By.CSS_SELECTOR, "input[name='Passwd'], input[type='password']")
                        if e.is_displayed()
                    ]
                    if pwd_els and gmail_password:
                        logger.info("Popup xác thực Gmail Forwarding yêu cầu nhập lại mật khẩu...")
                        safe_send_keys(pwd_els[0], gmail_password, delay=0.8)
                        next_btns = [
                            b for b in driver.find_elements(By.CSS_SELECTOR, "#passwordNext button, #passwordNext")
                            if b.is_displayed()
                        ]
                        if next_btns:
                            if not safe_click(next_btns[0], delay=2.5):
                                driver.execute_script("arguments[0].click();", next_btns[0])
                                time.sleep(2.5)
                        else:
                            pwd_els[0].send_keys(Keys.RETURN)
                            time.sleep(2.5)
                        continue

                    totp_els = [
                        e for e in driver.find_elements(By.CSS_SELECTOR, "input#totpPin, input[name='totpPin']")
                        if e.is_displayed()
                    ]
                    if totp_els and twofa_secret:
                        logger.info("Popup xác thực Gmail Forwarding yêu cầu nhập mã 2FA...")
                        code = generate_2fa_code(twofa_secret)
                        safe_send_keys(totp_els[0], code, delay=0.8)
                        totp_els[0].send_keys(Keys.RETURN)
                        time.sleep(2.5)
                        continue

                    # Nếu hiện màn hình Passkey -> bấm 'Thử cách khác' / 'Try another way' để chuyển về mật khẩu
                    if ("passkey" in body_pop or "khóa truy cập" in body_pop) and gmail_password:
                        try_btns = [
                            b for b in driver.find_elements(
                                By.XPATH,
                                "//button[contains(., 'Try another way') or contains(., 'Thử cách khác')]"
                                " | //div[@role='button'][contains(., 'Try another way') or contains(., 'Thử cách khác')]",
                            )
                            if b.is_displayed()
                        ]
                        if try_btns:
                            logger.info("Popup xác thực Gmail Forwarding hiện Passkey -> bấm 'Try another way' / 'Thử cách khác'")
                            if not safe_click(try_btns[0], delay=2.0):
                                driver.execute_script("arguments[0].click();", try_btns[0])
                                time.sleep(2.0)
                            continue
                        pwd_opt = [
                            el for el in driver.find_elements(
                                By.XPATH,
                                "//li[.//*[contains(., 'Enter your password') or contains(., 'Nhập mật khẩu') or contains(., 'Confirm your password') or contains(., 'Xác nhận mật khẩu')]]"
                                " | //div[@role='link' or @role='button'][contains(., 'Enter your password') or contains(., 'Nhập mật khẩu') or contains(., 'Confirm your password') or contains(., 'Xác nhận mật khẩu')]",
                            )
                            if el.is_displayed()
                        ]
                        if pwd_opt:
                            logger.info("Popup xác thực Gmail Forwarding chọn xác minh bằng mật khẩu...")
                            if not safe_click(pwd_opt[0], delay=2.0):
                                driver.execute_script("arguments[0].click();", pwd_opt[0])
                                time.sleep(2.0)
                            continue

                    cont_btns = [
                        b for b in driver.find_elements(
                            By.XPATH,
                            "//button[.//span[normalize-space()='Continue' or normalize-space()='Tiếp tục' or normalize-space()='Next' or normalize-space()='Tiếp theo'] or normalize-space()='Continue' or normalize-space()='Tiếp tục' or normalize-space()='Next' or normalize-space()='Tiếp theo']",
                        )
                        if b.is_displayed()
                    ]
                    if cont_btns:
                        logger.info("Popup accounts.google.com bấm 'Continue' / 'Next'...")
                        if not safe_click(cont_btns[0], delay=2.0):
                            driver.execute_script("arguments[0].click();", cont_btns[0])
                            time.sleep(2.0)
                        continue

                # 2. Popup 'Confirm forwarding address' / 'Xác nhận địa chỉ chuyển tiếp' có nút 'Proceed' / 'Tiếp tục'
                proceed_btns = [
                    b for b in driver.find_elements(
                        By.XPATH,
                        "//input[(@type='submit' or @type='button') and (@value='Proceed' or @value='Tiếp tục' or contains(@value, 'Proceed'))]"
                        " | //button[@type='submit' or normalize-space()='Proceed' or normalize-space()='Tiếp tục' or .//span[normalize-space()='Proceed' or normalize-space()='Tiếp tục']]",
                    )
                    if b.is_displayed()
                ]
                if not proceed_btns and any(k in body_pop for k in ("forwarding mail", "chuyển tiếp thư", "forwarding")):
                    proceed_btns = [
                        b for b in driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']")
                        if b.is_displayed()
                    ]

                if proceed_btns:
                    logger.info("Bấm 'Proceed' / 'Tiếp tục' trong popup xác nhận địa chỉ Gmail Forwarding...")
                    if not safe_click(proceed_btns[0], delay=2.5):
                        driver.execute_script("arguments[0].click();", proceed_btns[0])
                        time.sleep(2.5)
                    # Không break ngay — tiếp tục vòng để đóng hộp thoại 'OK' hoặc xử lý bước verify tiếp theo
                    continue
            except Exception as e:
                logger.debug(f"Lỗi xử lý popup Gmail Forwarding: {e}")
            finally:
                try:
                    driver.implicitly_wait(5)
                except Exception:
                    pass

    # Đảm bảo chuyển về tab chính và bấm nút 'OK' trên thông báo 'Đã gửi mã xác nhận...' (nếu còn)
    if not clicked_ok_alert:
        logger.warning(
            f"Hết vòng chờ popup Gmail Forwarding mà chưa đóng được hộp thoại OK "
            f"(window_handles={len(driver.window_handles)})"
        )
    if main_handle in driver.window_handles:
        driver.switch_to.window(main_handle)
        time.sleep(1.5)
        if _click_gmail_dialog_button(driver, ("ok", "đã hiểu", "got it"), name_attrs=("ok",)):
            logger.info("Đã đóng hộp thoại 'OK' sau khi gửi OTP xác nhận Gmail Forwarding")


def _reload_gmail_fwd_settings(driver: WebDriver) -> None:
    """Reload thật sự trang #settings/fwdandpop (tránh bị Chrome bỏ qua do trùng hash URL)."""
    cur = (driver.current_url or "").lower()
    if "mail.google.com" in cur:
        try:
            driver.refresh()
            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        except Exception:
            pass
    if "#settings/fwdandpop" not in (driver.current_url or "").lower():
        _safe_get(driver, GMAIL_FWD_SETTINGS_URL)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
    _wait_for_fwd_settings_ready(driver, timeout=15)
    time.sleep(1.5)


def _find_inline_forwarding_input(row_tr) -> Optional[object]:
    """Tìm ô <input type='text'> inline nằm trong hàng radio 'Chuyển tiếp bản sao...' (Gmail Workspace)."""
    try:
        for el in row_tr.find_elements(
            By.CSS_SELECTOR, "input[type='text'], input[type='email'], input:not([type])"
        ):
            if (
                el.is_displayed()
                and el.is_enabled()
                and not el.get_attribute("disabled")
                and not el.get_attribute("readonly")
            ):
                return el
    except Exception:
        pass
    return None


def _fill_inline_forwarding_input(driver: WebDriver, inp_el, forward_email: str) -> bool:
    """Điền địa chỉ email chuyển tiếp vào ô input inline và kích hoạt sự kiện input/change."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", inp_el)
        time.sleep(0.2)
        safe_click(inp_el, delay=0.2)
        inp_el.send_keys(Keys.CONTROL, "a")
        inp_el.send_keys(Keys.BACKSPACE)
        safe_send_keys(inp_el, forward_email, delay=0.4)
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
            inp_el,
        )
        logger.info(f"Đã điền địa chỉ forwarding '{forward_email}' vào ô input inline trên Gmail")
        return True
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        return False


def _enable_and_save_gmail_forwarding(
    driver: WebDriver,
    forward_email: str,
    force_reload: bool = True,
    allow_fill_inline_input: bool = False,
) -> bool:
    """
    Trên trang #settings/fwdandpop:
    1. Kiểm tra forward_email đã có trong danh sách địa chỉ đã xác minh chưa.
    2. Chọn đúng forward_email trong dropdown (hoặc điền ô input inline của Gmail Workspace khi đã xác minh).
    3. Tích chọn radio 'Chuyển tiếp bản sao của thư đến tới ...' / 'Forward a copy of incoming mail to ...' (input[name='sx_em'][value='1']).
    4. Bấm 'Lưu thay đổi' / 'Save Changes' ở cuối trang.
    """
    if force_reload:
        _reload_gmail_fwd_settings(driver)
    elif "#settings/fwdandpop" not in (driver.current_url or "").lower():
        _safe_get(driver, GMAIL_FWD_SETTINGS_URL)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        _wait_for_fwd_settings_ready(driver, timeout=15)
        time.sleep(1.0)
    else:
        time.sleep(1.5)

    enable_radio = None
    selected_target_email = False
    filled_inline_input = False
    row_text_debug = ""

    for retry_idx in range(2):
        enable_radio = wait_for_element(
            driver,
            By.CSS_SELECTOR,
            "input[type='radio'][name='sx_em'][value='1']",
            timeout=12,
            condition="presence",
        )
        if not enable_radio:
            logger.error("Chưa thấy tùy chọn 'Forward a copy of incoming mail to' (input[name='sx_em'][value='1']) trên Gmail")
            return False

        selected_target_email = False
        try:
            row_tr = enable_radio.find_element(By.XPATH, "./ancestor::tr[1]")
            row_text_debug = (row_tr.text or "").strip().replace("\n", " ")[:180]
            selects = row_tr.find_elements(By.TAG_NAME, "select")
            if not selects:
                selects = driver.find_elements(By.TAG_NAME, "select")
            for sel_el in selects:
                opts = sel_el.find_elements(By.TAG_NAME, "option")
                for idx, opt in enumerate(opts):
                    opt_combined = ((opt.text or "") + " " + (opt.get_attribute("value") or "")).lower()
                    if forward_email.lower() in opt_combined:
                        Select(sel_el).select_by_index(idx)
                        try:
                            driver.execute_script(
                                "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));", sel_el
                            )
                        except Exception:
                            pass
                        selected_target_email = True
                        logger.info(f"Đã chọn địa chỉ forwarding '{forward_email}' trong dropdown")
                        break
                if selected_target_email:
                    break

            # Fallback khi tài khoản chỉ có 1 địa chỉ đã xác minh (Gmail ẩn <select>, hiển thị qua banner / innerHTML / input)
            if not selected_target_email and forward_email.lower() in _get_forwarding_select_emails(driver):
                selected_target_email = True
                logger.info(f"Xác nhận địa chỉ forwarding '{forward_email}' đã được xác minh trên trang cài đặt Gmail")

            # Fallback Gmail Workspace (sau khi xác minh vf-, hàng radio hiển thị ô <input type='text'> trống để nhập địa chỉ đích)
            if not selected_target_email and allow_fill_inline_input:
                inline_inp = _find_inline_forwarding_input(row_tr)
                if inline_inp is not None and _fill_inline_forwarding_input(driver, inline_inp, forward_email):
                    selected_target_email = True
                    filled_inline_input = True
        except BrowserClosedError:
            raise
        except Exception:
            pass

        if selected_target_email:
            break
        if force_reload and retry_idx == 0:
            logger.info(f"Chưa thấy '{forward_email}' sau lần tải đầu -> Refresh lại trang cài đặt Gmail...")
            time.sleep(2.5)
            _reload_gmail_fwd_settings(driver)

    if not selected_target_email:
        logger.warning(
            f"Chưa thấy '{forward_email}' trong danh sách địa chỉ chuyển tiếp đã xác minh trên Gmail "
            f"(radio_row='{row_text_debug}')"
        )
        return False

    was_selected = bool(enable_radio.is_selected() or enable_radio.get_attribute("checked"))
    if not was_selected:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", enable_radio)
        time.sleep(0.3)
        if not safe_click(enable_radio, delay=1.0):
            driver.execute_script("arguments[0].click();", enable_radio)
            time.sleep(1.0)
        logger.info(f"Đã bật radio 'Chuyển tiếp bản sao của thư đến tới {forward_email}'")
    else:
        logger.info(f"Tùy chọn chuyển tiếp tới {forward_email} đã được bật!")

    time.sleep(0.8)

    # Tìm nút 'Lưu thay đổi' / 'Save Changes' ở cuối trang cài đặt Gmail
    save_btns = [
        b for b in driver.find_elements(
            By.XPATH,
            "//button[@guidedhelpid='save_changes_button' or normalize-space()='Save Changes' or normalize-space()='Lưu thay đổi']"
            " | //input[(@type='button' or @type='submit') and (@value='Save Changes' or @value='Lưu thay đổi')]",
        )
        if b.is_displayed()
    ]
    if save_btns:
        save_btn = save_btns[0]
        if save_btn.is_enabled() and not save_btn.get_attribute("disabled"):
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", save_btn)
            time.sleep(0.4)
            if not safe_click(save_btn, delay=3.0):
                driver.execute_script("arguments[0].click();", save_btn)
                time.sleep(3.0)
            logger.info("✅ Đã bấm 'Lưu thay đổi' / 'Save Changes' lưu cài đặt Gmail Forwarding thành công!")
        else:
            logger.info("ℹ️ Nút 'Lưu thay đổi' đang mờ (cài đặt Forwarding tới địa chỉ này đã được lưu sẵn)")

    if filled_inline_input:
        verified_after_save = forward_email.lower() in _get_forwarding_select_emails(driver)
        if not verified_after_save:
            time.sleep(2.0)
            _reload_gmail_fwd_settings(driver)
            verified_after_save = forward_email.lower() in _get_forwarding_select_emails(driver)
        if not verified_after_save:
            logger.warning(f"Sau khi lưu ô inline nhưng Gmail chưa ghi nhận chuyển tiếp tới '{forward_email}'")
            return False
    return True


def _wait_for_fwd_settings_ready(driver: WebDriver, timeout: int = 15) -> bool:
    """Chờ giao diện #settings/fwdandpop (Chuyển tiếp và POP/IMAP) tải xong."""
    start = time.time()
    while time.time() - start < timeout:
        check_browser_alive(driver)
        driver.implicitly_wait(0)
        try:
            markers = driver.find_elements(
                By.CSS_SELECTOR,
                "input[act='add'], input[name='sx_em'], input[act='verifyText']",
            )
            if any(m.is_displayed() for m in markers):
                return True
            add_btns = driver.find_elements(
                By.XPATH,
                "//input[@type='button' and (contains(@value, 'forwarding') or contains(@value, 'chuyển tiếp'))]"
                " | //button[contains(., 'Add a forwarding') or contains(., 'Thêm địa chỉ chuyển tiếp')]",
            )
            if any(b.is_displayed() for b in add_btns):
                return True
        except Exception:
            pass
        finally:
            try:
                driver.implicitly_wait(5)
            except Exception:
                pass
        time.sleep(0.8)
    return False


def setup_gmail_forwarding(
    driver: WebDriver,
    gmail_email: str,
    gmail_password: str = "",
    target_domain: str = TEMPMAIL_DOMAIN,
    recovery_email: str = "",
    twofa_secret: str = "",
    hupsms_api_key: Optional[str] = None,
    hupsms_service_id: Optional[int] = None,
    on_phone_verified: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Bước 1b (Tab Reg Gmail): Cài đặt chuyển tiếp thư từ Gmail về <prefix>@<target_domain> (mặc định @nguyenmaucole.io.vn).
    Trả về địa chỉ forward_email nếu thành công, ngược lại trả về "".
    """
    if not gmail_email or "@" not in gmail_email:
        logger.error("Thiếu địa chỉ Gmail để cài đặt Forwarding")
        return ""

    prefix = gmail_email.split("@")[0].strip().lower()
    target_dom = (target_domain or TEMPMAIL_DOMAIN).strip().lstrip("@").lower()
    forward_email = f"{prefix}@{target_dom}"
    logger.info(f"=== Cài đặt Gmail Forwarding ({gmail_email} -> {forward_email}) ===")

    try:
        if not _is_gmail_inbox_loaded(driver):
            login_gmail(
                driver,
                gmail_email,
                gmail_password,
                recovery_email,
                twofa_secret,
                hupsms_api_key=hupsms_api_key,
                hupsms_service_id=hupsms_service_id,
                on_phone_verified=on_phone_verified,
            )

        _safe_get(driver, GMAIL_FWD_SETTINGS_URL)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        _wait_for_fwd_settings_ready(driver, timeout=15)
        time.sleep(1.5)
        _dismiss_gmail_popups(driver)

        # Nếu hàng 'Chuyển tiếp bản sao của thư đến tới' đã có sẵn email @nguyenmaucole.io.vn (hoặc target_dom) -> dùng luôn & lưu cột H
        match_domains = (target_dom,) if target_dom == TEMPMAIL_DOMAIN else (target_dom, TEMPMAIL_DOMAIN)
        existing_row_email = _find_existing_forwarding_row_email(
            driver, match_domains, exclude_email=gmail_email
        )
        if existing_row_email:
            logger.info(
                f"ℹ️ Đã tồn tại email chuyển tiếp '{existing_row_email}' ở hàng 'Chuyển tiếp bản sao của thư đến tới' -> Sử dụng luôn!"
            )
            if _enable_and_save_gmail_forwarding(
                driver,
                existing_row_email,
                force_reload=False,
                allow_fill_inline_input=True,
            ):
                return existing_row_email

        # Kiểm tra xem forward_email đã có trong dropdown địa chỉ đã xác minh chưa
        verified_emails = _get_forwarding_select_emails(driver)
        if forward_email.lower() in verified_emails:
            logger.info(f"ℹ️ Địa chỉ {forward_email} đã có trong danh sách đã xác minh -> Kiểm tra bật chuyển tiếp & lưu...")
            if _enable_and_save_gmail_forwarding(
                driver, forward_email, force_reload=False, allow_fill_inline_input=True
            ):
                return forward_email

        # Kiểm tra xem địa chỉ đã được thêm trước đó và đang chờ xác minh (nhập OTP hoặc chờ bấm link 'Gửi lại email') chưa
        driver.implicitly_wait(0)
        needs_code_verify_only = False
        try:
            page_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
            has_pending_resend = any(
                e.is_displayed()
                for e in driver.find_elements(
                    By.XPATH,
                    "//*[@act='resend'] | //*[self::span or self::a or self::button][normalize-space()='Gửi lại email' or normalize-space()='Re-send email']",
                )
            )
            if forward_email.lower() in page_txt and (_find_fwd_verify_code_input(driver) is not None or has_pending_resend):
                needs_code_verify_only = True
        except Exception:
            pass
        finally:
            driver.implicitly_wait(5)

        seen_ids = _snapshot_forwarding_inbox_ids(forward_email)

        if not needs_code_verify_only:
            # Kiểm tra xem hộp thoại nhập email chuyển tiếp mới đã mở sẵn chưa; nếu chưa thì bấm 'Thêm địa chỉ chuyển tiếp'
            fwd_inp = _find_gmail_dialog_input(driver, for_otp=False)
            if not fwd_inp:
                add_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "input[act='add']",
                    timeout=10,
                    condition="clickable",
                )
                if not add_btn:
                    add_btn = wait_for_element(
                        driver,
                        By.XPATH,
                        "//input[@type='button' and (contains(@value, 'forwarding') or contains(@value, 'chuyển tiếp'))]"
                        " | //button[contains(., 'Add a forwarding') or contains(., 'Thêm địa chỉ chuyển tiếp')]",
                        timeout=5,
                        condition="clickable",
                    )
                if not add_btn:
                    # Một số tài khoản Workspace không có nút 'Add a forwarding address' mà hiển thị sẵn ô <input type='text'> inline
                    if _enable_and_save_gmail_forwarding(
                        driver, forward_email, force_reload=False, allow_fill_inline_input=True
                    ):
                        return forward_email
                    logger.error("Không tìm thấy nút 'Thêm địa chỉ chuyển tiếp' / 'Add a forwarding address'")
                    return ""

                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", add_btn)
                time.sleep(0.3)
                if not safe_click(add_btn, delay=1.5):
                    driver.execute_script("arguments[0].click();", add_btn)
                    time.sleep(1.5)

                # Chờ ô nhập email trong hộp thoại Material (role='dialog') hoặc classic (role='alertdialog')
                for _ in range(16):
                    fwd_inp = _find_gmail_dialog_input(driver, for_otp=False)
                    if fwd_inp:
                        break
                    time.sleep(0.5)

            if not fwd_inp:
                logger.error("Không tìm thấy ô nhập địa chỉ email chuyển tiếp trong hộp thoại Gmail")
                return ""

            safe_click(fwd_inp, delay=0.2)
            fwd_inp.send_keys(Keys.CONTROL, "a")
            fwd_inp.send_keys(Keys.BACKSPACE)
            safe_send_keys(fwd_inp, forward_email, delay=0.6)
            logger.info(f"Đã điền địa chỉ chuyển tiếp Gmail: {forward_email}")

            main_handle = driver.current_window_handle
            handles_before = set(driver.window_handles)

            if not _click_gmail_dialog_button(driver, ("tiếp theo", "next"), name_attrs=("next",)):
                fwd_inp.send_keys(Keys.RETURN)
                time.sleep(2.0)
            else:
                logger.info("Đã bấm nút 'Tiếp theo' / 'Next' trong hộp thoại Thêm địa chỉ chuyển tiếp")

            _handle_gmail_forwarding_popups(driver, main_handle, handles_before, gmail_password, twofa_secret)
        else:
            # Địa chỉ đang chờ xác nhận -> bấm 'Gửi lại email' / 'Re-send email' ([act='resend']) nếu có
            logger.info(f"Địa chỉ {forward_email} đang ở trạng thái chờ xác nhận -> Gửi lại mã/link xác nhận...")
            try:
                resend_els = [
                    e for e in driver.find_elements(
                        By.XPATH,
                        "//*[@act='resend'] | //*[self::span or self::a or self::button][normalize-space()='Gửi lại email' or normalize-space()='Re-send email']",
                    )
                    if e.is_displayed()
                ]
                if resend_els:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", resend_els[0])
                    time.sleep(0.3)
                    if not safe_click(resend_els[0], delay=2.0):
                        driver.execute_script("arguments[0].click();", resend_els[0])
                        time.sleep(2.0)
                    logger.info("Đã bấm 'Gửi lại email' / 'Re-send email' xác nhận Gmail Forwarding")
                    _click_gmail_dialog_button(driver, ("ok", "đã hiểu"), name_attrs=("ok",))
            except Exception as e_rs:
                logger.debug(f"Lỗi khi bấm Gửi lại email xác nhận: {e_rs}")

        # Lấy OTP / mã xác nhận & link xác nhận từ <prefix>@nguyenmaucole.io.vn (hoặc @fviainboxes.com)
        conf_code, vf_link = _fetch_gmail_forwarding_confirmation(driver, forward_email, seen_ids, timeout=120)
        if not conf_code and not vf_link:
            logger.error(f"Không nhận được OTP / thư xác nhận chuyển tiếp Gmail tại {forward_email}")
            return ""

        confirmed = False
        # Cách 1: Chờ ô nhập OTP / Mã xác nhận trên Gmail (input[act='verifyText'] hoặc ô trong hàng Xác minh / dialog) và điền OTP
        if conf_code:
            # Nếu vẫn còn hộp thoại 'OK' báo đã gửi mã thì đóng trước khi tìm ô OTP
            _click_gmail_dialog_button(driver, ("ok", "đã hiểu"), name_attrs=("ok",))

            verify_inp = None
            for _ in range(16):
                verify_inp = _find_fwd_verify_code_input(driver)
                if verify_inp:
                    break
                time.sleep(0.5)

            if verify_inp:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", verify_inp)
                time.sleep(0.3)
                safe_click(verify_inp, delay=0.2)
                verify_inp.send_keys(Keys.CONTROL, "a")
                verify_inp.send_keys(Keys.BACKSPACE)
                safe_send_keys(verify_inp, conf_code, delay=0.5)
                logger.info(f"Đã điền OTP xác nhận Gmail Forwarding: {conf_code}")

                if _click_fwd_verify_button(driver, near_input=verify_inp):
                    logger.info("✅ Đã bấm 'Xác minh' / 'Verify' sau khi điền OTP!")
                    confirmed = True
                else:
                    verify_inp.send_keys(Keys.RETURN)
                    time.sleep(2.5)
                    logger.info("✅ Đã gửi OTP xác nhận Gmail Forwarding (Enter)!")
                    confirmed = True

                # Đóng popup thông báo sau khi Verify nếu Gmail hiển thị
                _click_gmail_dialog_button(driver, ("ok", "đã hiểu"), name_attrs=("ok",))

                # Kiểm tra lại trong danh sách đã xác minh sau khi nhập OTP
                time.sleep(2.0)
                if forward_email.lower() not in _get_forwarding_select_emails(driver):
                    _reload_gmail_fwd_settings(driver)
                    if forward_email.lower() not in _get_forwarding_select_emails(driver):
                        confirmed = False

        # Cách 2: Fallback mở link xác nhận vf-... trong tab mới và bấm 'Confirm' / 'Xác nhận' nếu Cách 1 chưa xác minh xong
        if not confirmed and vf_link:
            main_h = driver.current_window_handle
            try:
                driver.switch_to.new_window("tab")
                logger.info(f"Mở link xác nhận Gmail Forwarding trên tab mới: {vf_link[:85]}...")
                _safe_get(driver, vf_link)
                wait_for_page_load(driver, timeout=15)
                time.sleep(2.5)
                confirm_btns = [
                    b for b in driver.find_elements(
                        By.XPATH,
                        "//input[@type='submit' or (@type='button' and (@value='Confirm' or @value='Xác nhận'))]"
                        " | //button[@type='submit' or normalize-space()='Confirm' or normalize-space()='Xác nhận'"
                        " or .//span[normalize-space()='Confirm' or normalize-space()='Xác nhận']]",
                    )
                    if b.is_displayed()
                ]
                if confirm_btns:
                    cbtn = confirm_btns[0]
                    logger.info(
                        f"Tìm thấy nút xác nhận vf- ({cbtn.tag_name}, value={cbtn.get_attribute('value')!r}, "
                        f"text={(cbtn.text or '').strip()!r}, url={driver.current_url[:75]})"
                    )
                    if not safe_click(cbtn, delay=2.0):
                        driver.execute_script("arguments[0].click();", cbtn)
                        time.sleep(2.0)

                    # Nếu vẫn còn nút Confirm/Xác nhận trên form -> thử JS click + form.submit()
                    def _poll_vf_status(max_loops: int = 8) -> Tuple[str, bool]:
                        txt = ""
                        done_ok = False
                        for _ in range(max_loops):
                            try:
                                txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").strip()
                            except Exception:
                                txt = ""
                            low = unicodedata.normalize("NFC", txt).lower()
                            still_form = any(
                                b.is_displayed()
                                for b in driver.find_elements(
                                    By.XPATH,
                                    "//input[@type='submit' and (@value='Confirm' or @value='Xác nhận')]"
                                    " | //button[normalize-space()='Confirm' or normalize-space()='Xác nhận']",
                                )
                            )
                            if any(
                                k in low
                                for k in (
                                    "may now forward",
                                    "confirmation success",
                                    "xác nhận thành công",
                                    "đã xác nhận",
                                    "giờ có thể chuyển tiếp",
                                    "bây giờ có thể chuyển tiếp",
                                    "success!",
                                )
                            ):
                                done_ok = True
                                break
                            if not still_form and txt and "vui lòng xác nhận" not in low and "please confirm" not in low:
                                done_ok = True
                                break
                            time.sleep(0.6)
                        return txt, done_ok

                    vf_done_text, vf_ok = _poll_vf_status(8)
                    if not vf_ok:
                        logger.warning(
                            f"Nút/form vf- chưa chuyển sau click đầu -> Thử submit lại form... "
                            f"(url={driver.current_url[:75]}, text={vf_done_text[:120].replace(chr(10), ' ')!r})"
                        )
                        try:
                            rem = [
                                b for b in driver.find_elements(
                                    By.XPATH,
                                    "//input[@type='submit'] | //button[@type='submit' or normalize-space()='Confirm' or normalize-space()='Xác nhận']",
                                )
                                if b.is_displayed()
                            ]
                            if rem:
                                driver.execute_script(
                                    "if (arguments[0].form) { arguments[0].form.submit(); } else { arguments[0].click(); }",
                                    rem[0],
                                )
                                time.sleep(2.5)
                                vf_done_text, vf_ok = _poll_vf_status(8)
                        except Exception as e_sub:
                            logger.debug(f"Lỗi khi submit lại form vf-: {e_sub}")

                    logger.info(
                        f"Kết quả trang vf- (url={driver.current_url[:75]}, ok={vf_ok}): "
                        f"{vf_done_text[:180].replace(chr(10), ' ')}"
                    )
                    if vf_ok:
                        confirmed = True
                else:
                    # Một số trường hợp trang vf-... tự xác nhận ngay khi mở
                    b_vf = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").strip()
                    logger.info(
                        f"Trang vf- không có nút Confirm (url={driver.current_url[:75]}): "
                        f"{b_vf[:180].replace(chr(10), ' ')}"
                    )
                    b_vf_low = unicodedata.normalize("NFC", b_vf).lower()
                    if any(k in b_vf_low for k in ("may now forward", "confirmation success", "xác nhận thành công", "đã xác nhận", "giờ có thể chuyển tiếp", "bây giờ có thể chuyển tiếp", "success!")):
                        logger.info("✅ Trang link Gmail Forwarding đã xác nhận thành công!")
                        confirmed = True
            except Exception as e:
                logger.warning(f"Lỗi khi mở link xác nhận Gmail Forwarding: {e}")
            finally:
                try:
                    if driver.current_window_handle != main_h and len(driver.window_handles) > 1:
                        driver.close()
                    driver.switch_to.window(main_h)
                except Exception:
                    pass

        if not confirmed:
            logger.error("Không thể xác minh OTP hoặc link Gmail Forwarding")
            return ""

        # Bước cuối: Chọn forward_email trong dropdown (hoặc điền ô inline Workspace), tích 'Chuyển tiếp bản sao...' và bấm 'Lưu thay đổi'
        if _enable_and_save_gmail_forwarding(
            driver, forward_email, force_reload=True, allow_fill_inline_input=True
        ):
            return forward_email

        return ""

    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi khi cài đặt Gmail Forwarding: {e}")
        return ""
