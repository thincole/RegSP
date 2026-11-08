# Quy Trình Đăng Nhập Các Loại Tài Khoản Facebook & Hotmail

Tài liệu này lưu trữ toàn bộ các nhánh xử lý đã được tích hợp trực tiếp vào mã nguồn của phần mềm.

---

## 1. Phân Tích Dữ Liệu Cột B (`modules/data_parser.py`)
Hàm `parse_column_b()` tự động nhận diện linh hoạt mọi định dạng chuỗi `|` trong Cột B:
- **Facebook UID**: Phần tử đầu tiên.
- **Facebook Password**: Phần tử thứ 2.
- **Mã 2FA Secret (Base32)**: Tự động nhận diện chuỗi Base32 (`A-Z, 2-7`, độ dài 16–64 ký tự, ví dụ `BM7SGORC67NHNK2MAEH3GEDK3BYKAGAT`).
- **Facebook Cookie**: Tự động tìm phần tử chứa `c_user=`.
- **Hotmail / Outlook Email & Password**: Tự động tìm phần tử chứa `@hotmail` / `@outlook` và lấy mật khẩu ngay kế tiếp.
- **Email Khôi Phục (`recovery_email`)**: Tự động nhận diện mọi địa chỉ email khôi phục đi kèm trong chuỗi (hỗ trợ tất cả domain: `@fviainboxes.com`, `@fviadropinbox.com`, `@fviamail.work`, `@dropinboxes.com`, `@nguyenmaucole.io.vn`, ...).

---

## 2. Quy Trình Đăng Nhập Facebook (`modules/facebook_login.py`)
Hàm `login_facebook()` hỗ trợ đầy đủ các loại tài khoản Facebook:

1. **Tài khoản đã đăng nhập sẵn trong Profile**:
   - Kiểm tra cookie `c_user` còn hiệu lực -> tự động xử lý thông báo nếu có rồi chuyển thẳng qua Bước 2.
2. **Cách 1 — Đăng nhập bằng Cookie**:
   - Inject toàn bộ cookie từ Cột B vào domain `.facebook.com` và reload trang.
3. **Cách 2 — Đăng nhập bằng UID + Password + 2FA**:
   - Điền UID + Password và bấm `Log in`.
   - **Nếu hiện màn hình chờ phê duyệt thiết bị (`"Check your notifications on another device"` / `"Waiting for approval"`, `_switch_to_auth_app_if_needed`)**:
     - Tự động bấm nút **`Try another way`** (`Thử cách khác`).
     - Trong popup `"Choose a way to confirm it's you"`, tích chọn **`Authentication app`** (`Ứng dụng xác thực`) rồi bấm **`Continue`** (`Tiếp tục`) để chuyển sang màn hình nhập mã 6 số.
   - **Nếu tài khoản yêu cầu mã 2FA** (`two_step_verification`, `checkpoint`, `approvals_code`):
     - Lấy mã 6 số từ `https://2fa.live/tok/<2FA_SECRET>` (kết hợp tính TOTP dự phòng và mở tab `2fa.live` nếu cần).
     - Điền mã 6 số vào ô xác thực và bấm `Continue` / `Submit`.
4. **Chuỗi Popup / Thông Báo Sau Đăng Nhập (`_handle_post_2fa_prompts` & `_dismiss_automated_behavior_warning`)**:
   - **Popup `"You're back on Facebook"`**: Bấm nút đóng `X` (hoặc `Back to Facebook`).
   - **Màn hình `"Trust this device?"`**: Bấm `"Trust this device"` (hoặc `"Save browser"` / `"Continue"`).
   - **Popup `"Remember Password"`**: Bấm `"OK"`.
   - **Cảnh báo `"Chúng tôi nghi là tài khoản của bạn có hành vi tự động"` (`checkpoint/601051028565049`)**: Tự động bấm `"Bỏ qua"` (`Dismiss`).

---

## 3. Quy Trình Đăng Nhập, Đổi Pass & Forwarding Hotmail (`modules/hotmail_login.py`, `modules/change_password.py`)

### A. Các Loại Màn Hình Xác Thực Khi Login & Đổi Mật Khẩu
Hệ thống lưu và xử lý tự động tất cả các dạng màn hình của Microsoft:

1. **Nhập mật khẩu trực tiếp (`Enter your password`)**:
   - Điền mật khẩu gốc ở Cột B trước -> nếu báo sai (do đã đổi từ lần chạy trước) thì tự động thử mật khẩu mới `Khoa01@@c`.
   - Nếu cả 2 mật khẩu đều không khớp và có nút `"Send a code to ..."`, tự động bấm `"Send a code to ..."` để xác minh qua email khôi phục.
2. **Màn hình `"Verify your email"` có nút `"Use your password"`**:
   - Bấm `"Use your password"`, ưu tiên điền mật khẩu hiện tại trước, nếu sai tự động thử mật khẩu dự phòng (`Khoa01@@c`).
3. **Màn hình `"Help us protect your account"` (`account.live.com/identity/confirm`)**:
   - Tự động tích chọn radio button `Email *****@...` (`#iProof0`).
   - Điền phần ẩn (prefix) của email khôi phục vào ô `#iProofEmail` (vì hậu tố `@domain` đã hiển thị sẵn bên cạnh) và bấm `Send code`.
   - Lấy mã từ hòm thư khôi phục, điền vào `#iOttText` và bấm `Next`.
4. **Màn hình `"Almost there"` (`Just one more step to verify it's you.`) / `"Verify your identity"`**:
   - Tự động bấm vào ô chọn **`Send a code to *****@...`**.
   - Nếu Microsoft yêu cầu nhập lại địa chỉ email khôi phục đầy đủ (`#proof-confirmation-email-input`), tự động điền đầy đủ `prefix@domain` và bấm `Send code`.
   - Lấy mã 6 số và điền vào 6 ô `codeEntry-0..5` (hoặc ô đơn `#iOttText`).
5. **Màn hình `"Help protect your account"` (Chưa có mail khôi phục -> `"Add an email"`)**:
   - Bấm `Add email` -> điền `<prefix>@nguyenmaucole.io.vn` -> lấy code 6 số từ `mail.nguyenmaucole.io.vn` -> xác nhận.
6. **Các màn hình trung gian sau xác thực (`handle_microsoft_prompts`)**:
   - **`Let's protect your account` (`proofs/Add`)**: Bấm `Skip for now` (`#iShowSkip`).
   - **`We're updating our terms` (`tou/accrue`)**: Bấm `Next`.
   - **`Setting up your passkey...` (`fido/create`)**: Bấm `Cancel`.
   - **`A quick note about your Microsoft account` (`privacynotice`)**: Bấm `OK`.
   - **`Stay signed in?`**: Bấm `Yes`.

### B. Hệ Thống Lấy Mã Từ Email Khôi Phục
- **Hệ thống `fviainboxes.com`** (hỗ trợ đầy đủ 4 domain: `@fviainboxes.com`, `@fviadropinbox.com`, `@fviamail.work`, `@dropinboxes.com`):
  - Ưu tiên gọi API trực tiếp theo đúng `username` và `domain`.
  - Nếu cần mở trình duyệt (`https://fviainboxes.com/`), tự động điền `username`, chọn đúng `domain` trong dropdown bên cạnh và bấm `Get Email` để đọc mã 6 số.
- **Hệ thống `mail.nguyenmaucole.io.vn`**:
  - Kết hợp gọi API trực tiếp và fallback mở tab web `https://mail.nguyenmaucole.io.vn/`.

### C. Đổi Mật Khẩu Hotmail (`change_hotmail_password`) & Cài Đặt Forwarding (`setup_hotmail_forwarding`)
- **Thứ tự thực hiện**: **Login Hotmail + Đổi mật khẩu Hotmail sang `Khoa01@@c` (Bước 2)** -> **Thêm mail Forwarding về `@nguyenmaucole.io.vn` (Bước 3)**.
- Truy cập `https://outlook.live.com/mail/options/mail/forwarding`, tự động đóng các popup giới thiệu lần đầu của Outlook và bấm **`Accept`** nếu xuất hiện thông báo quyền riêng tư/cookie (`"We and various third parties process data..."`).
- Nếu Outlook yêu cầu bấm **`Sign in`** để xác thực lại trong popup OAuth:
  - Điền mật khẩu Hotmail (`Khoa01@@c`, hoặc mật khẩu gốc nếu chưa đổi).
  - Nếu sau khi điền mật khẩu (hoặc ngay khi mở popup) Microsoft hiện màn hình **`Almost there`** (*"Just one more step to verify it's you."*) -> tự động bấm **`Send a code to *****@...`**, lấy mã 6 số từ email khôi phục (`fviainboxes.com` / `mail.nguyenmaucole.io.vn`), điền vào 6 ô và chờ popup tự đóng.
- Bật `Enable forwarding`, điền `<prefix>@nguyenmaucole.io.vn`, tích `Keep a copy of forwarded messages`, bấm `Save` và ghi vào **Cột H**.

---

## 4. Quy Trình Reg Shopee & Affiliate (`modules/shopee_login.py`, `main.py`, `config.py`)

### A. Quy Trình Chung Cho Cả 3 Quốc Gia (`Shopee.PH`, `Shopee MY`, `Shopee ID`)
Quy trình đăng ký và liên kết Affiliate hoàn toàn giống nhau cho Bước 4 (`Shopee.PH`), Bước 5 (`Shopee MY`), và Bước 6 (`Shopee ID`), chỉ khác URL đích và cột lưu kết quả `(Username, Affiliate ID)` trên Google Sheet:
- **Bước 4 — Reg Shopee.PH (`https://affiliate.shopee.ph`)**: Lưu Username vào **Cột F**, Affiliate ID vào **Cột G**.
- **Bước 5 — Reg Shopee MY (`https://affiliate.shopee.com.my`)**: Lưu Username vào **Cột I**, Affiliate ID vào **Cột J**.
- **Bước 6 — Reg Shopee ID (`https://affiliate.shopee.co.id`)**: Lưu Username vào **Cột K**, Affiliate ID vào **Cột L**.
- **Nếu gặp màn hình `"Mobile Phone Verification"` (`signup-bind-phone`) khi đăng nhập Shopee bằng Facebook**: Tự động bỏ qua luồng và ghi **`Lỗi đăng ký SP Mobile`** vào **Cột D**.

### B. Xử Lý Popup Facebook OAuth (Hỗ Trợ Cả Tiếng Việt & Tiếng Anh — `_click_fb_oauth_steps`)
Khi đăng nhập Shopee bằng Facebook hoặc khi bấm `+ Add` Facebook trên trang Shopee Affiliate, popup Facebook OAuth tự động nhận diện và bấm tuần tự 4 màn hình (cả giao diện Tiếng Việt lẫn Tiếng Anh):
1. **Màn hình 1**: Bấm **`Tiếp tục dưới tên [Tên]`** / **`Continue as [Name]`**.
2. **Màn hình 2 (`"Bạn muốn sử dụng Trang nào cùng Shopee?"`)**: Bấm **`Tiếp`** / **`Next`**.
3. **Màn hình 3 (`"Shopee được phép làm gì?"`)**: Bấm **`Xong`** / **`Done`** / **`Save`** / **`Lưu`**.
4. **Màn hình 4 (`"Bạn hiện đã liên kết Shopee với Facebook"`)**: Bấm **`OK`**.

### C. Bước 7 — Đổi Mật Khẩu Mới Shopee (`change_shopee_password`)
- Truy cập `/user/account/password` trên domain Shopee tương ứng.
- Bấm **`Verify by Email Link`** -> lấy link xác nhận từ `<prefix>@nguyenmaucole.io.vn` -> mở link xác nhận -> điền mật khẩu mới Shopee vào 2 ô và bấm **`Confirm`**.
