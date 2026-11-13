"""
Đăng nhập Gmail và cài đặt Email Forwarding về @nguyenmaucole.io.vn (hoặc @fviainboxes.com) cho Tab 'Reg Gmail'.
"""
import html
import json
import re
import time
import urllib.request
from typing import Optional, Tuple

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


def _handle_google_post_login_prompts(
    driver: WebDriver,
    gmail_email: str = "",
    recovery_email: str = "",
    twofa_secret: str = "",
) -> None:
    """
    Xử lý các bước xác minh 2FA / Email khôi phục / Passkey / Speedbump sau khi nhập mật khẩu Google.
    """
    for _ in range(8):
        check_browser_alive(driver)
        time.sleep(2)
        cur_u = (driver.current_url or "").lower()
        if "mail.google.com/mail" in cur_u and "accounts.google.com" not in cur_u:
            return

        driver.implicitly_wait(0)
        try:
            body_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "")
            body_low = body_txt.lower()

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

            # 5. Nếu gặp Captcha hoặc xác minh thủ công của Google -> chờ người dùng hoặc GemCaptcha giải
            if "captcha" in body_low or "verify it's you" in body_low or "xác minh danh tính" in body_low:
                logger.info("⏳ Phát hiện bước xác minh bổ sung của Google, đang chờ hoàn tất...")
                for _ in range(20):
                    time.sleep(3)
                    check_browser_alive(driver)
                    u_now = (driver.current_url or "").lower()
                    if "mail.google.com/mail" in u_now or "myaccount.google.com" in u_now:
                        break
                continue
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


def login_gmail(
    driver: WebDriver,
    gmail_email: str,
    gmail_password: str,
    recovery_email: str = "",
    twofa_secret: str = "",
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

        _handle_google_post_login_prompts(driver, gmail_clean, recovery_email, twofa_secret)

        if not _is_gmail_inbox_loaded(driver):
            _safe_get(driver, GMAIL_INBOX_URL)
            wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
            time.sleep(3)
            _handle_google_post_login_prompts(driver, gmail_clean, recovery_email, twofa_secret)

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
    """Trích xuất mã xác nhận (Confirmation code) và link xác nhận (vf-...) từ thư của Google Forwarding."""
    code = ""
    vf_link = ""

    # 1. Tìm mã trong tiêu đề dạng (#123456789)
    m_subj = re.search(r"\(#?(\d{7,11})\)", subject or "")
    if m_subj:
        code = m_subj.group(1)

    # 2. Tìm link xác nhận https://mail-settings.google.com/mail/vf-... hoặc isolated.mail.google.com/mail/vf-...
    raw_links = re.findall(r'href=["\'](https?://[^"\']+)["\']', body_html or "")
    if not raw_links:
        raw_links = re.findall(r"(https?://[^\s<>\"']+)", body_html or "")
    for rl in raw_links:
        clean_l = html.unescape(rl).strip().rstrip(".,;)")
        if "/mail/vf-" in clean_l or "mail-settings.google.com/mail/vf" in clean_l or "isolated.mail.google.com/mail/vf" in clean_l:
            vf_link = clean_l
            break

    # 3. Tìm mã Confirmation code trong nội dung thư nếu chưa có từ subject
    if not code:
        text_clean = re.sub(r"<[^>]+>", " ", body_html or "")
        m_body = re.search(
            r"(?:Confirmation code|Mã xác nhận|mã xác minh)[^0-9]*(\d{7,11})\b",
            text_clean,
            flags=re.IGNORECASE,
        )
        if m_body:
            code = m_body.group(1)

    return code, vf_link


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
    logger.info(f"Đang chờ thư xác nhận Gmail Forwarding gửi về {addr} (tối đa {timeout}s)...")

    start_time = time.time()
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
                        body_html = str(m.get("body_html") or "")
                        combined_low = (subj + " " + sender + " " + body_html[:300]).lower()
                        if "forwarding" in combined_low or "chuyển tiếp" in combined_low or "google" in combined_low:
                            code, vf_link = _extract_gmail_fwd_code_and_link(subj, body_html)
                            if code or vf_link:
                                logger.info(f"✅ Đã nhận thư xác nhận Gmail Forwarding (code={code or 'N/A'})")
                                return code, vf_link
                        seen_ids.add(f"nmc:{mid}")
            except Exception as e:
                logger.debug(f"Lỗi đọc API nguyenmaucole.io.vn: {e}")
        else:
            try:
                r = requests.get(
                    FVIA_MESSAGES_API,
                    params={"username": prefix, "domain": dom},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10,
                )
                if r.status_code == 200:
                    for msg in (r.json().get("result") or []):
                        mid = str(msg.get("id", ""))
                        if not mid or f"fvia:{mid}" in seen_ids:
                            continue
                        subj = str(msg.get("subject") or "")
                        sender = str(msg.get("from") or msg.get("sender") or "")
                        if any(k in (subj + " " + sender).lower() for k in ("forwarding", "chuyển tiếp", "google", "gmail")):
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
                                logger.info(f"✅ Đã nhận thư xác nhận Gmail Forwarding trên {dom} (code={code or 'N/A'})")
                                return code, vf_link
                        seen_ids.add(f"fvia:{mid}")
            except Exception as e:
                logger.debug(f"Lỗi đọc API fviainboxes.com: {e}")

        elapsed = int(time.time() - start_time)
        if elapsed > 0 and elapsed % 9 == 0:
            logger.info(f"⏳ Đang chờ email xác nhận Gmail Forwarding tại {addr}... ({elapsed}s/{timeout}s)")
        time.sleep(3)

    return "", ""


def _handle_gmail_forwarding_popups(
    driver: WebDriver,
    main_handle: str,
    handles_before: set,
    gmail_password: str = "",
    twofa_secret: str = "",
) -> None:
    """
    Xử lý cửa sổ popup xác thực lại mật khẩu Google ('Verify it's you') và cửa sổ 'Confirm forwarding address' -> 'Proceed'.
    """
    for _ in range(14):
        time.sleep(1.0)
        check_browser_alive(driver)
        new_handles = [h for h in driver.window_handles if h not in handles_before and h != main_handle]

        if not new_handles:
            # Kiểm tra xem trên tab chính đã hiện hộp thoại 'OK' báo đã gửi mã xác nhận chưa
            driver.implicitly_wait(0)
            try:
                if main_handle in driver.window_handles:
                    driver.switch_to.window(main_handle)
                    ok_btns = [
                        b for b in driver.find_elements(
                            By.CSS_SELECTOR,
                            "div[role='alertdialog'] button[name='ok'], div.Kj-JD button[name='ok']",
                        )
                        if b.is_displayed()
                    ]
                    if ok_btns:
                        safe_click(ok_btns[0], delay=1.5)
                        logger.info("Đã bấm 'OK' trên hộp thoại thông báo gửi mã Gmail Forwarding")
                        return
            except Exception:
                pass
            finally:
                driver.implicitly_wait(5)
            continue

        for pop_h in list(new_handles):
            if pop_h not in driver.window_handles:
                continue
            try:
                driver.switch_to.window(pop_h)
                pop_url = (driver.current_url or "").lower()
                driver.implicitly_wait(0)
                body_pop = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()

                # 1. Popup xác thực lại mật khẩu Google ('Verify it's you' trên accounts.google.com)
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
                            safe_click(next_btns[0], delay=2.5)
                        else:
                            pwd_els[0].send_keys(Keys.RETURN)
                            time.sleep(2.5)
                        continue

                    totp_els = [
                        e for e in driver.find_elements(By.CSS_SELECTOR, "input#totpPin, input[name='totpPin']")
                        if e.is_displayed()
                    ]
                    if totp_els and twofa_secret:
                        code = generate_2fa_code(twofa_secret)
                        safe_send_keys(totp_els[0], code, delay=0.8)
                        totp_els[0].send_keys(Keys.RETURN)
                        time.sleep(2.5)
                        continue

                    cont_btns = [
                        b for b in driver.find_elements(
                            By.XPATH,
                            "//button[.//span[normalize-space()='Continue' or normalize-space()='Tiếp tục'] or normalize-space()='Continue' or normalize-space()='Tiếp tục']",
                        )
                        if b.is_displayed()
                    ]
                    if cont_btns:
                        safe_click(cont_btns[0], delay=2.0)
                        continue

                # 2. Popup 'Confirm forwarding address' có nút 'Proceed' / 'Tiếp tục'
                proceed_btns = [
                    b for b in driver.find_elements(
                        By.XPATH,
                        "//input[@type='submit' and (@value='Proceed' or @value='Tiếp tục' or contains(@value, 'Proceed'))]"
                        " | //button[@type='submit' or normalize-space()='Proceed' or normalize-space()='Tiếp tục']",
                    )
                    if b.is_displayed()
                ]
                if not proceed_btns and ("forwarding mail to" in body_pop or "chuyển tiếp thư" in body_pop):
                    proceed_btns = [
                        b for b in driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']")
                        if b.is_displayed()
                    ]

                if proceed_btns:
                    logger.info("Bấm 'Proceed' trong popup xác nhận địa chỉ Gmail Forwarding...")
                    if not safe_click(proceed_btns[0], delay=2.0):
                        driver.execute_script("arguments[0].click();", proceed_btns[0])
                        time.sleep(2.0)
                    break
            except Exception as e:
                logger.debug(f"Lỗi xử lý popup Gmail Forwarding: {e}")
            finally:
                try:
                    driver.implicitly_wait(5)
                except Exception:
                    pass

    # Chuyển về tab chính và bấm nút 'OK' trên thông báo 'A confirmation code has been sent...' (nếu có)
    if main_handle in driver.window_handles:
        driver.switch_to.window(main_handle)
        time.sleep(1.5)
        driver.implicitly_wait(0)
        try:
            ok_btns = [
                b for b in driver.find_elements(
                    By.CSS_SELECTOR,
                    "div[role='alertdialog'] button[name='ok'], div.Kj-JD button[name='ok']",
                )
                if b.is_displayed()
            ]
            if not ok_btns:
                ok_btns = [
                    b for b in driver.find_elements(
                        By.XPATH,
                        "//div[@role='alertdialog']//button[normalize-space()='OK']",
                    )
                    if b.is_displayed()
                ]
            if ok_btns:
                if not safe_click(ok_btns[0], delay=1.5):
                    driver.execute_script("arguments[0].click();", ok_btns[0])
                    time.sleep(1.5)
                logger.info("Đã đóng hộp thoại 'OK' sau khi gửi mã xác nhận Gmail Forwarding")
        except Exception:
            pass
        finally:
            driver.implicitly_wait(5)


def _enable_and_save_gmail_forwarding(driver: WebDriver, forward_email: str) -> bool:
    """
    Trên trang #settings/fwdandpop:
    1. Tích chọn radio 'Forward a copy of incoming mail to ...' (input[name='sx_em'][value='1']).
    2. Chọn đúng forward_email trong dropdown (nếu có nhiều địa chỉ).
    3. Bấm 'Save Changes' ở cuối trang.
    """
    _safe_get(driver, GMAIL_FWD_SETTINGS_URL)
    wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
    time.sleep(3.5)

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

    # Chọn đúng địa chỉ forward_email trong thẻ <select> cạnh radio (nếu có)
    try:
        row_tr = enable_radio.find_element(By.XPATH, "./ancestor::tr[1]")
        selects = row_tr.find_elements(By.TAG_NAME, "select")
        for sel_el in selects:
            opts = sel_el.find_elements(By.TAG_NAME, "option")
            for opt in opts:
                if forward_email.lower() in ((opt.text or "") + " " + (opt.get_attribute("value") or "")).lower():
                    Select(sel_el).select_by_visible_text(opt.text)
                    break
    except Exception:
        pass

    was_selected = bool(enable_radio.is_selected() or enable_radio.get_attribute("checked"))
    if not was_selected:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", enable_radio)
        time.sleep(0.3)
        if not safe_click(enable_radio, delay=1.0):
            driver.execute_script("arguments[0].click();", enable_radio)
            time.sleep(1.0)
        logger.info(f"Đã chọn 'Forward a copy of incoming mail to {forward_email}'")
    else:
        logger.info(f"Tùy chọn 'Forward a copy of incoming mail to {forward_email}' đã được bật sẵn!")

    # Tìm nút 'Save Changes' / 'Lưu thay đổi' ở cuối trang cài đặt Gmail
    save_btns = [
        b for b in driver.find_elements(
            By.XPATH,
            "//button[@guidedhelpid='save_changes_button' or normalize-space()='Save Changes' or normalize-space()='Lưu thay đổi']",
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
            logger.info("✅ Đã bấm 'Save Changes' lưu cài đặt Gmail Forwarding thành công!")
        else:
            logger.info("ℹ️ Nút 'Save Changes' đang mờ (cài đặt Forwarding đã được lưu sẵn từ trước)")
    return True


def setup_gmail_forwarding(
    driver: WebDriver,
    gmail_email: str,
    gmail_password: str = "",
    target_domain: str = TEMPMAIL_DOMAIN,
    recovery_email: str = "",
    twofa_secret: str = "",
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
            login_gmail(driver, gmail_email, gmail_password, recovery_email, twofa_secret)

        _safe_get(driver, GMAIL_FWD_SETTINGS_URL)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        time.sleep(4)
        _dismiss_gmail_popups(driver)

        # Kiểm tra xem forward_email đã được thêm & xác minh từ trước chưa
        driver.implicitly_wait(0)
        already_verified = False
        needs_code_verify_only = False
        try:
            page_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
            enable_radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio'][name='sx_em'][value='1']")
            verify_inps = [
                el for el in driver.find_elements(By.CSS_SELECTOR, "input[act='verifyText']")
                if el.is_displayed()
            ]
            if forward_email.lower() in page_txt and enable_radios and not verify_inps:
                already_verified = True
            elif forward_email.lower() in page_txt and verify_inps:
                needs_code_verify_only = True
        except Exception:
            pass
        finally:
            driver.implicitly_wait(5)

        if already_verified:
            logger.info(f"ℹ️ Địa chỉ {forward_email} đã được xác minh trên Gmail -> Kiểm tra bật chuyển tiếp...")
            if _enable_and_save_gmail_forwarding(driver, forward_email):
                return forward_email

        seen_ids = _snapshot_forwarding_inbox_ids(forward_email)

        if not needs_code_verify_only:
            # Bấm nút 'Add a forwarding address' / 'Thêm địa chỉ chuyển tiếp'
            add_btn = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "input[act='add']",
                timeout=12,
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
                logger.error("Không tìm thấy nút 'Add a forwarding address' trên cài đặt Gmail")
                return ""

            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", add_btn)
            time.sleep(0.3)
            if not safe_click(add_btn, delay=1.5):
                driver.execute_script("arguments[0].click();", add_btn)
                time.sleep(1.5)

            fwd_inp = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "div[role='alertdialog'] input[type='text'], div.Kj-JD input[type='text']",
                timeout=8,
                condition="visible",
            )
            if not fwd_inp:
                logger.error("Không tìm thấy ô nhập địa chỉ email chuyển tiếp trong hộp thoại Gmail")
                return ""

            safe_click(fwd_inp, delay=0.2)
            safe_send_keys(fwd_inp, forward_email, delay=0.8)
            logger.info(f"Đã điền địa chỉ chuyển tiếp Gmail: {forward_email}")

            main_handle = driver.current_window_handle
            handles_before = set(driver.window_handles)

            next_modal_btn = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "div[role='alertdialog'] button[name='next'], div.Kj-JD button[name='next']",
                timeout=5,
                condition="clickable",
            )
            if not next_modal_btn:
                next_modal_btn = wait_for_element(
                    driver,
                    By.XPATH,
                    "//div[@role='alertdialog']//button[normalize-space()='Next' or normalize-space()='Tiếp theo']",
                    timeout=3,
                    condition="clickable",
                )
            if next_modal_btn:
                if not safe_click(next_modal_btn, delay=2.0):
                    driver.execute_script("arguments[0].click();", next_modal_btn)
                    time.sleep(2.0)
            else:
                fwd_inp.send_keys(Keys.RETURN)
                time.sleep(2.0)

            _handle_gmail_forwarding_popups(driver, main_handle, handles_before, gmail_password, twofa_secret)
        else:
            # Nếu địa chỉ đã thêm nhưng chưa xác nhận -> bấm 'Re-send email' (span[act='resend']) nếu có
            try:
                resend_els = [
                    e for e in driver.find_elements(By.CSS_SELECTOR, "[act='resend']")
                    if e.is_displayed()
                ]
                if resend_els:
                    safe_click(resend_els[0], delay=2.0)
                    ok_btns = [
                        b for b in driver.find_elements(By.CSS_SELECTOR, "div[role='alertdialog'] button[name='ok']")
                        if b.is_displayed()
                    ]
                    if ok_btns:
                        safe_click(ok_btns[0], delay=1.0)
            except Exception:
                pass

        # Lấy mã xác nhận / link xác nhận từ hòm thư forward_email
        conf_code, vf_link = _fetch_gmail_forwarding_confirmation(driver, forward_email, seen_ids, timeout=120)
        if not conf_code and not vf_link:
            logger.error(f"Không nhận được email xác nhận chuyển tiếp Gmail tại {forward_email}")
            return ""

        confirmed = False
        # Cách 1: Nhập trực tiếp mã xác nhận vào ô input[act='verifyText'] trên trang cài đặt Gmail
        if conf_code:
            verify_inp = wait_for_element(
                driver,
                By.CSS_SELECTOR,
                "input[act='verifyText']",
                timeout=6,
                condition="visible",
            )
            if verify_inp:
                safe_click(verify_inp, delay=0.2)
                safe_send_keys(verify_inp, conf_code, delay=0.6)
                verify_btn = wait_for_element(
                    driver,
                    By.CSS_SELECTOR,
                    "input[act='verify']",
                    timeout=4,
                    condition="clickable",
                )
                if verify_btn:
                    if not safe_click(verify_btn, delay=2.5):
                        driver.execute_script("arguments[0].click();", verify_btn)
                        time.sleep(2.5)
                    logger.info(f"✅ Đã điền mã {conf_code} và bấm 'Verify' xác nhận Gmail Forwarding!")
                    confirmed = True

        # Cách 2: Mở link xác nhận vf-... trong tab mới và bấm 'Confirm' / 'Xác nhận'
        if not confirmed and vf_link:
            main_h = driver.current_window_handle
            try:
                driver.switch_to.new_window("tab")
                logger.info(f"Mở link xác nhận Gmail Forwarding trên tab mới: {vf_link[:75]}...")
                _safe_get(driver, vf_link)
                wait_for_page_load(driver, timeout=15)
                time.sleep(2)
                confirm_btns = [
                    b for b in driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']")
                    if b.is_displayed()
                ]
                if confirm_btns:
                    if not safe_click(confirm_btns[0], delay=2.5):
                        driver.execute_script("arguments[0].click();", confirm_btns[0])
                        time.sleep(2.5)
                logger.info("✅ Đã bấm 'Confirm' trên trang xác nhận link Gmail Forwarding!")
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
            logger.error("Không thể xác nhận mã hoặc link Gmail Forwarding")
            return ""

        # Bước cuối: Bật radio 'Forward a copy of incoming mail to ...' và bấm 'Save Changes'
        if _enable_and_save_gmail_forwarding(driver, forward_email):
            return forward_email

        return ""

    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi khi cài đặt Gmail Forwarding: {e}")
        return ""
