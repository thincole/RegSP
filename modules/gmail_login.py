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
                check_btn = wait_for_element(
                    driver, By.XPATH, "//button[contains(text(), 'Kiểm tra Thư')]", timeout=5, condition="clickable"
                )
                if check_btn:
                    safe_click(check_btn, delay=3.0)
                mail_items = [
                    el for el in driver.find_elements(By.CSS_SELECTOR, ".mail-item")
                    if el.is_displayed()
                ]
                for item in mail_items[:5]:
                    subj_txt = item.text or ""
                    safe_click(item, delay=1.2)
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
    """Lấy danh sách các địa chỉ email ĐÃ XÁC MINH có trong dropdown chọn email chuyển tiếp."""
    emails = []
    driver.implicitly_wait(0)
    try:
        radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio'][name='sx_em'][value='1']")
        search_selects = []
        if radios:
            try:
                row_tr = radios[0].find_element(By.XPATH, "./ancestor::tr[1]")
                search_selects = row_tr.find_elements(By.TAG_NAME, "select")
            except Exception:
                pass
        if not search_selects:
            search_selects = driver.find_elements(By.TAG_NAME, "select")

        for sel_el in search_selects:
            for opt in sel_el.find_elements(By.TAG_NAME, "option"):
                txt = (opt.text or "").strip().lower()
                val = (opt.get_attribute("value") or "").strip().lower()
                for cand in (txt, val):
                    if "@" in cand and cand not in emails:
                        emails.append(cand)
    except Exception:
        pass
    finally:
        try:
            driver.implicitly_wait(5)
        except Exception:
            pass
    return emails


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

                    # Nếu hiện màn hình Passkey -> bấm 'Thử cách khác' / 'Try another way' để chuyển về mật khẩu
                    if ("passkey" in body_pop or "khóa truy cập" in body_pop) and gmail_password:
                        try_btns = [
                            b for b in driver.find_elements(
                                By.XPATH,
                                "//button[contains(., 'Try another way') or contains(., 'Thử cách khác')]",
                            )
                            if b.is_displayed()
                        ]
                        if try_btns:
                            safe_click(try_btns[0], delay=2.0)
                            continue
                        pwd_opt = [
                            el for el in driver.find_elements(
                                By.XPATH,
                                "//li[.//*[contains(., 'Enter your password') or contains(., 'Nhập mật khẩu')]]",
                            )
                            if el.is_displayed()
                        ]
                        if pwd_opt:
                            safe_click(pwd_opt[0], delay=2.0)
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
    if main_handle in driver.window_handles:
        driver.switch_to.window(main_handle)
        time.sleep(1.5)
        if _click_gmail_dialog_button(driver, ("ok", "đã hiểu", "got it"), name_attrs=("ok",)):
            logger.info("Đã đóng hộp thoại 'OK' sau khi gửi OTP xác nhận Gmail Forwarding")


def _enable_and_save_gmail_forwarding(driver: WebDriver, forward_email: str, force_reload: bool = True) -> bool:
    """
    Trên trang #settings/fwdandpop:
    1. Kiểm tra forward_email đã có trong danh sách địa chỉ đã xác minh chưa.
    2. Chọn đúng forward_email trong dropdown.
    3. Tích chọn radio 'Chuyển tiếp bản sao của thư đến tới ...' / 'Forward a copy of incoming mail to ...' (input[name='sx_em'][value='1']).
    4. Bấm 'Lưu thay đổi' / 'Save Changes' ở cuối trang.
    """
    if force_reload or "#settings/fwdandpop" not in (driver.current_url or "").lower():
        _safe_get(driver, GMAIL_FWD_SETTINGS_URL)
        wait_for_page_load(driver, timeout=config.PAGE_LOAD_TIMEOUT)
        _wait_for_fwd_settings_ready(driver, timeout=15)
        time.sleep(1.0)
    else:
        time.sleep(2.0)

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

    # Chọn đúng địa chỉ forward_email trong thẻ <select> cạnh radio
    selected_target_email = False
    try:
        row_tr = enable_radio.find_element(By.XPATH, "./ancestor::tr[1]")
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
    except Exception:
        pass

    if not selected_target_email:
        logger.warning(f"Chưa thấy '{forward_email}' trong danh sách địa chỉ chuyển tiếp đã xác minh trên Gmail")
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
        _wait_for_fwd_settings_ready(driver, timeout=15)
        time.sleep(1.5)
        _dismiss_gmail_popups(driver)

        # Kiểm tra xem forward_email đã có trong dropdown địa chỉ đã xác minh chưa
        verified_emails = _get_forwarding_select_emails(driver)
        if forward_email.lower() in verified_emails:
            logger.info(f"ℹ️ Địa chỉ {forward_email} đã có trong danh sách đã xác minh -> Kiểm tra bật chuyển tiếp & lưu...")
            if _enable_and_save_gmail_forwarding(driver, forward_email, force_reload=False):
                return forward_email

        # Kiểm tra xem địa chỉ đã được thêm trước đó và đang chờ nhập OTP (verifyText) chưa
        driver.implicitly_wait(0)
        needs_code_verify_only = False
        try:
            page_txt = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
            if forward_email.lower() in page_txt and _find_fwd_verify_code_input(driver) is not None:
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
            logger.info(f"Địa chỉ {forward_email} đang ở trạng thái chờ nhập OTP -> Gửi lại mã xác nhận...")
            try:
                resend_els = [
                    e for e in driver.find_elements(
                        By.XPATH,
                        "//*[@act='resend'] | //span[contains(., 'Re-send') or contains(., 'Gửi lại')]",
                    )
                    if e.is_displayed()
                ]
                if resend_els:
                    if not safe_click(resend_els[0], delay=2.0):
                        driver.execute_script("arguments[0].click();", resend_els[0])
                        time.sleep(2.0)
                    _click_gmail_dialog_button(driver, ("ok", "đã hiểu"), name_attrs=("ok",))
            except Exception:
                pass

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
                    _safe_get(driver, GMAIL_FWD_SETTINGS_URL)
                    _wait_for_fwd_settings_ready(driver, timeout=12)
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
                    if not safe_click(confirm_btns[0], delay=3.0):
                        driver.execute_script("arguments[0].click();", confirm_btns[0])
                        time.sleep(3.0)
                    logger.info("✅ Đã bấm 'Confirm' / 'Xác nhận' trên trang xác nhận link Gmail Forwarding!")
                    confirmed = True
                else:
                    # Một số trường hợp trang vf-... tự xác nhận ngay khi mở
                    b_vf = (driver.execute_script("return document.body ? document.body.innerText : '';") or "").lower()
                    if any(k in b_vf for k in ("may now forward", "confirmation success", "xác nhận thành công", "đã xác nhận", "giờ có thể chuyển tiếp")):
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

        # Bước cuối: Chọn forward_email trong dropdown, tích 'Chuyển tiếp bản sao...' và bấm 'Lưu thay đổi'
        if _enable_and_save_gmail_forwarding(driver, forward_email, force_reload=True):
            return forward_email

        return ""

    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Lỗi khi cài đặt Gmail Forwarding: {e}")
        return ""
