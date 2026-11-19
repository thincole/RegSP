# OMOCaptcha — Giải captcha Shopee tự động

Tích hợp OMOCaptcha API v2 để tự động giải captcha kéo-thả (slider) của Shopee.
Captcha xử lý được: `shopee.*/verify/captcha`, `verify/traffic` (dạng có ô kéo),
và captcha slider xuất hiện khi vào Affiliate.

Không giải được (DOM lạ / hết tiền / mất mạng) → tự rơi về chế độ **giải tay** như cũ,
không làm chết luồng đăng nhập.

---

## 1. Thành phần

| File | Vai trò |
|---|---|
| `modules/omocaptcha_api.py` | Client API v2: `createTask`, `getTaskResult`, `getServicePrice`, `getBalance`; xử lý `errorId`/`errorCode`, retry lỗi tạm thời |
| `modules/shopee_captcha_solver.py` | Nhận diện vùng captcha, chụp ảnh mảnh ghép + ảnh nền, gọi API, kéo slider bằng CDP |
| `modules/shopee_login.py` | `_wait_for_captcha()` gọi `_try_auto_solve_captcha()` trước khi chờ giải tay |
| `config.py` | Khối `OMO_*` |
| `gui/app.py` | Ô `OMOCaptcha API Key` + checkbox `Tự động giải Captcha Shopee` |
| `main.py` | `process_all_rows()` nạp key từ GUI vào `omocaptcha_api.configure()` cho toàn tiến trình |

---

## 2. Luồng chạy

```
_wait_for_captcha()
 ├─ phát hiện captcha (URL / text / DOM như cũ)
 ├─ hard-block "Please Try Again Later" -> trả False luôn (không đốt lượt)
 ├─ _try_auto_solve_captcha()                      [OMO_ENABLED + có key]
 │    ├─ quét DOM: tìm ảnh nền (>=150x60) + mảnh ghép (24..170px, gần vuông)
 │    ├─ chụp riêng từng lớp (CDP Page.captureScreenshot, ẩn lớp còn lại)
 │    ├─ POST /v2/createTask  {type: ShopeeSliderWebTask, imageBase64s:[MASK, BACKGROUND]}
 │    ├─ POST /v2/getTaskResult (2s/lần) -> solution.end.{x,y} hoặc solution.point.{x,y}
 │    ├─ đổi toạ độ ảnh -> pixel CSS:  dx = (end.x - maskOffset) / scale
 │    └─ kéo bằng Input.dispatchMouseEvent (giảm tốc + overshoot nhẹ), tối đa OMO_DRAG_ATTEMPTS lượt
 └─ fallback: chờ giải tay (nếu OMO_FALLBACK_MANUAL=1)
```

Chi tiết kỹ thuật đáng lưu ý:

- **Chụp ảnh bằng CDP, không dùng `canvas.toDataURL`.** Ảnh captcha Shopee nằm trên CDN
  khác origin nên canvas bị *taint*, `toDataURL()` sẽ ném `SecurityError`.
  Trước khi chụp mỗi lớp, lớp còn lại được set `visibility:hidden` rồi khôi phục ngay,
  nên ảnh gửi lên là ảnh sạch, không dính pixel của lớp kia.
- **`imageBase64s` đúng thứ tự `[MASK, BACKGROUND]`** theo tài liệu; sai thứ tự job sẽ fail.
- **Toạ độ API là pixel của ảnh nền**, còn chuột cần pixel CSS trên trang.
  `scale = png_width / css_width` (tự đọc header PNG nên đúng cả khi máy để DPI ≠ 100%).
- Kéo bằng CDP nên không bị lệch như `ActionChains.move_by_offset`, và có quỹ đạo
  giảm tốc + overshoot ~1–4px cho giống người thật.

---

## 3. Cấu hình

### GUI
- `OMOCaptcha API Key`: dán key `OMO_...`
- `Tự động giải Captcha Shopee`: bật/tắt. Key và công tắc dùng chung cho cả tab Reg Clone và Reg Gmail,
  được lưu vào `settings.json` (`omo_api_key`, `omo_enabled`).

### `.env` / `config.py`
| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `OMO_API_KEY` | `""` | API key. Rỗng → tự động tắt auto-solve |
| `OMO_ENABLED` | `1` | Bật/tắt toàn bộ |
| `OMO_BASE_URL` | `https://api.omocaptcha.com/v2` | Base URL API |
| `OMO_REQUEST_TIMEOUT` | `30` | Timeout 1 request HTTP (giây) |
| `OMO_SOLVE_TIMEOUT` | `120` | Chờ solver trả kết quả (giây) |
| `OMO_POLL_INTERVAL` | `2` | Chu kỳ hỏi `getTaskResult` (giây) |
| `OMO_MAX_TASK_RETRIES` | `2` | Retry khi gặp lỗi tạm thời |
| `OMO_DRAG_ATTEMPTS` | `3` | Số lượt kéo cho 1 captcha (mỗi lượt là 1 task mới) |
| `OMO_FALLBACK_MANUAL` | `1` | `0` = không chờ giải tay khi auto-solve thất bại |
| `OMO_SHOPEE_ROTATE` | `0` | `1` = gửi `typeCaptcha:"rotate"` (captcha quỹ đạo tự do) |
| `OMO_DEBUG_IMAGES` | `1` | Lưu ảnh + DOM captcha để soi khi nhận diện sai |

---

## 4. Debug khi captcha không được nhận diện

Bật `OMO_DEBUG_IMAGES=1`, chạy tới lúc gặp captcha, rồi xem:

```
logs/captcha_debug/<thời gian>_shopee_1_dom.json      <- danh sách phần tử ảnh + toạ độ
logs/captcha_debug/<thời gian>_shopee_1_mask.png      <- ảnh mảnh ghép đã gửi
logs/captcha_debug/<thời gian>_shopee_1_background.png <- ảnh nền đã gửi
```

Cách đọc: mở `background.png`, nếu nó **không phải** ảnh nền captcha thì selector đang trỏ sai;
nếu `mask.png` là cả khung captcha thay vì mảnh ghép nhỏ thì Shopee đang gộp 1 canvas duy nhất.

Gọi thủ công khi cần soi nhanh:

```python
from modules.shopee_captcha_solver import dump_captcha_debug
print(dump_captcha_debug(driver, tag="manual"))
```

---

## 5. Mã lỗi thường gặp (theo bảng mã lỗi OMOCaptcha)

| `errorCode` | Nghĩa | Phần mềm xử lý |
|---|---|---|
| `ERROR_KEY_DOES_NOT_EXIST` | Key sai/rỗng | Dừng auto-solve, về giải tay |
| `ERROR_ZERO_BALANCE` / `ERROR_INSUFFICIENT_BALANCE` | Hết tiền | Dừng, log rõ |
| `ERROR_PACKAGE_EXPIRED` / `ERROR_PACKAGE_EXHAUSTED` | Gói hết hạn/hết lượt | Dừng |
| `ERROR_TASK_IS_MAINTENANCE` / `ERROR_SERVICE_UNAVAILABLE` / `ERROR_REDIS_UNAVAILABLE` | Tạm bận | Retry exponential backoff |
| `ERROR_JOB_STATUS` | Solver giải sai / timeout (tiền tự hoàn) | Tính lại và kéo lượt khác |
| `SOLVE_TIMEOUT` | Quá `OMO_SOLVE_TIMEOUT` | Rơi về giải tay |

Kiểm tra key + số dư bằng tay:

```python
import config
from modules.omocaptcha_api import OmocaptchaClient
c = OmocaptchaClient(config.OMO_API_KEY)
print(c.get_balance())                 # {'balance': ..., 'voucherBalance': ...}
print([s for s in c.get_services() if 'Shopee' in s['type']])
print(c.get_packages())
```

---

## 6. Giới hạn đã biết

- Captcha nằm trong **iframe** hoặc DOM đổi cấu trúc hoàn toàn: chỉ quét document chính
  → không nhận diện được, tự rơi về giải tay. Soi `*_dom.json` rồi bổ sung selector nếu cần.
- Captcha một canvas gộp (mảnh ghép + nền vẽ chung 1 canvas): chưa tách được mảnh ghép.
- Mỗi lượt kéo là 1 task tính tiền (ShopeeSliderWebTask ≈ 0.0006 USD/lượt).
  Job fail do solver thì OMOCaptcha tự hoàn tiền.
- `1 CAPTCHA_TIMEOUT` (mặc định 300s) vẫn là trần chờ giải tay; pha auto-solve có trần riêng
  `OMO_DRAG_ATTEMPTS × (OMO_SOLVE_TIMEOUT + 15s)`.
