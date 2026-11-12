"""
Cấu hình phần mềm tự động đăng nhập Facebook, Hotmail, đổi pass, login Shopee.
Người dùng chỉnh sửa file này để phù hợp với dữ liệu của mình.
"""
import os
from dotenv import load_dotenv

APP_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(APP_DIR, ".env"))

# ============================================================
# GOOGLE SHEETS
# ============================================================
GOOGLE_SHEET_URL = os.getenv(
    "GOOGLE_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"
)
SHEET_NAME = os.getenv("SHEET_NAME", "AE PH Giỏ")
START_ROW = int(os.getenv("START_ROW", "55"))
END_ROW = int(os.getenv("END_ROW", "0"))  # 0 = đọc hết

COL_DATA = "B"              # Cột chứa FB + Hotmail info
COL_PROFILE = "E"           # Cột chứa tên Profile Gemlogin
COL_STATUS = "D"            # Cột ghi trạng thái ("FB die", "reg Shopee lỗi")
COL_USERNAME = "F"          # Cột ghi Username Shopee.PH Affiliate
COL_AFFILIATE_ID = "G"      # Cột ghi Affiliate ID Shopee.PH
COL_FORWARD_EMAIL = "H"     # Cột ghi email Forwarding (@nguyenmaucole.io.vn)
COL_USERNAME_MY = "I"       # Cột ghi Username Shopee MY Affiliate
COL_AFFILIATE_ID_MY = "J"   # Cột ghi Affiliate ID Shopee MY
COL_USERNAME_ID = "K"       # Cột ghi Username Shopee ID Affiliate
COL_AFFILIATE_ID_ID = "L"   # Cột ghi Affiliate ID Shopee ID

SHOPEE_RESULT_COLS = {
    "Shopee.PH": (COL_USERNAME, COL_AFFILIATE_ID),       # (F, G)
    "Shopee MY": (COL_USERNAME_MY, COL_AFFILIATE_ID_MY), # (I, J)
    "Shopee ID": (COL_USERNAME_ID, COL_AFFILIATE_ID_ID), # (K, L)
}

GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
)

# ============================================================
# DATA PARSING - Vị trí cố định trong chuỗi Cột B (split bởi '|')
# ============================================================
DATA_SEPARATOR = "|"

# Index cố định (0-based) - ĐÃ XÁC NHẬN
FB_UID_INDEX = 0              # Facebook UID
FB_PASSWORD_INDEX = 1         # Facebook Password
FB_2FA_SECRET_INDEX = 2       # Facebook 2FA Secret Key
FB_COOKIE_INDEX = 3           # Facebook Cookie
FB_TOKEN_INDEX = 4            # Facebook Access Token
HOTMAIL_EMAIL_INDEX = 5       # Hotmail/Outlook Email
HOTMAIL_PASSWORD_INDEX = 6    # Hotmail Password
MS_TOKEN_INDEX = 7            # Microsoft Token
UUID_INDEX = 8                # UUID

# ============================================================
# GEMLOGIN & GEMCAPTCHA API
# ============================================================
GEMLOGIN_API_PORT = int(os.getenv("GEMLOGIN_API_PORT", "1010"))
GEMLOGIN_API_BASE = f"http://localhost:{GEMLOGIN_API_PORT}/api/v3"
GEMCAPTCHA_API_KEY = os.getenv(
    "GEMCAPTCHA_API_KEY",
    "GEM_IIXATPHUNM8DESMJV53GWNVQYEDAQ0M0X0TNK9KGPKDEGM1HNJKHZI83AFNUKG1778167636",
)
GEMCAPTCHA_EXTENSION_DIR = os.path.join(APP_DIR, "extensions", "gemcaptcha")

# ============================================================
# TIMING & THREADS (giây)
# ============================================================
MAX_THREADS = int(os.getenv("MAX_THREADS", "4"))
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "300"))          # 5 phút (300 giây)
CAPTCHA_CHECK_INTERVAL = int(os.getenv("CAPTCHA_CHECK_INTERVAL", "5"))  # Quét 5s 1 lần
PAGE_LOAD_TIMEOUT = 30
ELEMENT_WAIT_TIMEOUT = 15
DELAY_BETWEEN_ACTIONS = 2
DELAY_BETWEEN_PROFILES = 5
MAX_RETRIES = 2

# ============================================================
# URLs
# ============================================================
FACEBOOK_URL = "https://www.facebook.com/"
HOTMAIL_SUPPORT_URL = (
    "https://support.microsoft.com/vi-vn/accounts-billing"
    "/manage/how-to-sign-in-to-hotmail"
)
HOTMAIL_CHANGE_PASSWORD_URL = "https://account.live.com/proofs/Manage/"

# Shopee URLs cho các vùng
SHOPEE_URLS = {
    "Shopee.PH": "https://shopee.ph/buyer/login",
    "Shopee MY": "https://shopee.com.my/buyer/login",
    "Shopee ID": "https://shopee.co.id/buyer/login",
}

# Shopee Affiliate URLs
SHOPEE_AFFILIATE_URLS = {
    "Shopee.PH": "https://affiliate.shopee.ph/",
    "Shopee MY": "https://affiliate.shopee.com.my/",
    "Shopee ID": "https://affiliate.shopee.co.id/",
}

# ============================================================
# MẬT KHẨU MỚI HOTMAIL & SHOPEE
# ============================================================
NEW_HOTMAIL_PASSWORD = os.getenv("NEW_HOTMAIL_PASSWORD", "Khoa01@@c")
NEW_SHOPEE_PASSWORD = os.getenv("NEW_SHOPEE_PASSWORD", "Khoa01@c")

# ============================================================
# LOGGING & VERSION
# ============================================================
LOG_DIR = "logs"
LOG_LEVEL = "DEBUG"

VERSION_FILE = os.path.join(APP_DIR, "VERSION")
GITHUB_REPO_URL = "https://github.com/thincole/RegSP"
GITHUB_RAW_VERSION_URL = "https://raw.githubusercontent.com/thincole/RegSP/main/VERSION"


def get_app_version() -> str:
    try:
        if os.path.exists(VERSION_FILE):
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                ver = f.read().strip()
                if ver:
                    return ver if ver.upper().startswith("V") else f"V {ver}"
    except Exception:
        pass
    return "V 1.0.1"


APP_VERSION = get_app_version()

