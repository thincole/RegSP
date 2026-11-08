# Tài Liệu Kỹ Thuật Hệ Thống Tự Động Hóa RegSP (`RegSP.md`)

Tài liệu này lưu trữ toàn bộ quy trình nghiệp vụ, kiến trúc hệ thống, các thuật toán xử lý quan trọng và các cơ chế vượt tường lửa/anti-bot của phần mềm tự động hóa đa tài khoản **Facebook – Hotmail – Shopee Affiliate**.

---

## 1. Kiến Trúc Hệ Thống & Cấu Trúc Thư Mục

```text
e:\0-regSP\
├── main.py                      # Điều phối luồng chạy chính (7 bước), quản lý đa luồng (ThreadPoolExecutor)
├── config.py                    # Cấu hình mặc định (Google Sheets, Gemlogin API, URLs, Mật khẩu, Timeout)
├── settings.json                # Lưu cấu hình GUI của người dùng giữa các lần mở app
├── gui/
│   └── app.py                   # Giao diện người dùng (Tkinter), chọn bước chạy (1..7), hiển thị tiến độ & log
├── modules/
│   ├── gemlogin_manager.py      # Giao tiếp Gemlogin Local API (find/start/stop profile, kết nối Selenium + CDP)
│   ├── data_parser.py           # Phân tích cú pháp dữ liệu cột B (FB, 2FA, Cookie, Hotmail...)
│   ├── google_sheets.py         # Đọc/ghi Google Sheets qua Service Account (gspread)
│   ├── facebook_login.py        # Bước 1: Đăng nhập Facebook (Cookie / UID+Pass+2FA), kiểm tra checkpoint
│   ├── hotmail_login.py         # Bước 2 & 3: Đăng nhập Hotmail, thêm mail khôi phục, cài đặt Forwarding
│   ├── change_password.py       # Đổi mật khẩu Hotmail (hỗ trợ cả form 2 ô và 3 ô)
│   └── shopee_login.py          # Bước 4/5/6 & 7: Login Shopee qua FB, đăng ký Affiliate, lấy ID, đổi pass Shopee
└── utils/
    ├── wait_helpers.py          # Các hàm đợi phần tử (wait_for_element, safe_click, safe_send_keys)
    ├── error_handler.py         # Các lớp Exception nghiệp vụ và decorator @retry
    └── logger.py                # Hệ thống ghi log ra file (logs/run_YYYYMMDD_HHMMSS.log) và đẩy lên GUI
```

---

## 2. Cấu Trúc Dữ Liệu Google Sheets

- **Định dạng Cột `B` (`COL_DATA`)**: Chuỗi phân tách bởi dấu `|` theo thứ tự cố định (0-based index):
  ```text
  FB_UID | FB_PASSWORD | FB_2FA_SECRET | FB_COOKIE | FB_TOKEN | HOTMAIL_EMAIL | HOTMAIL_PASSWORD | MS_TOKEN | UUID
    [0]  |     [1]     |      [2]      |    [3]    |   [4]    |      [5]      |       [6]        |   [7]    | [8]
  ```
- **Ánh xạ các Cột trên Sheet**:
  | Cột | Biến cấu hình | Nội dung |
  | :---: | :--- | :--- |
  | `B` | `COL_DATA` | Chuỗi dữ liệu đầu vào (`FB_UID\|FB_PASS\|2FA\|COOKIE\|...\|HOTMAIL\|PASS...`) |
  | `D` | `COL_STATUS` | Ghi trạng thái lỗi nếu có (`"FB die"`, `"reg Shopee lỗi"`) |
  | `E` | `COL_PROFILE` | Tên Profile trên Gemlogin (VD: `PH3084`, `PH3085`) |
  | `F` | `COL_USERNAME` | Ghi **Username Shopee Affiliate** sau khi đăng ký thành công |
  | `G` | `COL_AFFILIATE_ID` | Ghi **Affiliate ID Shopee** sau khi đăng ký thành công |
  | `H` | `COL_FORWARD_EMAIL` | Ghi địa chỉ **Email Forwarding** (`<prefix>@nguyenmaucole.io.vn`) sau Bước 3 |

---

## 3. Quy Trình 7 Bước Tự Động Hóa Chi Tiết

### Bước 1: Đăng nhập Facebook (`modules/facebook_login.py`)
1. Mở `https://www.facebook.com/`, kiểm tra nhanh cookie `c_user` xem profile đã đăng nhập sẵn từ trước chưa.
2. **Ưu tiên 1 — Đăng nhập bằng Cookie**:
   - Hỗ trợ tự động nhận diện 3 định dạng Cookie: JSON Array, Netscape, hoặc chuỗi `key=value; key2=value2`.
   - Nạp từng cookie qua `driver.add_cookie()`, tải lại trang và kiểm tra `c_user`.
3. **Ưu tiên 2 — Đăng nhập bằng UID + Password + 2FA**:
   - Điền `FB_UID` và `FB_PASSWORD`, bấm nút Login.
   - Nếu xuất hiện trang `two_step_verification` / `checkpoint`, sinh mã OTP 6 số thời gian thực từ `FB_2FA_SECRET` bằng `pyotp.TOTP(secret).now()`.
   - Tự động bấm qua các màn hình `"Trust this device"` / `"Save browser"` / `"Continue"`.
4. **Kiểm tra tài khoản chết (`FB die`)**:
   - Nếu URL rơi vào trạng thái khóa (`checkpoint/1501092823525282`, `checkpoint/828281030927956`, `account_disabled`...) mà không có ô nhập mã 2FA, đánh dấu `FBDieError` -> ghi `"FB die"` vào cột `D` và dừng xử lý dòng đó.

---

### Bước 2: Đăng nhập Hotmail & Đổi mật khẩu (`modules/hotmail_login.py`, `modules/change_password.py`)
1. Truy cập trực tiếp `https://login.live.com/`.
2. Kiểm tra nếu đã đăng nhập sẵn đúng tài khoản hoặc khác tài khoản (nếu lệch email thì tự động đăng xuất qua `https://login.live.com/logout.srf`).
3. Nhập `HOTMAIL_EMAIL` -> `Next` -> xử lý trường hợp tài khoản hiện danh sách phương thức xác minh (chuyển sang `"Use your password"`).
4. Nhập `HOTMAIL_PASSWORD` (nếu mật khẩu cũ trong Sheet báo sai, tự động thử mật khẩu mới `NEW_HOTMAIL_PASSWORD = "Khoa01@@c"`).
5. **Chuỗi xử lý các màn hình bảo mật Microsoft (`handle_microsoft_prompts`)**:
   - **Bắt buộc thêm Email khôi phục (`proofs/Add`)**: Tự động tạo địa chỉ `<prefix>@nguyenmaucole.io.vn` (lấy phần trước `@` của Hotmail), điền vào ô email khôi phục, bấm `Send code`, lấy mã xác nhận 6 số qua API `https://mail.nguyenmaucole.io.vn/api/emails?address=...` và điền vào 6 ô `#codeEntry-0..5`.
   - **Xác minh qua Email khôi phục đã có**: Chọn phương thức email `@nguyenmaucole.io.vn`, điền lại địa chỉ đầy đủ, lấy mã OTP 6 số từ TempMail và xác nhận.
   - **Màn hình `"Skip for now"` / `"Looks good!"` (`proofs/Remind`)**: Tự động bấm bỏ qua.
   - **Màn hình `"Setting up your passkey"` (`interrupt/passkey`)**: Tự động bấm `Cancel` / `Back` để không bị kẹt hộp thoại Windows Hello.
   - **Màn hình `"Stay signed in?"`**: Bấm `Yes`.
6. **Đổi mật khẩu Hotmail (`change_hotmail_password`)**:
   - Nếu Bước 3 (Forwarding) cũng được chọn, hệ thống sẽ cài Forwarding trước rồi mới đổi mật khẩu Hotmail để tận dụng phiên xác thực bảo mật.
   - Mở `https://account.live.com/password/Change`, tự động xử lý xác minh lại nếu được hỏi, nhận diện form 3 ô (có Old Password) hoặc form 2 ô (chỉ có New Password + Confirm Password) và đổi sang `Khoa01@@c`.

---

### Bước 3: Cài đặt Forwarding Mail về `@nguyenmaucole.io.vn` (`modules/hotmail_login.py`)
1. Truy cập `https://outlook.live.com/mail/options/mail/forwarding` và đóng các popup giới thiệu lần đầu (`_dismiss_outlook_popups`).
2. **Xử lý nút `Sign in` xác thực lại trong khung Forwarding (`_handle_forwarding_signin`)**:
   - Nếu khung `Forwarding and IMAP` hiện nút **`Sign in`** (`Sign in and verify your account to forward your email...`), tự động click **`Sign in`**.
   - Chuyển sang cửa sổ popup Microsoft OAuth (`login.live.com/oauth20_authorize.srf...`).
   - Nếu hiện màn hình `Verify your email` có nút **`Use your password`**, tự động click **`Use your password`**.
   - Điền mật khẩu **`"Khoa01@@c"`** (hoặc mật khẩu gốc nếu tài khoản chưa đổi pass) và bấm **`Sign in`**, chờ popup tự đóng và quay lại tab cài đặt Forwarding.
3. Tích bật **`Enable forwarding`**, điền `<prefix>@nguyenmaucole.io.vn` vào ô `Forward my email to`, tích **`Keep a copy of forwarded messages`**, và bấm **`Save`**.
4. Khi lưu thành công, trả về chuỗi `<prefix>@nguyenmaucole.io.vn` để `main.py` ghi trực tiếp vào **Cột `H`** trên Google Sheet.

---

### Bước 4 / 5 / 6: Đăng ký Shopee & Shopee Affiliate (`modules/shopee_login.py`)
1. **Kiểm tra nhanh trạng thái trên trang Shopee (`login_shopee_via_facebook`)**:
   - Ngay sau khi gọi `driver.get(shopee_url)`, quét nhanh mỗi `0.5s` (tối đa `6s`):
     - Nếu phát hiện đã đăng nhập sẵn (có `.navbar__username` ở góc phải hoặc cookie `SPC_U` + `SPC_ST`): ghi log xác nhận và **nhảy thẳng sang trang `https://affiliate.shopee.ph/`**.
     - Nếu phát hiện bị đẩy sang `/verify/captcha` hoặc `"Please Try Again Later"`: **nhảy thẳng sang trang `https://affiliate.shopee.ph/`**.
2. **Đăng nhập Shopee qua Facebook OAuth**:
   - Bấm nút `Facebook` trên trang `buyer/login`, chuyển sang cửa sổ popup Facebook, tự động điền tài khoản FB (nếu được hỏi) và bấm `Continue as ...` -> chờ popup tự đóng.
3. **Đăng ký Shopee Affiliate (`_register_affiliate`)**:
   - Mở `https://affiliate.shopee.ph/`:
     - Nếu gặp trang chào mừng có nút **`Enter`** (`a.home-btn`), tự động click `Enter`.
     - Nếu bị chuyển về `buyer/login?next=...affiliate...`, thực hiện login Facebook ngay tại đó để vào thẳng trang Affiliate.
     - Nếu tài khoản đã đăng ký Affiliate từ trước (vào thẳng `/dashboard`, `/offer`, hoặc hiện `"Registration Submitted Successfully!"`), chuyển thẳng qua bước lấy `Username` + `Affiliate ID`.
   - **Step 1 — Media Platform (Facebook)**:
     - Click nút **`+ Add`** cạnh mục Facebook.
     - Xử lý popup Facebook OAuth (`Continue as ...` -> `Next` -> `Done`/`Save`).
     - Khi modal `Add Facebook` hiện ra trên trang Affiliate, điền link `https://www.facebook.com/profile.php?id={fb_uid}` vào `textarea` và bấm `Confirm`.
   - **Step 2 — Thông tin liên hệ & Xác minh Email**:
     - Tạo số điện thoại ngẫu nhiên đúng định dạng vùng (VD: Philippines `9xxxxxxxxx` 10 chữ số) và điền vào ô Phone Number.
     - Điền `hotmail_email` vào ô Email (nếu chưa có sẵn).
     - Bấm **`Verify & Submit`** -> trên popup xác minh chọn **`Verify with Email`** -> bấm **`Confirm`**.
     - Khi chuyển sang trang `shopee.ph/verify/email`, lấy mã OTP 6 số gửi về `<prefix>@nguyenmaucole.io.vn`, điền vào ô OTP và bấm **`NEXT`**.
   - **Step 3 — Xác nhận hoàn tất & Lấy thông tin (`_get_affiliate_account_setting`)**:
     - Chờ trang quay về Affiliate và hiện thông báo **`"Registration Submitted Successfully!"`** (hoặc vào `/dashboard`).
     - Truy cập `https://affiliate.shopee.ph/account_setting`, trích xuất **Name (`Username`)** và **Affiliate ID**, ghi vào **Cột `F`** và **Cột `G`** trên Google Sheet.

---

### Bước 7: Đổi mật khẩu Shopee (`change_shopee_password` trong `modules/shopee_login.py`)
1. Kích hoạt bộ lọc chặn request nền gây Captcha (`_enable_shopee_antibot_bypass`).
2. Truy cập `https://shopee.ph/user/account/password`.
3. Xử lý màn hình `Security check` (`/verify/ivs`):
   - Chờ và bấm nút **`Verify by Email Link`**.
   - Nếu chỉ hiện `Verify by Password` (không có `Verify by Email Link`), ghi nhận tài khoản đã được đặt mật khẩu từ trước.
4. Gọi `_fetch_shopee_email_link()` quét hòm thư `<prefix>@nguyenmaucole.io.vn` lấy link xác nhận dạng `https://ph.shp.ee/dlink/...`.
5. Mở tab mới, kích hoạt `_enable_shopee_antibot_bypass(driver)`, truy cập link `dlink` để hệ thống Shopee xác nhận **`Attempt approved`**, sau đó đóng tab phụ và quay về tab chính.
6. Chờ tab chính tự động chuyển hướng từ `/verify/email-link` về `/user/account/password` (giữ nguyên phiên `ivs`).
7. Điền mật khẩu mới (`NEW_SHOPEE_PASSWORD = "Khoa01@c"`) vào 2 ô `input[name='newPassword']` và `input[name='newPasswordRepeat']`, sau đó bấm **`Confirm`**.

---

## 4. Các Thuật Toán & Cơ Chế Quan Trọng

### 4.1. Cơ chế Chặn Captcha Trắng (`Please Try Again Later` / Mã lỗi `90309999`) trên Shopee

#### Nguyên nhân kỹ thuật
- Khi mở các trang `shopee.ph` (`/buyer/login`, `/user/account/password`...), các API tài khoản chính (`/api/v4/account/*`, `/api/v4/pages/get_pc_me_page`) **không bị chặn** (luôn trả về `HTTP 200`, `error: 0`).
- Tuy nhiên, thanh điều hướng (Header Navbar) và trang chủ (`https://shopee.ph/?is_from_signup=true` ngay sau khi login bằng Facebook) luôn tự động gọi ngầm các API:
  - `GET /api/v4/flash_sale/*` (`app_key: "FlashSale.PC"`, `scene: "crawler_item"`)
  - `GET /api/v4/search/search_prefills` (`business: "Search"`)
  - `GET /api/v4/search/search_suggestion`
  - `POST /api/v4/cart/mini` (`app_key: "Cart.PC"`, `scene: "crawler_item"`)
- Các endpoint FlashSale/Search/Cart này có bộ lọc chống cào dữ liệu rất gắt và trả về JSON `{"error": 90309999}`. Ngay khi một request nền bất kỳ trả về `90309999`, hàm `fetch` interceptor toàn cục của Shopee sẽ cưỡng bức chuyển hướng cả trình duyệt sang `/verify/traffic` -> `/verify/captcha?anti_bot_tracking_id=...` (`Please Try Again Later`).
- Ngoài ra, trình duyệt Chromium 140 của Gemlogin đã giả lập sẵn `navigator.webdriver = false` ở tầng C++ (`Navigator.prototype`). Tuyệt đối **không** dùng `Object.defineProperty(navigator, 'webdriver', {get: () => undefined})` vì sẽ làm `navigator.hasOwnProperty('webdriver')` trở thành `true` và bị `@shopee/sap` (`af-ac-enc-dat`) từ chối tại `/api/v4/anti_fraud/captcha/generate`.

#### Code thực thi (`_enable_shopee_antibot_bypass`)
```python
def _enable_shopee_antibot_bypass(driver: WebDriver) -> None:
    """
    Chặn các request nền không cần thiết trên thanh điều hướng & trang chủ Shopee
    (flash_sale, search_prefills, search_suggestion, cart/mini, recommend, homepage, banner)
    vốn bị Shopee trả về mã lỗi 90309999 (app_key=FlashSale.PC / Cart.PC / Search, scene=crawler_item)
    và tự động ép chuyển hướng toàn trang sang /verify/traffic -> /verify/captcha.
    """
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*api/v4/flash_sale/*",
                    "*api/v2/flash_sale/*",
                    "*api/v4/search/search_prefills*",
                    "*api/v4/search/search_suggestion*",
                    "*api/v4/search/*",
                    "*api/v4/cart/mini*",
                    "*api/v4/pages/get_popular_search*",
                    "*api/v4/pages/get_category_tree*",
                    "*api/v4/recommend/*",
                    "*api/v4/homepage/*",
                    "*api/v4/banner/*",
                ]
            },
        )
    except Exception:
        pass
```

> **Lưu ý về CDP**: Lệnh `Network.setBlockedURLs` có hiệu lực theo từng tab (Target). Do đó khi mở tab mới bằng `driver.switch_to.new_window("tab")` (ví dụ khi mở link xác nhận email `dlink`), cần gọi lại `_enable_shopee_antibot_bypass(driver)` trên tab mới đó trước khi `driver.get()`.

---

### 4.2. Thuật toán Lấy Mã OTP & Link Xác Nhận từ TempMail (`nguyenmaucole.io.vn`)

Để tránh lấy nhầm email OTP hoặc link xác nhận cũ từ các lần chạy trước, hệ thống sử dụng thuật toán **Snapshot ID (`seen_ids`)**:

1. **Bước 1 — Chụp danh sách ID email hiện có TRƯỚC khi bấm nút gửi mã/link**:
   ```python
   def _get_existing_tempmail_ids(email_address: str) -> set:
       addr = email_address.strip().lower()
       api_url = f"https://mail.nguyenmaucole.io.vn/api/emails?address={urllib.parse.quote(addr)}"
       req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
       with urllib.request.urlopen(req, timeout=10) as resp:
           data = json.loads(resp.read().decode("utf-8"))
       emails = data.get("emails") or []
       return {e.get("id") for e in emails if e.get("id")}
   ```
2. **Bước 2 — Bấm nút gửi mã (`Verify with Email` / `Verify by Email Link` / `Send code`)**.
3. **Bước 3 — Quét API định kỳ mỗi 3 giây, chỉ xử lý các email có `id not in seen_ids`**:
   - **Với OTP 6 số Shopee (`_fetch_shopee_email_otp`)**: Lọc email từ `shopee` và khớp regex `\b(\d{6})\b` trên `subject` + `text_body` + `html_body`.
   - **Với Link xác nhận đổi mật khẩu Shopee (`_fetch_shopee_email_link`)**: Lọc email từ `shopee`, ưu tiên trích xuất link rút gọn `https://[a-z0-9.-]*shp\.ee/dlink/[^\s"'<>]+` hoặc link `/verify/email-link`.

---

### 4.3. Quản Lý Vòng Đời Trình Duyệt Gemlogin & Tiến Trình Driver

1. **Tự động dọn tab thừa khi khởi động (`connect_selenium`)**:
   - Khi kết nối vào `remote_debugging_address`, giữ lại duy nhất tab đầu tiên (`handles[0]`) và đóng toàn bộ các tab tồn đọng từ phiên trước để tránh rò rỉ bộ nhớ và nhầm lẫn `window_handles`.
2. **Đóng sạch tiến trình `gemlogindriver.exe` và Profile (`main.py`)**:
   - Không gọi `driver.quit()` trực tiếp để đóng trình duyệt mà giải phóng tiến trình `gemlogindriver.exe` qua `driver.service.process.kill()`, sau đó gọi API `stop_profile(profile_id)` (`GET http://127.0.0.1:1010/api/profiles/close/{id}`) để Gemlogin lưu dữ liệu profile (Cookies, LocalStorage) an toàn.
3. **Loại bỏ Extension mặc định của Gemlogin**:
   - Danh sách extension tự động nạp vào mọi profile nằm tại `C:\Users\thinc\.gemlogin\extensions\extension.json`. Đảm bảo file này là mảng rỗng `[]` để trình duyệt không bị gắn cờ `--load-extension=...` khi khởi chạy.

---

### 4.4. Nguyên Tắc Chống Lặp Captcha Facebook (`pre_authentication`) & Bảo Toàn Nhân Gemlogin

1. **Giữ nguyên bản gốc `gemlogindriver.exe` (Không vá chuỗi `cdc_`)**:
   - Nhân Chromium tùy chỉnh của Gemlogin (`chrome.exe`) đã tích hợp sẵn bộ lọc C++ ẩn các thuộc tính `cdc_*` của ChromeDriver trên `window` và `document`. Nếu sửa nhị phân `cdc_` thành chuỗi khác (như `xkx_`), bộ lọc C++ của Gemlogin sẽ không nhận diện được và làm lộ biến `$xkx_...` ra DOM mỗi khi Selenium thực thi JS Atom, khiến Facebook yêu cầu giải Captcha lặp lại ngay sau khi vừa giải xong.
2. **Không chọc DOM (`find_element` / `execute_script`) khi người dùng đang giải Captcha Facebook**:
   - Trong `_wait_for_fb_captcha`, khi URL đang ở trang xác thực (`pre_authentication`, `two_step_verification`, `captcha`, `checkpoint`, `security`), chỉ kiểm tra `driver.current_url` và `driver.get_cookies()` (giao thức HTTP/Browser thuần), tuyệt đối không gọi `driver.find_element(By.TAG_NAME, "body").text` mỗi 5 giây để tránh kích hoạt JS Atom của ChromeDriver trong lúc Arkose/reCAPTCHA đang chấm điểm phiên.
3. **Chỉ bật CDP `Network.enable` ở Bước 7 (`change_shopee_password`)**:
   - Không gọi `Network.enable` ngay khi khởi tạo `connect_selenium` (trước khi qua bước Login Facebook), mà chỉ bật cục bộ ở bước Đổi mật khẩu Shopee.
