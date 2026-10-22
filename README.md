# NovelReader Auto Run

Bot Telegram nhận EPUB tiếng Trung, lưu EPUB và manifest trong Pixeldrain Filesystem, rồi dispatch job qua GitHub Actions. Notebook MoLab dùng worker tại `notebook/NovelReader_Auto_Run.py` để nhận job, chạy NovelReader, lưu checkpoint SQLite và tải kết quả lên Pixeldrain.

Pixeldrain Filesystem yêu cầu tài khoản Pro hoặc Prepaid. Tạo API key tại [Pixeldrain API keys](https://pixeldrain.com/user/api_keys). Bot và notebook phải dùng cùng API key và cùng `PIXELDRAIN_ROOT_PATH`.

## GitHub Actions

Secrets:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_TELEGRAM_USER_IDS` — các Telegram user ID, phân cách bằng dấu phẩy
- `PIXELDRAIN_API_KEY`
- `GH_DISPATCH_TOKEN` — token có quyền repository `Contents: write` để gửi dispatch

Variables:

- `PIXELDRAIN_ROOT_PATH` — tùy chọn, mặc định `/me`
- `MOLAB_NOTEBOOK_URL` — URL notebook MoLab

Trong runtime MoLab, đặt `PIXELDRAIN_API_KEY` và, nếu đã đổi thư mục gốc, `PIXELDRAIN_ROOT_PATH`. Giữ notebook worker chạy để nhận job. GitHub Actions poll Telegram mỗi 5 phút; runner không giữ bot online liên tục.

## Telegram

- Gửi `.epub`, chỉnh cấu hình bằng `/set <nhóm.trường> <giá trị>`, rồi bấm **Bắt đầu**.
- `/config` xem preset; `/status` xem tiến độ; `/result` lấy link Pixeldrain.
- EPUB trên 20 MB hiện bị từ chối theo giới hạn tải của Telegram Bot API trong ứng dụng này.
- Mặc định `pixeldrain.sharing=link_reader`: worker chỉ chia sẻ thư mục `result` khi hoàn tất. Có thể đặt `private` để giữ kết quả riêng tư, khi đó bot không có link công khai để gửi.

Tạo job mới sau khi cấu hình Pixeldrain.

## Phạm vi còn thiếu

Workflow xác thực dispatch nhưng không tự mở hay bấm Run trên MoLab. Notebook worker cần được khởi chạy và giữ phiên hoạt động để xử lý job.
