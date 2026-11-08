"""Test dong 72: doc du lieu tu sheet va thu login FB."""
import time
import sys
sys.path.insert(0, '.')

import config
from utils.logger import setup_logger, get_logger
from modules.google_sheets import read_sheet_data
from modules.data_parser import parse_column_b
from modules.gemlogin_manager import find_profile_by_name, start_profile, stop_profile, connect_selenium
from modules.facebook_login import login_facebook

setup_logger('logs', 'DEBUG')
logger = get_logger()

# Doc dong 72 tu sheet
logger.info("Doc dong 72 tu Google Sheet...")
rows = read_sheet_data(
    sheet_url="https://docs.google.com/spreadsheets/d/1Pmw5BR0kKu0SQdhp7AOCt9dD8j6BgMUyzK-JFki4848/edit?gid=664034367#gid=664034367",
    sheet_name="Regnik",
    start_row=72,
    end_row=72,
    col_data="B",
    col_profile="E",
    service_account_file="F:/MMO/GemPhoneFarm.json",
)

if not rows:
    logger.error("Khong doc duoc dong 72!")
    sys.exit(1)

row = rows[0]
logger.info(f"Row {row['row_index']}: profile={row['profile_name']}")
logger.info(f"Raw data (50 chars): {row['raw_data'][:50]}...")

# Parse
parsed = parse_column_b(row['raw_data'])
logger.info(f"FB UID: {parsed['fb_uid']}")
logger.info(f"FB Pass: {parsed['fb_password']}")
logger.info(f"FB Cookie (50 chars): {parsed.get('fb_cookie', '')[:50]}...")
logger.info(f"FB 2FA: {parsed.get('fb_2fa_secret', '')}")

profile_name = row['profile_name']
if not profile_name:
    logger.error("Thieu profile name cot E!")
    sys.exit(1)

# Mo profile Gemlogin
profile_id = find_profile_by_name(profile_name)
profile_info = start_profile(profile_id)
driver = connect_selenium(profile_info)

try:
    # Login Facebook
    result = login_facebook(
        driver,
        parsed['fb_uid'],
        parsed['fb_password'],
        parsed.get('fb_2fa_secret', ''),
        parsed.get('fb_cookie', ''),
    )

    if result:
        logger.info("=== LOGIN FACEBOOK THANH CONG! ===")
        logger.info(f"URL: {driver.current_url}")
    else:
        logger.error("=== LOGIN FACEBOOK THAT BAI ===")

    time.sleep(5)

finally:
    stop_profile(profile_id)
    logger.info("Da dong profile")
