"""
Module ghi log hoạt động.
Sử dụng loguru để ghi log ra console và file.
"""
import os
import sys
from datetime import datetime
from loguru import logger


def setup_logger(log_dir: str = "logs", log_level: str = "INFO") -> None:
    """
    Khởi tạo logger với output ra console và file.

    Args:
        log_dir: Thư mục lưu file log.
        log_level: Mức log (DEBUG, INFO, WARNING, ERROR).
    """
    os.makedirs(log_dir, exist_ok=True)
    logger.remove()

    # Console handler (chỉ thêm khi có sys.stderr, tránh lỗi khi chạy bản .exe --noconsole)
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=log_level,
            format=(
                "<green>{time:HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
                "<level>{message}</level>"
            ),
            colorize=True,
        )

    # File handler
    log_filename = datetime.now().strftime("run_%Y%m%d_%H%M%S.log")
    log_filepath = os.path.join(log_dir, log_filename)
    logger.add(
        log_filepath,
        level="DEBUG",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
    )

    logger.info(f"Logger initialized. Log file: {log_filepath}")


def get_logger():
    """Trả về logger instance."""
    return logger
