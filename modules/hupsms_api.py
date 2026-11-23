"""
Client tích hợp HupSMS API (https://hupsms.com/api/v1) để thuê SIM Việt Nam nhận OTP SMS.
Dùng cho luồng xác minh SĐT khi đăng nhập Gmail/Google.
"""
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from utils.logger import get_logger

logger = get_logger()

DEFAULT_HUPSMS_BASE_URL = "https://hupsms.com/api/v1"
_NON_GMAIL_GOOGLE_KEYWORDS = ("youtube", "voice", "ads", "cloud", "play", "drive")
DEFAULT_EXCLUDED_SERVERS = ("Server 1",)


def _canonical_server_key(server_value: Any) -> str:
    """Chuẩn hóa tên server (VD: 'Server 1', 'server1', 'SV1', '1' -> '1'; 'Server 10' -> '10')."""
    s = re.sub(r"\s+", " ", str(server_value or "").strip().lower())
    if not s:
        return ""
    m = re.match(r"^(?:server|sv|s)\s*[-_:#]?\s*(\d+)\b", s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    return s


def is_excluded_server(
    server_value: Any,
    excluded_servers: Optional[Iterable[str]] = None,
) -> bool:
    """Kiểm tra server có nằm trong danh sách loại trừ hay không (mặc định loại Server 1)."""
    targets = DEFAULT_EXCLUDED_SERVERS if excluded_servers is None else excluded_servers
    key = _canonical_server_key(server_value)
    if not key:
        return False
    excluded_keys = {_canonical_server_key(x) for x in targets if str(x or "").strip()}
    return key in excluded_keys


class HupSmsError(Exception):
    """Lỗi trả về từ HupSMS API hoặc lỗi mạng/timeout khi thuê SIM."""

    def __init__(self, code: str = "", message: str = "", http_status: Optional[int] = None):
        self.code = (code or "").strip()
        self.message = (message or "").strip()
        self.http_status = http_status
        detail = f"[{self.code}] {self.message}" if self.code else self.message
        super().__init__(detail or "Lỗi HupSMS API không xác định")


def normalize_vn_phone(phone: str, e164: bool = False) -> str:
    """
    Chuẩn hóa số điện thoại Việt Nam về dạng local (`0xxxxxxxxx`) hoặc E.164 (`+84xxxxxxxxx`).
    """
    raw = (phone or "").strip()
    if not raw:
        return ""

    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return raw

    subscriber = ""
    if (has_plus and digits.startswith("84")) or (digits.startswith("84") and len(digits) >= 11):
        rest = digits[2:]
        subscriber = rest.lstrip("0") if rest.startswith("0") else rest
    elif digits.startswith("0") and len(digits) >= 9:
        subscriber = digits[1:]
    elif len(digits) == 9 and digits[0] in ("3", "5", "7", "8", "9"):
        subscriber = digits
    else:
        return f"+{digits}" if (e164 and not digits.startswith("+")) else digits

    return f"+84{subscriber}" if e164 else f"0{subscriber}"


def extract_otp_code(otp: Optional[str], sms_content: Optional[str] = None) -> str:
    """
    Trích xuất mã OTP (ưu tiên 6 chữ số) từ trường `otp` hoặc nội dung `smsContent`.
    """
    for candidate in (otp or "", sms_content or ""):
        text = str(candidate).strip()
        if not text:
            continue
        m_google = re.search(r"G-(\d{4,8})\b", text, re.IGNORECASE)
        if m_google:
            return m_google.group(1)
        m_six = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if m_six:
            return m_six.group(1)
    for candidate in (otp or "", sms_content or ""):
        text = str(candidate).strip()
        if not text:
            continue
        m_any = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
        if m_any:
            return m_any.group(1)
    return ""


class HupSmsClient:
    """REST client gọi HupSMS API v1."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_HUPSMS_BASE_URL,
        lang: str = "vi",
        request_timeout: int = 20,
        session: Optional[requests.Session] = None,
    ):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_HUPSMS_BASE_URL).rstrip("/")
        self.lang = (lang or "vi").strip()
        self.request_timeout = request_timeout
        self._session = session or requests.Session()

    def _request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        max_rate_limit_retries: int = 2,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Any:
        """Gọi GET tới endpoint HupSMS và trả về payload `data`."""
        if not self.api_key:
            raise HupSmsError("MISSING_API_KEY", "Chưa cấu hình HupSMS API Key")

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        query: Dict[str, Any] = {"api_key": self.api_key, "lang": self.lang}
        if params:
            for k, v in params.items():
                if v is not None and str(v) != "":
                    query[k] = v

        last_err: Optional[Exception] = None
        for attempt in range(max_rate_limit_retries + 1):
            if stop_check:
                stop_check()
            try:
                resp = self._session.get(url, params=query, timeout=self.request_timeout)
            except requests.RequestException as e:
                last_err = e
                if attempt < max_rate_limit_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise HupSmsError("NETWORK_ERROR", f"Lỗi kết nối HupSMS: {e}") from e

            try:
                payload = resp.json()
            except ValueError as e:
                if resp.status_code == 429 and attempt < max_rate_limit_retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise HupSmsError(
                    "INVALID_RESPONSE",
                    f"HupSMS trả về non-JSON (HTTP {resp.status_code})",
                    http_status=resp.status_code,
                ) from e

            status_val = str(payload.get("status", "")).lower()
            code_val = str(payload.get("code", "")).strip()
            msg_val = str(payload.get("message", "")).strip()

            if status_val == "success" and resp.status_code < 400:
                return payload.get("data")

            if (code_val == "RATE_LIMIT" or resp.status_code == 429) and attempt < max_rate_limit_retries:
                wait_s = 2.0 * (attempt + 1)
                logger.warning(f"⏳ HupSMS RATE_LIMIT tại /{endpoint.lstrip('/')}, chờ {wait_s:.0f}s rồi thử lại...")
                time.sleep(wait_s)
                continue

            raise HupSmsError(
                code=code_val or f"HTTP_{resp.status_code}",
                message=msg_val or f"HTTP {resp.status_code}",
                http_status=resp.status_code,
            )

        raise HupSmsError("NETWORK_ERROR", str(last_err or "Không thể gọi HupSMS API"))

    def get_balance(self, stop_check: Optional[Callable[[], None]] = None) -> int:
        """Kiểm tra số dư tài khoản HupSMS (VND)."""
        data = self._request("balance", stop_check=stop_check) or {}
        try:
            return int(data.get("balance", 0))
        except (TypeError, ValueError):
            return 0

    def get_services(
        self,
        server: Optional[str] = None,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Lấy danh sách dịch vụ HupSMS (tùy chọn lọc theo `server`)."""
        params = {"server": server} if server else None
        data = self._request("services", params=params, stop_check=stop_check)
        return data if isinstance(data, list) else []

    def find_google_services(
        self,
        max_price: Optional[int] = None,
        server: Optional[str] = None,
        exclude_servers: Optional[Iterable[str]] = DEFAULT_EXCLUDED_SERVERS,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Tìm các dịch vụ Google/Gmail và sắp xếp ưu tiên dịch vụ chuẩn, giá thấp và timeout phù hợp.
        Mặc định loại trừ Server 1.
        """
        services = self.get_services(server=server, stop_check=stop_check)
        candidates: List[Dict[str, Any]] = []
        for svc in services:
            if not isinstance(svc, dict):
                continue
            if exclude_servers and is_excluded_server(svc.get("server"), exclude_servers):
                continue
            name_low = str(svc.get("name", "")).strip().lower()
            if not ("google" in name_low or "gmail" in name_low):
                continue
            if any(bad in name_low for bad in _NON_GMAIL_GOOGLE_KEYWORDS):
                continue
            try:
                price = int(svc.get("price", 0))
            except (TypeError, ValueError):
                continue
            if max_price is not None and price > max_price:
                continue
            candidates.append(svc)

        def _rank(item: Dict[str, Any]):
            name_l = str(item.get("name", "")).strip().lower()
            exact_rank = 0 if name_l == "google/gmail" else (1 if "google/gmail" in name_l else 2)
            price_val = int(item.get("price", 0))
            timeout_val = int(item.get("timeoutMinutes", 0) or 0)
            svc_id = int(item.get("id", 0) or 0)
            return (exact_rank, price_val, -timeout_val, svc_id)

        candidates.sort(key=_rank)
        return candidates

    def rent_number(
        self,
        service_id: int,
        network: str = "",
        prefix: str = "",
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """Thuê 1 số điện thoại cho `service_id` cụ thể."""
        params: Dict[str, Any] = {"serviceId": int(service_id)}
        if network and network.strip():
            params["network"] = network.strip()
        if prefix and prefix.strip():
            params["prefix"] = prefix.strip()

        data = self._request("rent", params=params, stop_check=stop_check) or {}
        phone_raw = str(data.get("phone", "")).strip()
        return {
            **data,
            "serviceId": int(service_id),
            "phoneLocal": normalize_vn_phone(phone_raw, e164=False),
            "phoneE164": normalize_vn_phone(phone_raw, e164=True),
        }

    def rent_google_number(
        self,
        service_id: int = 0,
        network: str = "",
        prefix: str = "",
        exclude_service_ids: Optional[List[int]] = None,
        exclude_servers: Optional[Iterable[str]] = DEFAULT_EXCLUDED_SERVERS,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """
        Thuê số cho Google/Gmail:
        - Nếu `service_id > 0`: ưu tiên dùng đúng Service ID được chỉ định (từ chối nếu thuộc server bị loại trừ).
        - Nếu `service_id <= 0`: tự động lấy danh sách Google/Gmail vừa túi tiền và fallback qua từng server (mặc định không thuê Server 1).
        - Tự bỏ qua `network` nếu server trả `INVALID_PARAMETERS` do không hỗ trợ chọn nhà mạng.
        """
        balance = self.get_balance(stop_check=stop_check)
        if balance <= 0:
            raise HupSmsError("INSUFFICIENT_BALANCE", f"Số dư HupSMS hiện bằng {balance}đ")

        excluded = set(int(x) for x in (exclude_service_ids or []) if x)
        raw_google = self.find_google_services(max_price=None, exclude_servers=(), stop_check=stop_check)
        svc_map = {int(s["id"]): s for s in raw_google if "id" in s}
        all_google = [
            s for s in raw_google
            if not (exclude_servers and is_excluded_server(s.get("server"), exclude_servers))
        ]

        candidate_ids: List[int] = []
        if service_id and int(service_id) > 0:
            sid_int = int(service_id)
            svc_info = svc_map.get(sid_int, {})
            if exclude_servers and is_excluded_server(svc_info.get("server"), exclude_servers):
                raise HupSmsError(
                    "SERVER_EXCLUDED",
                    f"Không thuê dịch vụ ID={sid_int} ({svc_info.get('server')}) vì thuộc danh sách server bị loại trừ",
                )
            candidate_ids = [sid_int]
        else:
            affordable = [
                int(s["id"])
                for s in all_google
                if int(s.get("price", 0)) <= balance and int(s["id"]) not in excluded
            ]
            if not affordable:
                if all_google:
                    min_price = min(int(s.get("price", 0)) for s in all_google)
                    raise HupSmsError(
                        "INSUFFICIENT_BALANCE",
                        f"Số dư HupSMS ({balance}đ) không đủ thuê Google/Gmail (rẻ nhất {min_price}đ)",
                    )
                raise HupSmsError("SERVICE_NOT_FOUND", "Không tìm thấy dịch vụ Google/Gmail phù hợp trên HupSMS (đã loại Server 1)")
            candidate_ids = affordable

        last_err: Optional[HupSmsError] = None
        for sid in candidate_ids:
            svc_info = svc_map.get(sid, {})
            svc_label = f"ID={sid} ({svc_info.get('name', 'Google/Gmail')} - {svc_info.get('server', '?')}, {svc_info.get('price', '?')}đ)"
            try:
                order = self.rent_number(sid, network=network, prefix=prefix, stop_check=stop_check)
            except HupSmsError as e:
                if e.code == "INVALID_PARAMETERS" and (network or "").strip():
                    logger.info(f"ℹ️ {svc_label} không hỗ trợ filter network='{network}', thử thuê không filter network...")
                    try:
                        order = self.rent_number(sid, network="", prefix=prefix, stop_check=stop_check)
                    except HupSmsError as e2:
                        last_err = e2
                        logger.warning(f"⚠️ HupSMS không thuê được {svc_label}: {e2}")
                        if e2.code in ("RENTAL_LIMIT_EXCEEDED", "MISSING_API_KEY"):
                            raise
                        continue
                else:
                    last_err = e
                    logger.warning(f"⚠️ HupSMS không thuê được {svc_label}: {e}")
                    if e.code in ("RENTAL_LIMIT_EXCEEDED", "MISSING_API_KEY"):
                        raise
                    continue

            order["serviceName"] = svc_info.get("name", "Google/Gmail")
            order["server"] = svc_info.get("server", "")
            order["timeoutMinutes"] = svc_info.get("timeoutMinutes")
            return order

        if last_err:
            raise last_err
        raise HupSmsError("NO_NUMBER_AVAILABLE", "Không thuê được số Google/Gmail từ HupSMS")

    def check_order(
        self,
        order_id: str,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """Kiểm tra trạng thái đơn thuê SIM và OTP đã nhận."""
        oid = (order_id or "").strip()
        if not oid:
            raise HupSmsError("INVALID_PARAMETERS", "Thiếu orderId để kiểm tra")
        return self._request(f"check/{oid}", stop_check=stop_check) or {}

    def cancel_order(
        self,
        order_id: str,
        ignore_errors: bool = False,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """Hủy đơn đang pending để hoàn tiền (nếu server hỗ trợ)."""
        oid = (order_id or "").strip()
        if not oid:
            return {"cancelled": False, "error_code": "INVALID_PARAMETERS", "message": "Thiếu orderId"}
        try:
            data = self._request(f"cancel/{oid}", stop_check=stop_check) or {}
            return {"cancelled": True, **data}
        except HupSmsError as e:
            if not ignore_errors:
                raise
            logger.info(f"ℹ️ HupSMS cancel đơn {oid} bỏ qua ({e.code}: {e.message})")
            return {"cancelled": False, "error_code": e.code, "message": e.message}

    def rerent_number(
        self,
        phone: str,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """Thuê lại số điện thoại đã dùng trước đó."""
        clean_phone = normalize_vn_phone(phone, e164=False)
        data = self._request("rerent", params={"phone": clean_phone}, stop_check=stop_check) or {}
        phone_raw = str(data.get("phone", clean_phone)).strip()
        return {
            **data,
            "phoneLocal": normalize_vn_phone(phone_raw, e164=False),
            "phoneE164": normalize_vn_phone(phone_raw, e164=True),
        }

    def wait_for_otp(
        self,
        order_id: str,
        timeout: int = 180,
        poll_interval: float = 4.0,
        auto_cancel_on_timeout: bool = True,
        stop_check: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """
        Chờ OTP SMS về cho `order_id`.
        Trả về dict chứa `code` (6 chữ số nếu có), `smsContent`, `status`, `orderId`, ...
        """
        deadline = time.time() + max(15, int(timeout))
        interval = max(2.0, float(poll_interval))

        while time.time() < deadline:
            if stop_check:
                stop_check()

            data = self.check_order(order_id, stop_check=stop_check)
            status = str(data.get("status", "")).strip().lower()
            otp_raw = data.get("otp")
            sms_content = data.get("smsContent")
            otp_code = extract_otp_code(otp_raw, sms_content)

            if status in ("cancelled", "expired"):
                raise HupSmsError(f"ORDER_{status.upper()}", f"Đơn HupSMS {order_id} đã {status}")

            if otp_code:
                return {
                    **data,
                    "code": otp_code,
                }

            if status == "success":
                if data.get("is_voice_otp") and data.get("audio_url") and not otp_code:
                    raise HupSmsError(
                        "VOICE_OTP_UNSUPPORTED",
                        f"Đơn {order_id} trả về Voice OTP ({data.get('audio_url')}) thay vì SMS text",
                    )

            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        if auto_cancel_on_timeout:
            self.cancel_order(order_id, ignore_errors=True)
        raise HupSmsError("OTP_TIMEOUT", f"Quá thời gian {timeout}s nhưng chưa nhận được SMS OTP (đơn {order_id})")
