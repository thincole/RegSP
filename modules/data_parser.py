"""
Phân tích chuỗi dữ liệu từ Cột B.
Sử dụng index cố định (đã xác nhận từ dữ liệu mẫu).

Cấu trúc Cột B (phân cách bởi '|'):
  [0] FB UID
  [1] FB Password
  [2] FB 2FA Secret
  [3] FB Cookie
  [4] FB Access Token
  [5] Hotmail Email
  [6] Hotmail Password
  [7] MS Token
  [8] UUID
"""
from typing import Dict, Optional

import config
from utils.logger import get_logger
from utils.error_handler import DataParsingError

logger = get_logger()


def parse_column_b(raw_string: str) -> Dict[str, Optional[str]]:
    """
    Phân tích chuỗi raw từ Cột B bằng index cố định.

    Args:
        raw_string: Chuỗi raw từ Cột B.

    Returns:
        Dict chứa thông tin đã tách.

    Raises:
        DataParsingError: Nếu không thể phân tích.
    """
    if not raw_string or not raw_string.strip():
        raise DataParsingError("Chuỗi Cột B rỗng")

    parts = raw_string.split(config.DATA_SEPARATOR)

    if len(parts) < 3:
        raise DataParsingError(
            f"Cột B không đủ trường (cần ≥ 3, có {len(parts)}): "
            f"{raw_string[:80]}..."
        )

    # --- Cố định: UID / Password / 2FA ---
    fb_uid = parts[0].strip()
    fb_password = parts[1].strip()
    fb_2fa_secret = parts[2].strip() if len(parts) > 2 else ""

    # --- Tự tìm Cookie (chứa "c_user=") ---
    fb_cookie = ""
    for part in parts:
        if "c_user=" in part:
            fb_cookie = part.strip()
            break

    # Nếu chưa có fb_uid chuẩn, tự trích từ c_user=... trong cookie
    if (not fb_uid or not fb_uid.isdigit()) and fb_cookie:
        import re
        m_uid = re.search(r"c_user=(\d+)", fb_cookie)
        if m_uid:
            fb_uid = m_uid.group(1)

    # --- Tự tìm FB Token (bắt đầu bằng "EAA": EAAA, EAAB, EAAD, EAAG...) ---
    fb_token = ""
    for part in parts:
        p = part.strip()
        if p.startswith("EAA"):
            fb_token = p
            break

    # --- Tự tìm Hotmail Email (chứa @outlook hoặc @hotmail) ---
    hotmail_email = ""
    hotmail_password = ""
    for i, part in enumerate(parts):
        p = part.strip()
        if "@outlook" in p.lower() or "@hotmail" in p.lower():
            hotmail_email = p
            # Password hotmail là phần ngay sau email
            if i + 1 < len(parts):
                next_part = parts[i + 1].strip()
                # Chỉ lấy nếu không phải email/cookie/token
                if "@" not in next_part and "c_user" not in next_part and not next_part.startswith("EAA"):
                    hotmail_password = next_part
            break

    # --- Fallback: dùng index cố định nếu không tìm thấy ---
    if not hotmail_email and len(parts) > config.HOTMAIL_EMAIL_INDEX:
        hotmail_email = parts[config.HOTMAIL_EMAIL_INDEX].strip()
    if not hotmail_password and len(parts) > config.HOTMAIL_PASSWORD_INDEX:
        hotmail_password = parts[config.HOTMAIL_PASSWORD_INDEX].strip()

    # Làm sạch hotmail_password nếu trong Sheet bị dính liền với RefreshToken=M.C... hoặc $ClientId=...
    extracted_ms_token = ""
    if hotmail_password:
        import re
        m_rt = re.search(r"RefreshToken=(M\.[A-Za-z0-9._!*-]+)", hotmail_password, flags=re.IGNORECASE)
        if m_rt:
            extracted_ms_token = m_rt.group(1)
        hotmail_password = re.split(
            r"(?:RefreshToken=|\$ClientId=|ClientId=|M\.C\d{3}[_.]|M\.R\d[_.]|0\.U\.MsaArtifacts)",
            hotmail_password,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

    # --- Tự tìm Email khôi phục (chứa @boxnt.com, @fviainboxes.com, @fviadropinbox.com, @fviamail.work, @nguyenmaucole.io.vn, ...) ---
    import re
    recovery_email = ""
    for part in parts:
        p = part.strip()
        if (
            "@" in p
            and "c_user=" not in p
            and p.lower() != hotmail_email.lower()
            and p != fb_password
            and p != hotmail_password
            and not p.startswith("M.")
            and "RefreshToken=" not in p
            and " " not in p
            and re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", p)
        ):
            recovery_email = p
            break

    if not recovery_email:
        m_rec = re.search(
            r"([A-Za-z0-9._%+-]+@(?:boxnt\.com|tempinbox\.vn|tmpmails\.net|fviainboxes\.com|fviadropinbox\.com|fviamail\.work|dropinboxes\.com|nguyenmaucole\.io\.vn))",
            raw_string,
            flags=re.IGNORECASE,
        )
        if m_rec and m_rec.group(1).lower() != hotmail_email.lower():
            recovery_email = m_rec.group(1).strip()

    # --- MS Token, UUID (nếu có) ---
    ms_token = extracted_ms_token
    uuid_val = ""
    for part in parts:
        p = part.strip()
        if p.startswith("M.C") or p.startswith("M.R"):
            ms_token = p
        if len(p) == 36 and p.count("-") == 4:  # UUID format
            uuid_val = p

    logger.debug(
        f"Parsed: FB UID={fb_uid}, "
        f"FB Pass=***{fb_password[-3:] if len(fb_password) > 3 else '***'}, "
        f"Cookie={'CÓ' if fb_cookie else 'KHÔNG'}, "
        f"Email={hotmail_email}, "
        f"HotmailPass={hotmail_password}, "
        f"Recovery={recovery_email or 'Mặc định (@nguyenmaucole.io.vn)'}"
    )

    return {
        "fb_uid": fb_uid,
        "fb_password": fb_password,
        "fb_2fa_secret": fb_2fa_secret,
        "fb_cookie": fb_cookie,
        "fb_token": fb_token,
        "hotmail_email": hotmail_email,
        "hotmail_password": hotmail_password,
        "recovery_email": recovery_email,
        "ms_token": ms_token,
        "uuid": uuid_val,
    }


def validate_parsed_data(data: Dict, steps: list) -> bool:
    """
    Kiểm tra dữ liệu đã parse có đủ cho các bước đã chọn (1-6):
      1. Login Facebook
      2. Login tài khoản Shopee (PH / MY / ID) bằng Facebook
      3. Login Hotmail và đổi password Hotmail
      4. Thêm Email forwarding
      5. Reg Aff Shopee theo thị trường chọn ở bước 2
      6. Đổi Pass Shopee

    Args:
        data: Dict từ parse_column_b().
        steps: Danh sách bước đã chọn (1-6).

    Returns:
        True nếu dữ liệu hợp lệ.
    """
    if 1 in steps:  # Bước 1: Login FB
        has_cookie = bool(data.get("fb_cookie") and "c_user=" in data.get("fb_cookie", ""))
        has_creds = bool(data.get("fb_uid") and data.get("fb_password"))
        if not has_cookie and not has_creds:
            logger.error("Thiếu cả Cookie Facebook lẫn UID/Password Facebook")
            return False

    if 2 in steps or 5 in steps:  # Bước 2 (Login Shopee bằng FB) hoặc Bước 5 (Reg Aff Shopee)
        if not data.get("fb_uid"):
            logger.error("Shopee cần Facebook UID")
            return False

    if 3 in steps:  # Bước 3: Login Hotmail và đổi password Hotmail
        if not data.get("hotmail_email"):
            logger.error("Thiếu Hotmail email")
            return False
        if not data.get("hotmail_password"):
            logger.error("Thiếu Hotmail password")
            return False

    if 4 in steps:  # Bước 4: Thêm Email forwarding
        if not data.get("hotmail_email"):
            logger.error("Thiếu Hotmail email để cài đặt forwarding")
            return False

    if 6 in steps:  # Bước 6: Đổi Pass Shopee cần email để lấy link xác nhận
        if not data.get("hotmail_email"):
            logger.error("Đổi pass Shopee cần Hotmail email để lấy link xác nhận")
            return False

    return True
