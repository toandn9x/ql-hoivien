# Google Sheets Bot Quản Lý Thời Hạn Hội Viên

Bot Python chạy nền, dùng Google Sheets làm nơi quản lý hội viên. Bot tự điền ngày vào nhóm, ngày hết hạn, số ngày còn lại, trạng thái tự động, tô màu trạng thái và gửi cảnh báo qua Email, Telegram, Discord khi hội viên sắp hết hạn.

## Cài đặt

### 1. Cài Python

- Tải Python 3.12 hoặc mới hơn từ https://www.python.org/downloads/
- Khi cài trên Windows, nhớ tick `Add Python to PATH`
- Kiểm tra lại:

```powershell
python --version
pip --version
```

### 2. Tạo môi trường ảo và cài thư viện

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

File `requirements.txt` chứa toàn bộ thư viện cần cho bot:

- `fastapi`
- `gspread`
- `google-auth`
- `python-dotenv`
- `requests`
- `uvicorn`
- `pytest`

## Chuẩn bị Google Sheet

1. Tạo Google Cloud service account và tải file JSON.
2. Lưu file JSON vào thư mục dự án, ví dụ `credentials.json`.
3. Điền `GOOGLE_SPREADSHEET_NAME` trong `.env`, ví dụ `ql-hoivien`.
4. Bot sẽ tự mở Google Sheet theo tên đó. Nếu chưa có, bot tự tạo file mới trong Drive của service account.

Bạn có thể dùng lại file `credentials.json` từ dự án khác. Chỉ cần dùng một tên spreadsheet khác trong `GOOGLE_SPREADSHEET_NAME` để tách dữ liệu. Nếu bạn tự tạo Google Sheet bằng tài khoản cá nhân, hãy share sheet đó cho email service account với quyền Editor.

Bot sẽ tự tạo hoặc cập nhật worksheet/tab tên `HoiVien` với các cột:

- `STT`
- `Họ tên`
- `Nick/Link Facebook`
- `SĐT`
- `Nguồn biết đến`
- `Ngày chuyển khoản`
- `Tên CK/Mã GD`
- `Số tiền`
- `Gói đăng ký`
- `Ngày vào nhóm`
- `Ngày hết hạn`
- `Số ngày còn lại`
- `Trạng thái tự động`
- `Đã nhắc gia hạn?`
- `Telegram ID`
- `Ghi chú`
- `Mã hội viên`

Bot dùng `Mã hội viên` làm khóa duy nhất khi gia hạn/hủy. Các dòng chưa có mã sẽ được tự sinh mã khi bot sync sheet.

## Chạy bot

```powershell
python bot.py
```

Trang admin trực quan:

```text
http://localhost:8687/
```

- Dashboard hiển thị danh sách hội viên theo hạn tăng dần, mỗi trang 100 dòng.
- Đổi trang bằng `?page=2`, `?page=3`, ...

Health check JSON:

```text
http://localhost:8687/health
```

## Cấu hình cảnh báo

- Email: điền `SMTP_USER` và `SMTP_PASSWORD`. Với Gmail, dùng App Password.
- Email nhận cảnh báo chung: điền `ALERT_EMAIL_TO`, có thể nhập nhiều email bằng dấu phẩy.
- Ngưỡng cảnh báo theo ngày: chỉnh `ALERT_BEFORE_DAYS`, mặc định `3`.
- Giờ gửi cảnh báo: chỉnh `ALERT_RUN_TIME` dạng `HH:MM`, ví dụ `09:00`. Để trống nếu muốn gửi ngay khi bot phát hiện hội viên sắp hết hạn.
- Telegram: tạo bot bằng BotFather, điền `TELEGRAM_BOT_TOKEN`. Khi nhập từ Telegram, bot tự lưu `Telegram ID` riêng để dễ kiểm tra.
- Discord: điền `DISCORD_WEBHOOK_URL` trong `.env`, hoặc ghi `discord:<webhook_url>` trong cột `Ghi chú`.
- Email: ghi `email:<địa chỉ email>` trong cột `Ghi chú` nếu muốn gửi email cho một hội viên cụ thể.

Bot chỉ gửi cảnh báo một lần cho mỗi kỳ hạn. Khi bạn đổi `Gói đăng ký` hoặc `Ngày vào nhóm`, bot tính lại hạn và cho phép cảnh báo kỳ mới.

## Nhập hội viên từ Telegram

Bot dùng Telegram polling nên vẫn chỉ chạy một port duy nhất cho health check. Sau khi điền `TELEGRAM_BOT_TOKEN`, nhắn bot:

```text
/start
```

Nếu muốn chỉ cho một vài chat được nhập dữ liệu, điền chat ID vào `.env`:

```env
TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321
```

Nếu muốn gửi digest danh sách hội viên sắp hết hạn vào group Telegram hằng ngày, thêm bot vào group rồi gửi `/id` trong group để lấy chat ID. Sau đó cấu hình:

```env
TELEGRAM_DIGEST_CHAT_IDS=-1001234567890
TELEGRAM_DIGEST_RUN_TIME=09:00
TELEGRAM_DIGEST_DAYS=3
```

- `TELEGRAM_DIGEST_CHAT_IDS`: chat ID group nhận thông báo, có thể nhập nhiều ID bằng dấu phẩy.
- `TELEGRAM_DIGEST_RUN_TIME`: giờ gửi hằng ngày dạng `HH:MM`. Để trống nếu không muốn gửi tự động.
- `TELEGRAM_DIGEST_DAYS`: số ngày nhìn trước để lọc hội viên sắp hết hạn.

Các trường bắt buộc khi nhập hội viên được cấu hình trong `.env`:

```env
TELEGRAM_ADD_REQUIRED_FIELDS=member_name,months
```

Mapping key trong `.env`:

| Key | Tên hiển thị |
| --- | --- |
| `member_name` | Họ tên |
| `facebook` | Nick/Link Facebook |
| `phone` | SĐT |
| `source` | Nguồn biết đến |
| `amount` | Số tiền |
| `transaction_name` | Tên CK/Mã GD |
| `months` | Gói đăng ký |
| `note` | Ghi chú |

Menu và kiểm tra trạng thái:

```text
/add
/list
/active
/expiring
/expired
/cancelled
/search Nguyễn Văn A
/renew HV000001 | 6 | 300000
/huy HV000001
/id
/menu
/help
/addchatid
/addemail
/senddigest
/testalert
/history Nguyễn Văn A
/health
/stats
```

Bot tự đăng ký danh sách lệnh với Telegram khi khởi động, nên Telegram Desktop/PC có thể mở menu lệnh của bot mà không cần tự gõ từng lệnh.

Xem danh sách hội viên:

```text
/list
/active
/expiring
/expiring 7
/expired
/search Nguyễn Văn A
```

- `/list`: xem danh sách hội viên.
- `/active`: xem hội viên còn hạn.
- `/expiring`: xem hội viên sắp hết hạn theo ngưỡng `ALERT_BEFORE_DAYS`.
- `/expiring 7`: xem hội viên hết hạn trong 7 ngày tới.
- `/expired`: xem hội viên đã hết hạn.
- `/cancelled`: xem hội viên đã hủy.
- `/search <từ khóa>`: tìm theo tên, SĐT, gói đăng ký hoặc ghi chú.
- `/health`: xem trạng thái bot và lần sync gần nhất.
- `/stats`: xem tổng số hội viên theo trạng thái.
- `/testalert`: gửi email thử cho quản trị, nội dung là danh sách hội viên sắp hết hạn.
- `/addchatid`: thêm group chat ID vào `TELEGRAM_DIGEST_CHAT_IDS` trong `.env`.
- `/addemail`: thêm email người nhận cảnh báo vào `.env`.
- `/senddigest`: gửi ngay danh sách hội viên sắp hết hạn vào các group trong `TELEGRAM_DIGEST_CHAT_IDS`.
- `/history <tên hoặc SĐT>`: xem lịch sử gia hạn.

Lưu ý: `/testalert` chỉ gửi email khi `SMTP_USER`, `SMTP_PASSWORD` và `ALERT_EMAIL_TO` đã được cấu hình.
`/addemail` sẽ tự cập nhật dòng `ALERT_EMAIL_TO=` trong file `.env` ngay khi bạn nhập email hợp lệ.

Thêm hội viên:

```text
/add
```

Bot sẽ hỏi lần lượt từng trường:

```text
Họ tên
Nick/Link Facebook
SĐT
Nguồn biết đến
Số tiền
Tên CK/Mã GD
Gói đăng ký
Ghi chú
```

Mặc định chỉ bắt buộc `Họ tên` và `Gói đăng ký`. Các trường không bắt buộc có thể gõ dấu chấm `.` để bỏ qua.
Bot tự lưu chat hiện tại vào cột `Telegram ID` để gửi cảnh báo sau này. `Ghi chú` chỉ dùng cho ghi chú tự do.

Mã hội viên:

- Bot tự sinh cột `Mã hội viên` dạng `HV000001`, `HV000002` cho các dòng chưa có mã khi sync sheet.
- Khi thêm hội viên bằng `/add`, bot trả về mã hội viên ngay sau khi lưu.
- Gia hạn và hủy dùng mã hội viên để tránh nhầm khi trùng tên.

Gia hạn hội viên:

```text
/renew Mã hội viên | Gói đăng ký | Số tiền
```

Gõ chỉ `/renew` để bot hỏi từng bước và tự lưu lịch sử gia hạn vào worksheet `LichSuGiaHan`.
Nếu hội viên đổi sang gói cao hơn hoặc thấp hơn, vẫn dùng `/renew` và nhập số tháng của gói mới. Bot tự so với gói cũ để ghi lịch sử là `nâng cấp`, `hạ cấp` hoặc `gia hạn`.

Ví dụ:

```text
/renew HV000001 | 6 | 300000
```

Hủy hội viên:

```text
/huy Mã hội viên
```

Ví dụ:

```text
/huy HV000001
```

Bot sẽ đổi trạng thái hội viên thành `Đã hủy`, xóa gói/ngày hết hạn khỏi dòng hiện tại và ghi lịch sử vào worksheet `LichSuGiaHan`.

## Kiểm thử

```powershell
python -m pytest
```
