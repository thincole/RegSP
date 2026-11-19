"""
Client tích hợp OMOCaptcha API v2 (https://docs.omocaptcha.com).

Dùng cho luồng giải captcha tự động (Shopee slider, image-to-text).

Quy ước của API v2 (theo tài liệu):
  - Mọi endpoint đều là POST + JSON, HTTP status luôn 200 kể cả khi lỗi nghiệp vụ.
  - Lỗi nghiệp vụ nằm ở body: {"errorId": 1, "errorCode": "...", "errorDescription": "..."}
  - Chỉ retry khi HTTP 5xx hoặc khi gặp các errorCode tạm thời
    (ERROR_TASK_IS_MAINTENANCE / ERROR_SERVICE_UNAVAILABLE / ERROR_REDIS_UNAVAILABLE).
  - getTaskResult trả status "processing" -> chờ 2s rồi hỏi lại; "ready" -> lấy solution.

Client an toàn khi dùng đa luồng: mỗi thread giữ session riêng (threading.local),
vì requests.Session không đảm bảo thread-safe khi dùng chung.
"""
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from utils.logger import get_logger

logger = get_logger()

DEFAULT_OMO_BASE_URL = "https://api.omocaptcha.com/v2"

# Các errorCode được phép thử lại (xem Bảng mã lỗi của OMOCaptcha)
_RETRYABLE_ERROR_CODES = frozenset({
    "ERROR_TASK_IS_MAINTENANCE",
    "ERROR_SERVICE_UNAVAILABLE",
    "ERROR_REDIS_UNAVAILABLE",
    "ERROR_REDIS_JOB_DATA",
})

# Các errorCode phải dừng ngay (không retry, không đốt thêm lượt)
_FATAL_ERROR_CODES = frozenset({
    "ERROR_KEY_DOES_NOT_EXIST",
    "ERROR_TASK_KEY_MISMATCH",
    "ERROR_ZERO_BALANCE",
    "ERROR_INSUFFICIENT_BALANCE",
    "ERROR_PACKAGE_NOT_FOUND",
    "ERROR_PACKAGE_EXPIRED",
    "ERROR_PACKAGE_EXHAUSTED",
    "ERROR_PACKAGE_NOT_SUPPORT",
    "ERROR_TASK_NOT_SUPPORTED",
    "ERROR_TYPE_JOB_REQUIRED",
    "ERROR_TASK_NOT_FOUND",
})

# Chu kỳ chờ khi server trả status = processing (tài liệu khuyến nghị 2s)
_RESULT_POLL_SECONDS = 2.0


class OmocaptchaError(Exception):
    """Lỗi trả về từ OMOCaptcha API hoặc lỗi mạng/timeout khi giải captcha."""

    def __init__(
        self,
        code: str = "",
        message: str = "",
        http_status: Optional[int] = None,
        retryable: bool = False,
    ):
        self.code = (code or "").strip()
        self.message = (message or "").strip()
        self.http_status = http_status
        self.retryable = retryable
        detail = f"[{self.code}] {self.message}" if self.code else self.message
        super().__init__(detail or "Lỗi OMOCaptcha API không xác định")


def strip_data_uri(value: str) -> str:
    """
    Nhận base64 hoặc data URI ('data:image/png;base64,...') và trả về base64 thuần.
    API chỉ nhận base64, có prefix sẽ bị coi là ảnh hỏng.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("data:"):
        marker = text.find(";base64,")
        if marker == -1:
            return ""
        return text[marker + len(";base64,"):].strip()
    return text


class OmocaptchaClient:
    """REST client cho OMOCaptcha API v2."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_OMO_BASE_URL,
        request_timeout: int = 30,
        max_retries: int = 2,
    ):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_OMO_BASE_URL).rstrip("/")
        self.request_timeout = max(5, int(request_timeout or 30))
        self.max_retries = max(0, int(max_retries or 0))
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Hạ tầng HTTP
    # ------------------------------------------------------------------
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Content-Type": "application/json"})
            self._local.session = session
        return session

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST JSON tới endpoint v2, trả body đã parse. Chỉ raise khi lỗi hạ tầng."""
        if not self.api_key:
            raise OmocaptchaError("MISSING_API_KEY", "Chưa cấu hình OMOCaptcha API Key")

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        body = {"clientKey": self.api_key}
        body.update(payload or {})

        try:
            resp = self._session().post(url, json=body, timeout=self.request_timeout)
        except requests.RequestException as e:
            raise OmocaptchaError("NETWORK_ERROR", f"Lỗi kết nối OMOCaptcha: {e}") from e

        if resp.status_code >= 500:
            raise OmocaptchaError(
                f"HTTP_{resp.status_code}",
                f"OMOCaptcha lỗi hạ tầng (HTTP {resp.status_code})",
                http_status=resp.status_code,
                retryable=True,
            )
        if resp.status_code >= 400:
            raise OmocaptchaError(
                f"HTTP_{resp.status_code}",
                f"OMOCaptcha từ chối request (HTTP {resp.status_code})",
                http_status=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise OmocaptchaError(
                "INVALID_RESPONSE",
                f"OMOCaptcha trả về non-JSON (HTTP {resp.status_code})",
                http_status=resp.status_code,
            ) from e

        if not isinstance(data, dict):
            raise OmocaptchaError("INVALID_RESPONSE", "OMOCaptcha trả về payload không phải object")
        return data

    @staticmethod
    def _raise_for_business_error(data: Dict[str, Any], context: str) -> None:
        """errorId != 0 -> raise OmocaptchaError với cờ retryable theo bảng mã lỗi."""
        try:
            error_id = int(data.get("errorId", 0) or 0)
        except (TypeError, ValueError):
            error_id = 1
        if error_id == 0:
            return

        code = str(data.get("errorCode", "") or "").strip()
        desc = str(data.get("errorDescription", "") or "").strip()
        if not desc:
            desc = str(data.get("status", "") or "").strip()
        raise OmocaptchaError(
            code=code or "UNKNOWN_ERROR",
            message=f"{context}: {desc or 'không rõ nguyên nhân'}",
            retryable=code in _RETRYABLE_ERROR_CODES,
        )

    def _call_with_retry(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        context: str,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """POST + xử lý errorId, tự retry với exponential backoff cho lỗi tạm thời."""
        last_error: Optional[OmocaptchaError] = None
        for attempt in range(self.max_retries + 1):
            if stop_check:
                stop_check()
            try:
                data = self._post(endpoint, payload)
                self._raise_for_business_error(data, context)
                return data
            except OmocaptchaError as e:
                last_error = e
                if e.code in _FATAL_ERROR_CODES or not e.retryable:
                    raise
                if attempt >= self.max_retries:
                    raise
                wait_s = min(8.0, 1.5 * (2 ** attempt))
                logger.warning(
                    f"⏳ OMOCaptcha {endpoint.split('/')[-1]} lỗi tạm thời [{e.code}], "
                    f"chờ {wait_s:.1f}s rồi thử lại ({attempt + 1}/{self.max_retries})..."
                )
                time.sleep(wait_s)
        raise last_error or OmocaptchaError("UNKNOWN_ERROR", f"{context}: không thể gọi API")

    # ------------------------------------------------------------------
    # Endpoint dịch vụ / tài khoản
    # ------------------------------------------------------------------
    def get_service_price(self, kind: str = "service") -> Dict[str, Any]:
        """
        POST /v2/getServicePrice.

        kind='service' -> danh sách captcha bán lẻ (key 'services').
        kind='package' -> danh sách gói combo (key 'packages').
        """
        kind_norm = "package" if str(kind or "").strip().lower() == "package" else "service"
        return self._call_with_retry(
            "getServicePrice", {"type": kind_norm}, "getServicePrice"
        )

    def get_services(self) -> list:
        """Danh sách captcha bán lẻ kèm giá / độ chính xác / thời gian trung bình."""
        return list(self.get_service_price("service").get("services") or [])

    def get_packages(self) -> list:
        """Danh sách gói combo đang bán kèm giá tháng và các type được cover."""
        return list(self.get_service_price("package").get("packages") or [])

    def get_balance(self) -> Dict[str, Any]:
        """POST /v2/getBalance -> {'balance': float, 'voucherBalance': float, 'quantity': float|None}."""
        data = self._call_with_retry("getBalance", {}, "getBalance")
        out: Dict[str, Any] = {"balance": 0.0, "voucherBalance": 0.0, "quantity": None}
        for key in ("balance", "voucherBalance"):
            try:
                out[key] = float(data.get(key, 0) or 0)
            except (TypeError, ValueError):
                out[key] = 0.0
        quantity = data.get("quantity")
        if quantity not in (None, ""):
            try:
                out["quantity"] = float(quantity)
            except (TypeError, ValueError):
                out["quantity"] = None
        return out

    # ------------------------------------------------------------------
    # Endpoint giải captcha
    # ------------------------------------------------------------------
    def create_task(
        self,
        task: Dict[str, Any],
        stop_check: Optional[Callable[[], None]] = None,
    ) -> str:
        """Tạo task, trả về taskId. Tự retry cho lỗi tạm thời."""
        if not isinstance(task, dict) or not task:
            raise OmocaptchaError("EMPTY_TASK", "Task rỗng, không thể tạo yêu cầu")
        if not str(task.get("type", "") or "").strip():
            raise OmocaptchaError("ERROR_TYPE_JOB_REQUIRED", "Task thiếu 'type'")

        data = self._call_with_retry("createTask", {"task": task}, "createTask", stop_check)
        task_id = str(data.get("taskId", "") or "").strip()
        if not task_id:
            raise OmocaptchaError("INVALID_RESPONSE", "createTask không trả về taskId")
        return task_id

    def get_task_result(self, task_id: str) -> Dict[str, Any]:
        """Hỏi kết quả 1 lần. Trả body gốc (status = processing | ready | fail)."""
        task_id = (task_id or "").strip()
        if not task_id:
            raise OmocaptchaError("ERROR_TASK_NOT_FOUND", "Thiếu taskId khi hỏi kết quả")
        return self._post("getTaskResult", {"taskId": task_id})

    def wait_solution(
        self,
        task_id: str,
        timeout: float = 120.0,
        poll_interval: float = _RESULT_POLL_SECONDS,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """
        Poll getTaskResult tới khi có solution.

        Trả dict solution (vd {'end': {'x': .., 'y': ..}} hoặc {'text': 'ABC'}).
        Raise OmocaptchaError khi job fail, hết hạn chờ hoặc bị dừng.
        """
        deadline = time.time() + max(5.0, float(timeout))
        poll_s = max(0.5, float(poll_interval))
        last_status = ""

        while time.time() < deadline:
            if stop_check:
                stop_check()
            try:
                data = self.get_task_result(task_id)
            except OmocaptchaError as e:
                if e.retryable:
                    time.sleep(poll_s)
                    continue
                raise

            error_id = int(data.get("errorId", 0) or 0)
            status = str(data.get("status", "") or "").strip().lower()

            if status == "ready" and error_id == 0:
                solution = data.get("solution")
                if isinstance(solution, dict) and solution:
                    return solution
                raise OmocaptchaError("INVALID_RESPONSE", "Task ready nhưng solution rỗng")

            if error_id != 0 or status == "fail":
                code = str(data.get("errorCode", "") or "").strip() or "ERROR_JOB_STATUS"
                desc = str(data.get("errorDescription", "") or "").strip() or "Job failed"
                raise OmocaptchaError(code, f"Task {task_id} thất bại: {desc}")

            if status != last_status:
                last_status = status
                logger.debug(f"OMOCaptcha task {task_id} -> status={status or 'unknown'}")
            time.sleep(poll_s)

        raise OmocaptchaError(
            "SOLVE_TIMEOUT",
            f"Hết thời gian chờ kết quả OMOCaptcha sau {timeout:.0f}s",
            retryable=True,
        )

    def solve(
        self,
        task: Dict[str, Any],
        timeout: float = 120.0,
        poll_interval: float = _RESULT_POLL_SECONDS,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """createTask + wait_solution trong 1 lần gọi."""
        task_id = self.create_task(task, stop_check=stop_check)
        logger.debug(f"OMOCaptcha đã tạo task {task_id} (type={task.get('type')})")
        return self.wait_solution(
            task_id, timeout=timeout, poll_interval=poll_interval, stop_check=stop_check
        )

    # ------------------------------------------------------------------
    # Tiện ích theo từng loại captcha
    # ------------------------------------------------------------------
    def solve_shopee_slider(
        self,
        mask_image: str,
        background_image: str,
        rotate: bool = False,
        timeout: float = 120.0,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """
        ShopeeSliderWebTask.

        Thứ tự ảnh theo tài liệu là [MASK, BACKGROUND].
        - Dạng thường  -> solution.end.x / end.y (toạ độ đích của mảnh ghép, px ảnh nền).
        - Dạng rotate  -> solution.point.x / point.y (điểm đích của mảnh ghép).

        Trả dict chuẩn hoá:
          {'mode': 'x',     'x': float, 'y': float}      # quỹ đạo ngang
          {'mode': 'point', 'x': float, 'y': float}      # quỹ đạo tự do
        """
        mask_b64 = strip_data_uri(mask_image)
        bg_b64 = strip_data_uri(background_image)
        if not mask_b64 or not bg_b64:
            raise OmocaptchaError("EMPTY_IMAGE", "Thiếu ảnh mask hoặc ảnh nền của captcha")
        if len(mask_b64) < 200 or len(bg_b64) < 200:
            raise OmocaptchaError("EMPTY_IMAGE", "Ảnh captcha quá nhỏ, có thể chụp hụt vùng captcha")

        task: Dict[str, Any] = {
            "type": "ShopeeSliderWebTask",
            "imageBase64s": [mask_b64, bg_b64],
        }
        if rotate:
            task["typeCaptcha"] = "rotate"

        solution = self.solve(task, timeout=timeout, stop_check=stop_check)
        return self._normalize_slider_solution(solution)

    @staticmethod
    def _normalize_slider_solution(solution: Dict[str, Any]) -> Dict[str, Any]:
        """Đọc solution của ShopeeSliderWebTask về dạng thống nhất."""

        def _coord(node: Any) -> Optional[Tuple[float, float]]:
            if not isinstance(node, dict):
                return None
            try:
                return float(node.get("x")), float(node.get("y", 0) or 0)
            except (TypeError, ValueError):
                return None

        point = _coord(solution.get("point"))
        if point:
            return {"mode": "point", "x": point[0], "y": point[1]}

        end = _coord(solution.get("end"))
        if end:
            return {"mode": "x", "x": end[0], "y": end[1]}

        # Một số job trả thẳng x/y ở cấp solution
        flat = _coord(solution)
        if flat:
            return {"mode": "x", "x": flat[0], "y": flat[1]}

        raise OmocaptchaError(
            "INVALID_SOLUTION",
            f"Solution không có toạ độ hợp lệ: {list(solution.keys())}",
        )

    def solve_image_to_text(
        self,
        image: str,
        module: Optional[str] = None,
        timeout: float = 120.0,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> str:
        """ImageToTextTask -> chuỗi text đọc được từ ảnh captcha."""
        image_b64 = strip_data_uri(image)
        if not image_b64:
            raise OmocaptchaError("EMPTY_IMAGE", "Thiếu ảnh captcha cho ImageToTextTask")

        task: Dict[str, Any] = {"type": "ImageToTextTask", "imageBase64": image_b64}
        if module:
            task["module"] = module

        solution = self.solve(task, timeout=timeout, stop_check=stop_check)
        text = str(solution.get("text", "") or "").strip()
        if not text:
            raise OmocaptchaError("INVALID_SOLUTION", "ImageToTextTask không trả về text")
        return text


# ======================================================================
# Cấu hình dùng chung toàn tiến trình
# ======================================================================
_config_lock = threading.Lock()
_config: Dict[str, Any] = {
    "api_key": "",
    "enabled": False,
    "base_url": DEFAULT_OMO_BASE_URL,
    "request_timeout": 30,
    "max_retries": 2,
    "solve_timeout": 120,
    "poll_interval": _RESULT_POLL_SECONDS,
    "drag_attempts": 3,
    "fallback_manual": True,
    "shopee_rotate": False,
}
_client: Optional[OmocaptchaClient] = None


def configure(
    api_key: Optional[str] = None,
    enabled: Optional[bool] = None,
    base_url: Optional[str] = None,
    request_timeout: Optional[int] = None,
    max_retries: Optional[int] = None,
    solve_timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
    drag_attempts: Optional[int] = None,
    fallback_manual: Optional[bool] = None,
    shopee_rotate: Optional[bool] = None,
) -> None:
    """
    Cập nhật cấu hình OMOCaptcha dùng chung (gọi 1 lần khi bắt đầu chạy từ GUI).

    Tham số None = giữ nguyên giá trị cũ. Client singleton được tạo lại khi key đổi.
    """
    global _client
    with _config_lock:
        if api_key is not None:
            _config["api_key"] = (api_key or "").strip()
        if enabled is not None:
            _config["enabled"] = bool(enabled)
        if base_url is not None:
            _config["base_url"] = (base_url or DEFAULT_OMO_BASE_URL).strip() or DEFAULT_OMO_BASE_URL
        if request_timeout is not None:
            _config["request_timeout"] = max(5, int(request_timeout))
        if max_retries is not None:
            _config["max_retries"] = max(0, int(max_retries))
        if solve_timeout is not None:
            _config["solve_timeout"] = max(10.0, float(solve_timeout))
        if poll_interval is not None:
            _config["poll_interval"] = max(0.5, float(poll_interval))
        if drag_attempts is not None:
            _config["drag_attempts"] = max(1, int(drag_attempts))
        if fallback_manual is not None:
            _config["fallback_manual"] = bool(fallback_manual)
        if shopee_rotate is not None:
            _config["shopee_rotate"] = bool(shopee_rotate)

        # Key đổi -> dựng lại client theo key mới
        _client = None


def get_config() -> Dict[str, Any]:
    """Bản sao cấu hình hiện tại."""
    with _config_lock:
        return dict(_config)


def is_enabled() -> bool:
    """True nếu đã bật auto-solve và có API key."""
    with _config_lock:
        return bool(_config.get("enabled")) and bool(_config.get("api_key"))


def get_client() -> Optional[OmocaptchaClient]:
    """Client singleton, None nếu chưa bật hoặc thiếu key."""
    global _client
    with _config_lock:
        if not (_config.get("enabled") and _config.get("api_key")):
            return None
        if _client is None:
            _client = OmocaptchaClient(
                api_key=_config["api_key"],
                base_url=_config["base_url"],
                request_timeout=_config["request_timeout"],
                max_retries=_config["max_retries"],
            )
        return _client


def load_from_config_module() -> None:
    """Nạp cấu hình từ config.py (dùng khi chạy không qua GUI)."""
    try:
        import config as app_config
    except Exception:
        return

    configure(
        api_key=getattr(app_config, "OMO_API_KEY", "") or "",
        enabled=bool(getattr(app_config, "OMO_ENABLED", False)),
        base_url=getattr(app_config, "OMO_BASE_URL", DEFAULT_OMO_BASE_URL),
        request_timeout=getattr(app_config, "OMO_REQUEST_TIMEOUT", 30),
        max_retries=getattr(app_config, "OMO_MAX_TASK_RETRIES", 2),
        solve_timeout=getattr(app_config, "OMO_SOLVE_TIMEOUT", 120),
        poll_interval=getattr(app_config, "OMO_POLL_INTERVAL", _RESULT_POLL_SECONDS),
        drag_attempts=getattr(app_config, "OMO_DRAG_ATTEMPTS", 3),
        fallback_manual=getattr(app_config, "OMO_FALLBACK_MANUAL", True),
        shopee_rotate=getattr(app_config, "OMO_SHOPEE_ROTATE", False),
    )
