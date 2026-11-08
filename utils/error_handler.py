"""
Module xử lý lỗi và retry logic.
"""
import time
import functools
from typing import Callable, Any, Optional

from utils.logger import get_logger

logger = get_logger()


class AutoLoginError(Exception):
    """Base exception."""
    pass

class ProfileNotFoundError(AutoLoginError):
    """Profile Gemlogin không tìm thấy."""
    pass

class ProfileStartError(AutoLoginError):
    """Không thể khởi động profile."""
    pass

class LoginFailedError(AutoLoginError):
    """Đăng nhập thất bại."""
    pass

class DataParsingError(AutoLoginError):
    """Lỗi phân tích dữ liệu."""
    pass

class SheetReadError(AutoLoginError):
    """Lỗi đọc Google Sheets."""
    pass

class PasswordChangeError(AutoLoginError):
    """Lỗi đổi mật khẩu."""
    pass


class BrowserClosedError(AutoLoginError):
    """Trình duyệt đã bị người dùng đóng thủ công."""
    pass


_CLOSED_KEYWORDS = (
    "no such window",
    "target window already closed",
    "web view not found",
    "invalid session id",
    "session deleted",
    "disconnected",
    "not connected to devtools",
    "chrome not reachable",
    "failed to establish a new connection",
    "connection refused",
    "forcibly closed by the remote host",
    "max retries exceeded",
    "trình duyệt đã bị đóng",
)


def is_browser_closed_exception(exc: BaseException) -> bool:
    """Kiểm tra xem exception có phải do trình duyệt đã bị tắt/đóng hay không."""
    if isinstance(exc, BrowserClosedError):
        return True
    msg = str(exc or "").lower()
    return any(k in msg for k in _CLOSED_KEYWORDS)


def check_browser_alive(driver: Any) -> None:
    """
    Kiểm tra trình duyệt còn mở hay không.
    Nếu người dùng đã tắt cửa sổ trình duyệt, ném BrowserClosedError ngay lập tức.
    """
    if driver is None:
        raise BrowserClosedError("Trình duyệt đã bị đóng")
    try:
        handles = driver.window_handles
        if not handles:
            raise BrowserClosedError("Trình duyệt đã bị đóng (không còn cửa sổ nào)")
    except BrowserClosedError:
        raise
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e


def retry(
    max_retries: int = 2,
    delay: float = 3.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,),
) -> Callable:
    """
    Decorator retry với exponential backoff.

    Args:
        max_retries: Số lần retry tối đa.
        delay: Thời gian chờ ban đầu (giây).
        backoff_factor: Hệ số tăng thời gian chờ.
        exceptions: Tuple các exception cần retry.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"[Retry {attempt + 1}/{max_retries}] "
                            f"{func.__name__} failed: {e}. "
                            f"Retrying in {current_delay:.1f}s..."
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff_factor
                    else:
                        logger.error(
                            f"[Failed] {func.__name__} failed after "
                            f"{max_retries} retries: {e}"
                        )
            raise last_exception

        return wrapper
    return decorator


def log_result(
    row_index: int,
    profile_name: str,
    fb_status: str,
    hotmail_status: str,
    change_pwd_status: str,
    shopee_status: str,
    error_msg: Optional[str] = None,
) -> dict:
    """
    Tạo bản ghi kết quả cho mỗi profile.

    Returns:
        Dict chứa thông tin kết quả.
    """
    result = {
        "row": row_index,
        "profile": profile_name,
        "facebook": fb_status,
        "hotmail": hotmail_status,
        "change_password": change_pwd_status,
        "shopee": shopee_status,
        "error": error_msg,
    }

    all_success = all(
        s == "success" for s in [fb_status, hotmail_status, change_pwd_status, shopee_status]
    )
    icon = "✅" if all_success else "❌"
    logger.info(
        f"{icon} Row {row_index} | Profile: {profile_name} | "
        f"FB: {fb_status} | Hotmail: {hotmail_status} | "
        f"ChgPwd: {change_pwd_status} | Shopee: {shopee_status}"
        + (f" | Error: {error_msg}" if error_msg else "")
    )

    return result
