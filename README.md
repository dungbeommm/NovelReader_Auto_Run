# NovelReader Auto Run

Bot Telegram nhận EPUB tiếng Trung, tải truyện và manifest lên Google Drive, hiển thị cấu hình cùng nút **Bắt đầu**, rồi dispatch job sang GitHub Actions. Notebook MoLab `https://molab.marimo.io/notebooks/nb_jto8ABNt6hR63odUKU4UuJ` dùng worker tại `notebook/NovelReader_Auto_Run.py` để tìm job trên Drive, chạy NovelReader theo commit đã pin, checkpoint SQLite và upload kết quả.

## GitHub Actions secrets và variables

Secrets:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_TELEGRAM_USER_IDS` — ID số Telegram, phân cách dấu phẩy
- `GDRIVE_OAUTH_TOKEN_JSON` — OAuth JSON có refresh token và quyền Google Drive
- `GITHUB_DISPATCH_TOKEN` — token có quyền gọi repository dispatch

Variables:

- `GDRIVE_ROOT_FOLDER_ID` — tùy chọn; mặc định Drive root
- `MOLAB_NOTEBOOK_URL` — dùng URL notebook ở trên

Trong runtime MoLab, cấu hình cùng `GDRIVE_OAUTH_TOKEN_JSON` dưới dạng secret/biến môi trường. Giữ notebook worker chạy để nhận job; GitHub Actions poll Telegram theo lịch 5 phút/lượt. Runner không duy trì bot online liên tục.

## Telegram

- Gửi `.epub`; lần đầu bot hiển thị cấu hình mặc định đầy đủ. Dùng `/set nhóm.trường giá_trị` để chỉnh trước khi bấm **Bắt đầu**.
- `/config` xem lại preset; `/status` xem tiến độ khi được gọi; `/result` lấy link Google Drive.
- File trên 20 MB hiện bị từ chối theo giới hạn Telegram Bot API download.

Drive mặc định riêng tư. Chỉ chọn `drive.sharing=link_reader` nếu muốn chia sẻ link cho người có liên kết.

## Phạm vi còn thiếu

Workflow `notebook-job.yml` xác nhận yêu cầu dispatch; nó chưa tự đăng nhập, mở hoặc bấm Run trong giao diện MoLab. Notebook tự quét Drive khi phiên đã được khởi chạy. Chưa xác minh khởi chạy/đánh thức phiên MoLab từ Actions, dừng compute tự động, catalog model/voice động hoặc chạy một EPUB thật end-to-end. Cần kiểm tra các phần này trước khi vận hành job thật.
