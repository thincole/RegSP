"""
Các hàm tiện ích để chờ element trên trang web.
"""
import time
from typing import Optional

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
)

from utils.logger import get_logger
from utils.error_handler import BrowserClosedError, is_browser_closed_exception, check_browser_alive

logger = get_logger()


def wait_for_element(
    driver: WebDriver,
    by: By,
    value: str,
    timeout: int = 15,
    condition: str = "presence",
) -> Optional[WebElement]:
    """
    Chờ element xuất hiện trên trang.

    Args:
        driver: WebDriver instance.
        by: Loại selector (By.ID, By.CSS_SELECTOR, By.XPATH...).
        value: Giá trị selector.
        timeout: Thời gian chờ tối đa (giây).
        condition: 'presence', 'visible', hoặc 'clickable'.

    Returns:
        WebElement nếu tìm thấy, None nếu timeout.
    """
    check_browser_alive(driver)
    try:
        wait = WebDriverWait(
            driver, timeout,
            ignored_exceptions=[StaleElementReferenceException],
        )
        if condition == "visible":
            element = wait.until(EC.visibility_of_element_located((by, value)))
        elif condition == "clickable":
            element = wait.until(EC.element_to_be_clickable((by, value)))
        else:
            element = wait.until(EC.presence_of_element_located((by, value)))

        logger.debug(f"Element found: {by}='{value}'")
        return element
    except TimeoutException:
        check_browser_alive(driver)
        logger.warning(f"Timeout ({timeout}s) waiting for element: {by}='{value}'")
        return None
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        raise


def wait_for_page_load(driver: WebDriver, timeout: int = 30) -> bool:
    """
    Chờ trang web load xong hoàn toàn.

    Returns:
        True nếu trang load xong, False nếu timeout.
    """
    check_browser_alive(driver)
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        logger.debug("Page loaded completely")
        return True
    except TimeoutException:
        check_browser_alive(driver)
        logger.warning(f"Page load timeout ({timeout}s)")
        return False
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        raise


def wait_for_url_contains(
    driver: WebDriver, url_part: str, timeout: int = 15
) -> bool:
    """
    Chờ URL hiện tại chứa một chuỗi nhất định.

    Returns:
        True nếu URL chứa chuỗi, False nếu timeout.
    """
    check_browser_alive(driver)
    try:
        WebDriverWait(driver, timeout).until(EC.url_contains(url_part))
        logger.debug(f"URL contains '{url_part}'")
        return True
    except TimeoutException:
        check_browser_alive(driver)
        logger.warning(f"Timeout ({timeout}s) waiting for URL to contain '{url_part}'")
        return False
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        raise


def wait_for_new_window(driver: WebDriver, current_handles: list, timeout: int = 15) -> Optional[str]:
    """
    Chờ window/tab mới xuất hiện.

    Args:
        driver: WebDriver instance.
        current_handles: Danh sách window handles trước khi action.
        timeout: Thời gian chờ tối đa.

    Returns:
        Handle của window mới nếu có, None nếu timeout.
    """
    check_browser_alive(driver)
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.window_handles) > len(current_handles)
        )
        new_handles = set(driver.window_handles) - set(current_handles)
        if new_handles:
            new_handle = new_handles.pop()
            logger.debug(f"New window detected: {new_handle}")
            return new_handle
        return None
    except TimeoutException:
        check_browser_alive(driver)
        logger.warning(f"Timeout ({timeout}s) waiting for new window")
        return None
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        raise


def safe_click(element: WebElement, delay: float = 0.5) -> bool:
    """
    Click element một cách an toàn.

    Returns:
        True nếu click thành công.
    """
    try:
        element.click()
        time.sleep(delay)
        logger.debug("Element clicked successfully")
        return True
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Failed to click element: {e}")
        return False


def safe_send_keys(
    element: WebElement, text: str, delay: float = 0.3, clear_first: bool = True
) -> bool:
    """
    Nhập text vào element một cách an toàn (hỗ trợ cả React/FluentUI input có autofill).

    Returns:
        True nếu nhập thành công.
    """
    try:
        if clear_first:
            element.clear()
            if element.get_attribute("value"):
                element.send_keys(Keys.CONTROL + "a")
                element.send_keys(Keys.BACKSPACE)
        element.send_keys(text)
        # Kiểm tra nếu React/autofill làm nhân đôi chuỗi vừa nhập (vd 'Khoa01@@cKhoa01@@c')
        if clear_first:
            cur_val = element.get_attribute("value") or ""
            if cur_val and cur_val != text:
                element.send_keys(Keys.CONTROL + "a")
                element.send_keys(Keys.BACKSPACE)
                element.send_keys(text)
        time.sleep(delay)
        logger.debug(f"Text entered successfully (length={len(text)})")
        return True
    except Exception as e:
        if is_browser_closed_exception(e):
            raise BrowserClosedError(f"Trình duyệt đã bị đóng: {e}") from e
        logger.error(f"Failed to send keys: {e}")
        return False
