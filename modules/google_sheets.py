"""
Module đọc dữ liệu từ Google Sheets.
Hỗ trợ 2 phương thức: Service Account và Public CSV export.
"""
import os
import re
import csv
import io
import threading
from typing import List, Dict, Optional

import requests

from utils.logger import get_logger
from utils.error_handler import SheetReadError

logger = get_logger()
_sheet_write_lock = threading.Lock()


def _extract_sheet_id(url: str) -> str:
    """Trích xuất Sheet ID từ URL."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not match:
        raise SheetReadError(f"Không thể trích xuất Sheet ID từ URL: {url}")
    return match.group(1)


def _extract_gid(url: str) -> str:
    """Trích xuất GID (tab ID) từ URL."""
    match = re.search(r"gid=(\d+)", url)
    return match.group(1) if match else "0"


def _col_letter_to_index(col_letter: str) -> int:
    """Chuyển ký tự cột (A, B, E...) sang index 0-based."""
    col_letter = col_letter.upper()
    result = 0
    for char in col_letter:
        result = result * 26 + (ord(char) - ord('A') + 1)
    return result - 1


def read_via_service_account(
    sheet_url: str, sheet_name: str, service_account_file: str,
    start_row: int, end_row: int, col_data: str, col_profile: str,
) -> List[Dict]:
    """Đọc Google Sheets qua Service Account (gspread)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(service_account_file, scopes=scopes)
        client = gspread.authorize(creds)

        sheet_id = _extract_sheet_id(sheet_url)
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet(sheet_name)
        all_values = worksheet.get_all_values()

        col_data_idx = _col_letter_to_index(col_data)
        col_profile_idx = _col_letter_to_index(col_profile)

        rows = []
        actual_end = end_row if end_row > 0 else len(all_values)

        for i in range(start_row - 1, min(actual_end, len(all_values))):
            row = all_values[i]
            raw_data = row[col_data_idx] if col_data_idx < len(row) else ""
            profile_name = row[col_profile_idx] if col_profile_idx < len(row) else ""

            if raw_data:
                pname = profile_name.strip() if profile_name else ""
                rows.append({
                    "row_index": i + 1,
                    "raw_data": raw_data,
                    "profile_name": pname,
                })

        logger.info(f"Đọc được {len(rows)} dòng hợp lệ (hàng {start_row} đến {actual_end})")
        return rows

    except ImportError:
        raise SheetReadError("Thư viện gspread chưa cài. Chạy: pip install gspread google-auth")
    except Exception as e:
        raise SheetReadError(f"Lỗi đọc Sheet via Service Account: {e}")


def read_via_public_csv(
    sheet_url: str, start_row: int, end_row: int,
    col_data: str, col_profile: str,
) -> List[Dict]:
    """Đọc Google Sheets qua URL công khai (CSV export)."""
    sheet_id = _extract_sheet_id(sheet_url)
    gid = _extract_gid(sheet_url)
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

    logger.info("Đang tải dữ liệu từ Google Sheets (CSV)...")

    try:
        response = requests.get(csv_url, timeout=30)
        response.raise_for_status()
        response.encoding = "utf-8"

        reader = csv.reader(io.StringIO(response.text))
        all_rows = list(reader)

        col_data_idx = _col_letter_to_index(col_data)
        col_profile_idx = _col_letter_to_index(col_profile)

        rows = []
        actual_end = end_row if end_row > 0 else len(all_rows)

        for i in range(start_row - 1, min(actual_end, len(all_rows))):
            row = all_rows[i]
            raw_data = row[col_data_idx] if col_data_idx < len(row) else ""
            profile_name = row[col_profile_idx] if col_profile_idx < len(row) else ""

            if raw_data:
                pname = profile_name.strip() if profile_name else ""
                rows.append({
                    "row_index": i + 1,
                    "raw_data": raw_data,
                    "profile_name": pname,
                })

        logger.info(f"Đọc được {len(rows)} dòng hợp lệ (hàng {start_row} đến {actual_end})")
        return rows

    except requests.RequestException as e:
        raise SheetReadError(f"Lỗi tải CSV từ Google Sheets: {e}")


def read_sheet_data(
    sheet_url: str, sheet_name: str, start_row: int, end_row: int,
    col_data: str, col_profile: str,
    service_account_file: Optional[str] = None,
) -> List[Dict]:
    """
    Hàm chính đọc dữ liệu từ Google Sheets.
    Ưu tiên Service Account, fallback sang Public CSV.

    Returns:
        List[{row_index, raw_data, profile_name}]
    """
    if service_account_file and os.path.exists(service_account_file):
        logger.info("Sử dụng Google Service Account")
        return read_via_service_account(
            sheet_url, sheet_name, service_account_file,
            start_row, end_row, col_data, col_profile,
        )

    logger.info("Không tìm thấy Service Account, dùng Public CSV")
    return read_via_public_csv(
        sheet_url, start_row, end_row, col_data, col_profile,
    )


def _get_gspread_client(service_account_file: str):
    """Tạo gspread client từ Service Account."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(service_account_file, scopes=scopes)
    return gspread.authorize(creds)


def write_cell(
    sheet_url: str, sheet_name: str, row_index: int,
    col_letter: str, value: str,
    service_account_file: str,
) -> bool:
    """
    Ghi giá trị vào ô cụ thể trên Google Sheet.
    Dùng để ghi "FB die" hoặc "reg Shopee lỗi" vào Cột D.

    Args:
        sheet_url: URL Google Sheet.
        sheet_name: Tên sheet tab.
        row_index: Số hàng (1-indexed).
        col_letter: Ký tự cột (VD: 'D').
        value: Giá trị cần ghi.
        service_account_file: Đường dẫn file JSON.

    Returns:
        True nếu ghi thành công.
    """
    try:
        with _sheet_write_lock:
            client = _get_gspread_client(service_account_file)
            sheet_id = _extract_sheet_id(sheet_url)
            spreadsheet = client.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet(sheet_name)

            col_index = _col_letter_to_index(col_letter) + 1  # gspread dùng 1-indexed
            worksheet.update_cell(row_index, col_index, value)

            logger.info(f"Đã ghi '{value}' vào ô {col_letter}{row_index}")
            return True

    except Exception as e:
        logger.error(f"Lỗi ghi vào Sheet {col_letter}{row_index}: {e}")
        return False
