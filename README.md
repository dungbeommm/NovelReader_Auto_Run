# NovelReader Auto Run

Bot Telegram nhận EPUB tiếng Trung, tải truyện và manifest lên Google Drive, hiển thị cấu hình cùng nút **Bắt đầu**, rồi dispatch job sang GitHub Actions. Notebook MoLab `https://molab.marimo.io/notebooks/nb_jto8ABNt6hR63odUKU4UuJ` dùng worker tại `notebook/NovelReader_Auto_Run.py` để tìm job trên Drive, chạy NovelReader theo commit đã pin, checkpoint SQLite và upload kết quả.

## GitHub Actions secrets và variables

Secrets:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_TELEGRAM_USER_IDS` — ID số Telegram, phân cách dấu phẩy
- `GDRIVE_OAUTH_TOKEN_JSON` — OAuth authorized-user JSON có refresh token, hoặc service-account JSON hợp lệ; có thể lưu JSON thô hoặc Base64 của JSON
- `GH_DISPATCH_TOKEN` — fine-grained token có quyền repository `Contents: write` để gọi repository dispatch

Variables:

- `GDRIVE_ROOT_FOLDER_ID` — tùy chọn; mặc định Drive root
- `MOLAB_NOTEBOOK_URL` — dùng URL notebook ở trên

Trong runtime MoLab, cấu hình cùng `GDRIVE_OAUTH_TOKEN_JSON` dưới dạng secret/biến môi trường. Nếu dùng service account, chia sẻ thư mục Drive đích cho địa chỉ `client_email` của service account. JSON `credentials.json` chỉ có `installed`/`web` OAuth client là chưa đủ; cần token đã cấp quyền và có `refresh_token`. Giữ notebook worker chạy để nhận job; GitHub Actions poll Telegram theo lịch 5 phút/lượt. Runner không duy trì bot online liên tục.

## Telegram

- Gửi `.epub`; lần đầu bot hiển thị cấu hình mặc định đầy đủ. Dùng `/set nhóm.trường giá_trị` để chỉnh trước khi bấm **Bắt đầu**.
- `/config` xem lại preset; `/status` xem tiến độ khi được gọi; `/result` lấy link Google Drive.
- File trên 20 MB hiện bị từ chối theo giới hạn Telegram Bot API download.

Drive mặc định riêng tư. Chỉ chọn `drive.sharing=link_reader` nếu muốn chia sẻ link cho người có liên kết.

## Phạm vi còn thiếu

Một workflow duy nhất `.github/workflows/novelreader.yml` chạy CI, poll Telegram theo lịch và xác thực yêu cầu dispatch. Chế độ `workflow_dispatch` có `health-check` và `poll-telegram`. Workflow không đăng nhập hoặc bấm Run trong giao diện MoLab; notebook worker vẫn cần được khởi chạy trong MoLab và giữ phiên hoạt động để xử lý job.
