"""
Tạo mã TOTP (Time-based One-Time Password) cho xác thực 2FA.
"""
import time
import pyotp

from utils.logger import get_logger

logger = get_logger()


def generate_2fa_code(secret: str) -> str:
    """
    Tạo mã TOTP 6 số từ 2FA secret key.

    Args:
        secret: Secret key base32.

    Returns:
        Mã TOTP 6 số.
    """
    if not secret or not secret.strip():
        raise ValueError("2FA secret key rỗng")

    clean_secret = secret.strip().replace(" ", "").upper()

    try:
        totp = pyotp.TOTP(clean_secret)
        code = totp.now()
        logger.debug(f"Generated 2FA code: {code}")
        return code
    except Exception as e:
        logger.error(f"Lỗi tạo mã 2FA: {e}")
        raise ValueError(f"Secret key không hợp lệ: {e}")


def get_remaining_seconds(secret: str) -> int:
    """
    Lấy số giây còn lại trước khi mã TOTP hiện tại hết hạn.

    Returns:
        Số giây còn lại (0-30).
    """
    return 30 - (int(time.time()) % 30)
