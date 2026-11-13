import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
import config
from utils.logger import setup_logger, get_logger
from modules.google_sheets import read_sheet_data, write_cell
from modules.data_parser import (
    parse_column_b,
    validate_parsed_data,
    parse_gmail_column_b,
    validate_gmail_parsed_data,
)
from modules.gemlogin_manager import find_profile_by_name, start_profile, stop_profile, connect_selenium
from modules.facebook_login import login_facebook
from modules.gmail_login import login_gmail, setup_gmail_forwarding
from modules.hotmail_login import (
    login_hotmail,
    setup_hotmail_forwarding,
    replace_hotmail_recovery_email,
    set_preferred_recovery_domain,
)
from modules.change_password import change_hotmail_password
from modules.shopee_login import (
    login_shopee_account_via_facebook,
    login_shopee_account_via_google,
    register_shopee_affiliate,
    change_shopee_password,
)
from utils.error_handler import (
    LoginFailedError,
    BrowserClosedError,
    is_browser_closed_exception,
    check_browser_alive,
)
from gui.app import AutoLoginApp


def _process_single_row(row, gui_config, selected_steps, stop_flag):
    logger = get_logger()
    if stop_flag.is_set():
        return

    sheet_url = gui_config['GOOGLE_SHEET_URL']
    sheet_name = gui_config['SHEET_NAME']
    sa_file = gui_config['GOOGLE_SERVICE_ACCOUNT_FILE']
    new_hotmail_pass = gui_config['NEW_HOTMAIL_PASSWORD']
    new_shopee_pass = gui_config.get('NEW_SHOPEE_PASSWORD', getattr(config, 'NEW_SHOPEE_PASSWORD', ''))

    row_index = row['row_index']
    raw_data = row['raw_data']
    profile_name = row['profile_name']

    if not profile_name:
        logger.error(f"Dòng {row_index}: Thiếu tên profile Gemlogin (cột E). Bỏ qua dòng này.")
        return

    logger.info(f"Đang xử lý dòng {row_index} - Profile: {profile_name}")

    try:
        parsed = parse_column_b(raw_data)
        if not validate_parsed_data(parsed, selected_steps):
            logger.error(f"Dòng {row_index} ({profile_name}): Dữ liệu không đủ cho các bước đã chọn. Bỏ qua dòng này.")
            return
    except Exception as e:
        logger.error(f"Dòng {row_index} ({profile_name}): Lỗi parse dữ liệu - {e}")
        return

    if stop_flag.is_set():
        return

    driver = None
    profile_id = None
    try:
        profile_id = find_profile_by_name(profile_name)
        profile_info = start_profile(profile_id)
        driver = connect_selenium(profile_info)
    except Exception as e:
        logger.error(f"Dòng {row_index} ({profile_name}): Lỗi mở profile - {e}")
        if profile_id:
            try:
                stop_profile(profile_id)
            except Exception:
                pass
        return

    try:
        shopee_urls = getattr(config, 'SHOPEE_URLS', {})
        region = gui_config.get('SHOPEE_REGION', 'Shopee.PH')
        if region not in shopee_urls:
            region = 'Shopee.PH'
        shopee_url = shopee_urls.get(region, 'https://shopee.ph/buyer/login')
        rec_target_domain = (gui_config.get('HOTMAIL_RECOVERY_DOMAIN') or 'nguyenmaucole.io.vn').strip().lstrip('@').lower()
        fwd_target_domain = (gui_config.get('HOTMAIL_FORWARDING_DOMAIN') or 'nguyenmaucole.io.vn').strip().lstrip('@').lower()
        set_preferred_recovery_domain(rec_target_domain)

        # Bước 1: Login Facebook
        if 1 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            fb_no_cookie = bool(gui_config.get('FB_NO_COOKIE', False))
            if fb_no_cookie:
                logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 1 - Login Facebook (Chế độ ID + Pass, bỏ qua Cookie)")
            else:
                logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 1 - Login Facebook")
            try:
                fb_ok = login_facebook(
                    driver,
                    parsed['fb_uid'],
                    parsed['fb_password'],
                    parsed.get('fb_2fa_secret', ''),
                    "" if fb_no_cookie else parsed.get('fb_cookie', ''),
                    parsed.get('hotmail_email', ''),
                    parsed.get('hotmail_password', ''),
                    parsed.get('recovery_email', ''),
                    new_hotmail_pass,
                    rec_target_domain,
                    fwd_target_domain,
                )
            except BrowserClosedError:
                raise
            except LoginFailedError as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Login Facebook lỗi - {e}")
                fb_ok = False

            if not fb_ok:
                check_browser_alive(driver)
                logger.error(f"Dòng {row_index} ({profile_name}): Login Facebook thất bại. Đánh dấu 'FB die'.")
                write_cell(sheet_url, sheet_name, row_index, config.COL_STATUS, 'FB die', sa_file)
                return
            logger.info(f"Dòng {row_index} ({profile_name}): Login Facebook thành công.")

        # Bước 2: Login tài khoản Shopee PH hoặc MY hoặc ID bằng Facebook
        if 2 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 2 - Login tài khoản {region} bằng Facebook")
            shopee_err_msg = "reg Shopee lỗi"
            try:
                shopee_login_ok = login_shopee_account_via_facebook(
                    driver,
                    parsed['fb_uid'],
                    parsed['fb_password'],
                    shopee_url,
                    region,
                    parsed.get('hotmail_email', ''),
                    parsed.get('hotmail_password', ''),
                    parsed.get('recovery_email', ''),
                    new_hotmail_pass,
                )
            except BrowserClosedError:
                raise
            except LoginFailedError as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Login tài khoản {region} lỗi - {e}")
                if "Mobile Phone Verification" in str(e) or "Lỗi đăng ký SP Mobile" in str(e):
                    shopee_err_msg = "Lỗi đăng ký SP Mobile"
                shopee_login_ok = False

            if not shopee_login_ok:
                check_browser_alive(driver)
                logger.error(f"Dòng {row_index} ({profile_name}): Login tài khoản {region} thất bại. Đánh dấu '{shopee_err_msg}'.")
                write_cell(sheet_url, sheet_name, row_index, config.COL_STATUS, shopee_err_msg, sa_file)
                return
            logger.info(f"Dòng {row_index} ({profile_name}): Login tài khoản {region} bằng Facebook thành công.")

        # Bước 3: Login Hotmail -> Thay mail khôi phục (nếu chọn) -> Đổi password Hotmail
        hot_ok = False
        recovery_email = parsed.get('recovery_email', '')
        want_change_recovery = bool(gui_config.get('HOTMAIL_CHANGE_RECOVERY', False))
        if (3 in selected_steps or want_change_recovery) and not stop_flag.is_set():
            check_browser_alive(driver)
            logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 3 - Login Hotmail")
            try:
                hot_ok = login_hotmail(
                    driver,
                    parsed['hotmail_email'],
                    parsed['hotmail_password'],
                    recovery_email,
                    new_hotmail_pass,
                    target_domain=rec_target_domain,
                )
            except BrowserClosedError:
                raise
            except Exception as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Login Hotmail lỗi - {e}")
                hot_ok = False

            if hot_ok:
                logger.info(f"Dòng {row_index} ({profile_name}): Login Hotmail thành công.")

                # 3a. Thay mail khôi phục về @<rec_target_domain> TRƯỚC (cùng trang account.live.com/proofs/Manage/additional)
                if want_change_recovery and not stop_flag.is_set():
                    check_browser_alive(driver)
                    logger.info(
                        f"Dòng {row_index} ({profile_name}): Tiến hành kiểm tra / thay mail khôi phục Hotmail về @{rec_target_domain}..."
                    )
                    try:
                        rec_ok = replace_hotmail_recovery_email(
                            driver,
                            parsed['hotmail_email'],
                            recovery_email,
                            parsed.get('hotmail_password', ''),
                            new_hotmail_pass,
                            target_domain=rec_target_domain,
                        )
                    except BrowserClosedError:
                        raise
                    except Exception as e:
                        if is_browser_closed_exception(e):
                            raise BrowserClosedError(str(e)) from e
                        logger.error(f"Dòng {row_index} ({profile_name}): Thay mail khôi phục Hotmail lỗi - {e}")
                        rec_ok = False

                    if rec_ok:
                        email_prefix = parsed['hotmail_email'].split('@')[0].strip().lower()
                        recovery_email = f"{email_prefix}@{rec_target_domain}"
                        logger.info(f"Dòng {row_index} ({profile_name}): Hoàn tất kiểm tra / thay mail khôi phục Hotmail ({recovery_email}).")
                    else:
                        logger.error(f"Dòng {row_index} ({profile_name}): Thay mail khôi phục Hotmail thất bại.")

                # 3b. Đổi mật khẩu Hotmail SAU (bấm trực tiếp Change password ngay trên trang Security/Manage nếu đang mở sẵn)
                if 3 in selected_steps and not stop_flag.is_set():
                    check_browser_alive(driver)
                    logger.info(f"Dòng {row_index} ({profile_name}): Tiến hành đổi mật khẩu Hotmail...")
                    try:
                        pwd_ok = change_hotmail_password(
                            driver, parsed['hotmail_password'], new_hotmail_pass, parsed['hotmail_email'], recovery_email
                        )
                    except BrowserClosedError:
                        raise
                    except Exception as e:
                        if is_browser_closed_exception(e):
                            raise BrowserClosedError(str(e)) from e
                        logger.error(f"Dòng {row_index} ({profile_name}): Đổi mật khẩu Hotmail lỗi - {e}")
                        pwd_ok = False

                    if pwd_ok:
                        logger.info(f"Dòng {row_index} ({profile_name}): Đổi mật khẩu Hotmail thành công.")
                    else:
                        logger.error(f"Dòng {row_index} ({profile_name}): Đổi mật khẩu Hotmail thất bại.")
            else:
                logger.error(f"Dòng {row_index} ({profile_name}): Login Hotmail thất bại.")

        # Bước 4: Thêm Email forwarding (@nguyenmaucole.io.vn hoặc @fviainboxes.com)
        if 4 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            if 3 in selected_steps and not hot_ok:
                logger.warning(f"Dòng {row_index} ({profile_name}): Bỏ qua Bước 4 (Forwarding) vì Login Hotmail thất bại.")
            else:
                logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 4 - Thêm Email forwarding về @{fwd_target_domain}")
                try:
                    fwd_email = setup_hotmail_forwarding(
                        driver,
                        parsed['hotmail_email'],
                        recovery_email,
                        new_hotmail_pass,
                        parsed.get('hotmail_password', ''),
                        target_domain=fwd_target_domain,
                    )
                except BrowserClosedError:
                    raise
                except Exception as e:
                    if is_browser_closed_exception(e):
                        raise BrowserClosedError(str(e)) from e
                    logger.error(f"Dòng {row_index} ({profile_name}): Thêm forwarding mail lỗi - {e}")
                    fwd_email = ""

                if fwd_email:
                    fwd_str = (
                        fwd_email
                        if isinstance(fwd_email, str)
                        else f"{parsed['hotmail_email'].split('@')[0].strip().lower()}@{fwd_target_domain}"
                    )
                    col_h = getattr(config, 'COL_FORWARD_EMAIL', 'H')
                    write_cell(sheet_url, sheet_name, row_index, col_h, fwd_str, sa_file)
                    logger.info(
                        f"Dòng {row_index} ({profile_name}): Thêm forwarding mail thành công ({fwd_str}) -> Đã ghi vào cột {col_h}."
                    )
                else:
                    logger.error(f"Dòng {row_index} ({profile_name}): Thêm forwarding mail thất bại.")

        # Bước 5: Reg aff Shopee theo thị trường chọn ở bước 2
        if 5 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 5 - Reg Aff Shopee ({region})")
            aff_username, aff_id = "", ""
            shopee_err_msg = "reg Shopee lỗi"
            try:
                res = register_shopee_affiliate(
                    driver,
                    parsed['fb_uid'],
                    parsed['fb_password'],
                    region,
                    parsed.get('hotmail_email', ''),
                    fwd_target_domain,
                )
                if isinstance(res, tuple):
                    aff_ok, aff_username, aff_id = res
                else:
                    aff_ok = bool(res)
            except BrowserClosedError:
                raise
            except LoginFailedError as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Reg Aff Shopee {region} lỗi - {e}")
                if "Mobile Phone Verification" in str(e) or "Lỗi đăng ký SP Mobile" in str(e):
                    shopee_err_msg = "Lỗi đăng ký SP Mobile"
                aff_ok = False

            if not aff_ok:
                check_browser_alive(driver)
                logger.error(f"Dòng {row_index} ({profile_name}): Reg Aff Shopee {region} thất bại. Đánh dấu '{shopee_err_msg}'.")
                write_cell(sheet_url, sheet_name, row_index, config.COL_STATUS, shopee_err_msg, sa_file)
                return

            result_cols = getattr(config, 'SHOPEE_RESULT_COLS', {}).get(
                region,
                (getattr(config, 'COL_USERNAME', 'F'), getattr(config, 'COL_AFFILIATE_ID', 'G')),
            )
            col_u, col_a = result_cols
            if aff_username:
                write_cell(sheet_url, sheet_name, row_index, col_u, aff_username, sa_file)
                logger.info(f"Dòng {row_index} ({profile_name}): Đã ghi Username {region} ('{aff_username}') vào cột {col_u}.")
            if aff_id:
                write_cell(sheet_url, sheet_name, row_index, col_a, aff_id, sa_file)
                logger.info(f"Dòng {row_index} ({profile_name}): Đã ghi Affiliate ID {region} ('{aff_id}') vào cột {col_a}.")

            logger.info(f"Dòng {row_index} ({profile_name}): Reg Aff Shopee {region} thành công.")

        # Bước 6: Đổi Pass Shopee
        if 6 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 6 - Đổi Pass Shopee ({region})")
            try:
                sp_pass_ok = change_shopee_password(
                    driver,
                    shopee_url,
                    new_shopee_pass,
                    parsed.get('hotmail_email', ''),
                    fwd_target_domain,
                )
            except BrowserClosedError:
                raise
            except Exception as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Đổi pass Shopee lỗi - {e}")
                sp_pass_ok = False

            if sp_pass_ok:
                logger.info(f"Dòng {row_index} ({profile_name}): Đổi pass Shopee thành công.")
            else:
                logger.error(f"Dòng {row_index} ({profile_name}): Đổi pass Shopee thất bại.")

    except BrowserClosedError:
        logger.warning(
            f"🛑 Dòng {row_index} ({profile_name}): Trình duyệt đã bị đóng bởi người dùng -> Bỏ qua luồng này và mở luồng tiếp theo."
        )
    except Exception as e:
        if is_browser_closed_exception(e):
            logger.warning(
                f"🛑 Dòng {row_index} ({profile_name}): Trình duyệt đã bị đóng bởi người dùng -> Bỏ qua luồng này và mở luồng tiếp theo."
            )
        else:
            logger.error(f"Dòng {row_index} ({profile_name}): Lỗi không xác định trong quá trình chạy - {e}")
    finally:
        if driver:
            try:
                if getattr(driver, "service", None) and getattr(driver.service, "process", None):
                    driver.service.process.kill()
            except Exception:
                pass
        if profile_id:
            logger.info(f"Dòng {row_index} ({profile_name}): Đóng profile.")
            try:
                stop_profile(profile_id)
            except Exception as e:
                logger.error(f"Dòng {row_index} ({profile_name}): Lỗi khi đóng profile - {e}")


def _process_single_row_gmail(row, gui_config, selected_steps, stop_flag):
    """
    Luồng xử lý 1 dòng cho Tab 'Reg Gmail' (4 bước):
      1. Login Gmail - Thêm Forwarding mail về @nguyenmaucole.io.vn (hoặc @fviainboxes.com)
      2. Login Shopee bằng Gmail (PH / MY / ID)
      3. Reg AFF Shopee theo thị trường chọn ở bước 2 (bỏ qua Media Information)
      4. Change Pass Shopee
    """
    logger = get_logger()
    if stop_flag.is_set():
        return

    sheet_url = gui_config['GOOGLE_SHEET_URL']
    sheet_name = gui_config['SHEET_NAME']
    sa_file = gui_config['GOOGLE_SERVICE_ACCOUNT_FILE']
    new_shopee_pass = gui_config.get('NEW_SHOPEE_PASSWORD', getattr(config, 'NEW_SHOPEE_PASSWORD', ''))

    row_index = row['row_index']
    raw_data = row['raw_data']
    profile_name = row['profile_name']

    if not profile_name:
        logger.error(f"Dòng {row_index}: Thiếu tên profile Gemlogin (cột E). Bỏ qua dòng này.")
        return

    logger.info(f"[Reg Gmail] Đang xử lý dòng {row_index} - Profile: {profile_name}")

    try:
        parsed = parse_gmail_column_b(raw_data)
        if not validate_gmail_parsed_data(parsed, selected_steps):
            logger.error(f"Dòng {row_index} ({profile_name}): Dữ liệu Gmail không hợp lệ. Bỏ qua dòng này.")
            return
    except Exception as e:
        logger.error(f"Dòng {row_index} ({profile_name}): Lỗi parse dữ liệu Gmail - {e}")
        return

    if stop_flag.is_set():
        return

    driver = None
    profile_id = None
    try:
        profile_id = find_profile_by_name(profile_name)
        profile_info = start_profile(profile_id)
        driver = connect_selenium(profile_info)
    except Exception as e:
        logger.error(f"Dòng {row_index} ({profile_name}): Lỗi mở profile - {e}")
        if profile_id:
            try:
                stop_profile(profile_id)
            except Exception:
                pass
        return

    try:
        shopee_urls = getattr(config, 'SHOPEE_URLS', {})
        region = gui_config.get('SHOPEE_REGION', 'Shopee.PH')
        if region not in shopee_urls:
            region = 'Shopee.PH'
        shopee_url = shopee_urls.get(region, 'https://shopee.ph/buyer/login')
        fwd_target_domain = (
            gui_config.get('GMAIL_FORWARDING_DOMAIN') or 'nguyenmaucole.io.vn'
        ).strip().lstrip('@').lower()

        gmail_email = parsed['gmail_email']
        gmail_password = parsed.get('gmail_password', '')
        recovery_email = parsed.get('recovery_email', '')
        twofa_secret = parsed.get('gmail_2fa_secret', '')

        # Bước 1: Login Gmail + Thêm Forwarding mail về @nguyenmaucole.io.vn (hoặc @fviainboxes.com)
        if 1 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            logger.info(
                f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 1 - Login Gmail & Thêm Forwarding về @{fwd_target_domain}"
            )
            try:
                gm_ok = login_gmail(driver, gmail_email, gmail_password, recovery_email, twofa_secret)
            except BrowserClosedError:
                raise
            except LoginFailedError as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Login Gmail lỗi - {e}")
                gm_ok = False

            if not gm_ok:
                check_browser_alive(driver)
                logger.error(f"Dòng {row_index} ({profile_name}): Login Gmail thất bại. Đánh dấu 'Gmail die'.")
                write_cell(sheet_url, sheet_name, row_index, config.COL_STATUS, 'Gmail die', sa_file)
                return

            logger.info(f"Dòng {row_index} ({profile_name}): Login Gmail thành công -> Cài đặt Forwarding về @{fwd_target_domain}...")
            try:
                fwd_email = setup_gmail_forwarding(
                    driver,
                    gmail_email,
                    gmail_password,
                    target_domain=fwd_target_domain,
                    recovery_email=recovery_email,
                    twofa_secret=twofa_secret,
                )
            except BrowserClosedError:
                raise
            except Exception as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Cài đặt Gmail Forwarding lỗi - {e}")
                fwd_email = ""

            if fwd_email:
                col_h = getattr(config, 'COL_FORWARD_EMAIL', 'H')
                write_cell(sheet_url, sheet_name, row_index, col_h, fwd_email, sa_file)
                logger.info(
                    f"Dòng {row_index} ({profile_name}): Cài đặt Gmail Forwarding thành công ({fwd_email}) -> Đã ghi vào cột {col_h}."
                )
            else:
                logger.error(f"Dòng {row_index} ({profile_name}): Cài đặt Gmail Forwarding thất bại.")

        # Bước 2: Login Shopee bằng Gmail (PH / MY / ID)
        if 2 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 2 - Login {region} bằng Gmail")
            shopee_err_msg = "reg Shopee lỗi"
            try:
                shopee_login_ok = login_shopee_account_via_google(
                    driver,
                    gmail_email,
                    gmail_password,
                    shopee_url,
                    region,
                    recovery_email,
                    twofa_secret,
                )
            except BrowserClosedError:
                raise
            except LoginFailedError as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Login {region} bằng Gmail lỗi - {e}")
                if "Mobile Phone Verification" in str(e) or "Lỗi đăng ký SP Mobile" in str(e):
                    shopee_err_msg = "Lỗi đăng ký SP Mobile"
                shopee_login_ok = False

            if not shopee_login_ok:
                check_browser_alive(driver)
                logger.error(f"Dòng {row_index} ({profile_name}): Login {region} bằng Gmail thất bại. Đánh dấu '{shopee_err_msg}'.")
                write_cell(sheet_url, sheet_name, row_index, config.COL_STATUS, shopee_err_msg, sa_file)
                return
            logger.info(f"Dòng {row_index} ({profile_name}): Login {region} bằng Gmail thành công.")

        # Bước 3: Reg AFF Shopee theo thị trường chọn ở bước 2 (bỏ qua Media Information)
        if 3 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 3 - Reg AFF Shopee ({region}) [Bỏ qua MXH]")
            aff_username, aff_id = "", ""
            shopee_err_msg = "reg Shopee lỗi"
            try:
                res = register_shopee_affiliate(
                    driver,
                    "",
                    "",
                    region,
                    gmail_email,
                    fwd_target_domain,
                    skip_social_link=True,
                    login_method="google",
                    gmail_password=gmail_password,
                    recovery_email=recovery_email,
                    twofa_secret=twofa_secret,
                )
                if isinstance(res, tuple):
                    aff_ok, aff_username, aff_id = res
                else:
                    aff_ok = bool(res)
            except BrowserClosedError:
                raise
            except LoginFailedError as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Reg AFF Shopee {region} lỗi - {e}")
                if "Mobile Phone Verification" in str(e) or "Lỗi đăng ký SP Mobile" in str(e):
                    shopee_err_msg = "Lỗi đăng ký SP Mobile"
                aff_ok = False

            if not aff_ok:
                check_browser_alive(driver)
                logger.error(f"Dòng {row_index} ({profile_name}): Reg AFF Shopee {region} thất bại. Đánh dấu '{shopee_err_msg}'.")
                write_cell(sheet_url, sheet_name, row_index, config.COL_STATUS, shopee_err_msg, sa_file)
                return

            result_cols = getattr(config, 'SHOPEE_RESULT_COLS', {}).get(
                region,
                (getattr(config, 'COL_USERNAME', 'F'), getattr(config, 'COL_AFFILIATE_ID', 'G')),
            )
            col_u, col_a = result_cols
            if aff_username:
                write_cell(sheet_url, sheet_name, row_index, col_u, aff_username, sa_file)
                logger.info(f"Dòng {row_index} ({profile_name}): Đã ghi Username {region} ('{aff_username}') vào cột {col_u}.")
            if aff_id:
                write_cell(sheet_url, sheet_name, row_index, col_a, aff_id, sa_file)
                logger.info(f"Dòng {row_index} ({profile_name}): Đã ghi Affiliate ID {region} ('{aff_id}') vào cột {col_a}.")

            logger.info(f"Dòng {row_index} ({profile_name}): Reg AFF Shopee {region} thành công.")

        # Bước 4: Change Pass Shopee
        if 4 in selected_steps and not stop_flag.is_set():
            check_browser_alive(driver)
            logger.info(f"Dòng {row_index} ({profile_name}): Bắt đầu Bước 4 - Đổi Pass Shopee ({region})")
            try:
                sp_pass_ok = change_shopee_password(
                    driver,
                    shopee_url,
                    new_shopee_pass,
                    gmail_email,
                    fwd_target_domain,
                )
            except BrowserClosedError:
                raise
            except Exception as e:
                if is_browser_closed_exception(e):
                    raise BrowserClosedError(str(e)) from e
                logger.error(f"Dòng {row_index} ({profile_name}): Đổi pass Shopee lỗi - {e}")
                sp_pass_ok = False

            if sp_pass_ok:
                logger.info(f"Dòng {row_index} ({profile_name}): Đổi pass Shopee thành công.")
            else:
                logger.error(f"Dòng {row_index} ({profile_name}): Đổi pass Shopee thất bại.")

    except BrowserClosedError:
        logger.warning(
            f"🛑 Dòng {row_index} ({profile_name}): Trình duyệt đã bị đóng bởi người dùng -> Bỏ qua luồng này và mở luồng tiếp theo."
        )
    except Exception as e:
        if is_browser_closed_exception(e):
            logger.warning(
                f"🛑 Dòng {row_index} ({profile_name}): Trình duyệt đã bị đóng bởi người dùng -> Bỏ qua luồng này và mở luồng tiếp theo."
            )
        else:
            logger.error(f"Dòng {row_index} ({profile_name}): Lỗi không xác định trong quá trình chạy - {e}")
    finally:
        if driver:
            try:
                if getattr(driver, "service", None) and getattr(driver.service, "process", None):
                    driver.service.process.kill()
            except Exception:
                pass
        if profile_id:
            logger.info(f"Dòng {row_index} ({profile_name}): Đóng profile.")
            try:
                stop_profile(profile_id)
            except Exception as e:
                logger.error(f"Dòng {row_index} ({profile_name}): Lỗi khi đóng profile - {e}")


def process_all_rows(gui_config, selected_steps, stop_flag, progress_callback, log_callback):
    logger = get_logger()
    mode = gui_config.get('MODE', 'clone')
    mode_label = "Reg Gmail" if mode == "gmail" else "Reg Clone"
    max_threads = max(1, int(gui_config.get('MAX_THREADS', getattr(config, 'MAX_THREADS', 4))))
    logger.info(f"Bắt đầu xử lý [{mode_label}] (Số luồng chạy song song: {max_threads})...")
    
    sheet_url = gui_config['GOOGLE_SHEET_URL']
    sheet_name = gui_config['SHEET_NAME']
    start_row = gui_config['START_ROW']
    end_row = gui_config['END_ROW']
    sa_file = gui_config['GOOGLE_SERVICE_ACCOUNT_FILE']
    
    try:
        rows_data = read_sheet_data(
            sheet_url=sheet_url,
            sheet_name=sheet_name,
            start_row=start_row,
            end_row=end_row,
            col_data=config.COL_DATA,
            col_profile=config.COL_PROFILE,
            service_account_file=sa_file,
        )
    except Exception as e:
        logger.error(f"Lỗi đọc Google Sheet: {e}")
        return

    total_rows = len(rows_data)
    if total_rows == 0:
        logger.warning("Không có dữ liệu để xử lý.")
        return

    completed_count = 0
    progress_lock = threading.Lock()

    def _worker(row_item):
        nonlocal completed_count
        if not stop_flag.is_set():
            if mode == "gmail":
                _process_single_row_gmail(row_item, gui_config, selected_steps, stop_flag)
            else:
                _process_single_row(row_item, gui_config, selected_steps, stop_flag)
        with progress_lock:
            completed_count += 1
            progress_callback(completed_count, total_rows)

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = [executor.submit(_worker, row) for row in rows_data]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Lỗi trong luồng xử lý: {e}")

    logger.info("Hoàn thành danh sách!")

if __name__ == "__main__":
    setup_logger(getattr(config, 'LOG_DIR', 'logs'), getattr(config, 'LOG_LEVEL', 'INFO'))
    logger = get_logger()

    root = tk.Tk()
    app = AutoLoginApp(root)

    # Inject hàm xử lý vào GUI (tránh circular import)
    app.process_func = process_all_rows

    # Thêm loguru sink để ghi log vào GUI
    def gui_log_sink(message):
        record = message.record
        text = f"{record['time']:HH:mm:ss} | {record['level'].name: <8} | {record['message']}"
        app.add_log(text)

    logger.add(gui_log_sink, level="INFO", format="{message}")
    logger.info(f"Phiên bản phần mềm: {config.get_app_version()}")

    root.mainloop()
