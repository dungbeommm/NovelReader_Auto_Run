# NovelReader Auto Run

Bot Telegram nhận EPUB tiếng Trung và lưu job trên **Cloudflare R2**. Notebook MoLab đang mở sẽ poll job, chạy NovelReader, đồng bộ SQLite checkpoint và tải kết quả lên cùng bucket. GitHub Actions poll Telegram mỗi 5 phút; Actions không thể tự mở hoặc giữ phiên MoLab chạy.

## Thiết lập R2

1. Tạo [R2 bucket](https://developers.cloudflare.com/r2/get-started/) và [S3 API token](https://developers.cloudflare.com/r2/api/tokens/) có quyền đọc/ghi bucket đó. Giữ Access Key ID và Secret Access Key.
2. Trong GitHub Actions, đặt secrets `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS` (danh sách Telegram user ID ngăn bằng dấu phẩy), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.
3. Đặt repository variables `R2_ACCOUNT_ID`, `R2_BUCKET`, và tùy chọn `R2_ROOT_PREFIX` (mặc định `novelreader`). `STORAGE_BACKEND` mặc định là `r2`.
4. Trong MoLab, nhập **bản notebook mới** từ `notebook/NovelReader_Auto_Run.py` (notebook đã lưu trước đây không tự nhận thay đổi trong repo). Đặt **cùng** `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ROOT_PREFIX`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, rồi chạy notebook và giữ phiên hoạt động. Notebook tự cài `boto3` và `requests` nếu runtime chưa có.
5. Chạy Actions → **NovelReader automation** → **Run workflow** → `health-check` để kiểm tra kết nối R2. Sau đó gửi EPUB cho bot và thử `poll-telegram` nếu muốn chạy ngay.

Bot và notebook phải dùng cùng bucket và prefix. Những job cũ trên Pixeldrain không tự chuyển sang R2; hãy tạo job mới sau khi đổi backend. Có thể dùng Pixeldrain cũ bằng `STORAGE_BACKEND=pixeldrain` và `PIXELDRAIN_API_KEY` ở cả Actions và MoLab.

## Telegram

- Gửi `.epub`, chỉnh `/set <nhóm.trường> <giá trị>`, rồi bấm **Bắt đầu**.
- `/config` xem preset, `/status` xem tiến độ, `/result` lấy link tải kết quả, `/cancel` hủy bản nháp chưa chạy.
- EPUB trên 20 MB bị từ chối theo giới hạn tải của Telegram Bot API đang dùng.
- Kết quả gồm `result.zip` và các tệp riêng trong R2. Với `storage.sharing=link_reader`, bot gửi link tải ZIP có hạn 7 ngày; gọi `/result` để lấy link mới. `private` giữ kết quả trong bucket và không gửi link.

## Kiểm tra cục bộ

```bash
pip install -r requirements-dev.txt
python -m compileall -q app notebook tests
python -m unittest discover -s tests -v
marimo check notebook/NovelReader_Auto_Run.py
```

## Giới hạn vận hành

Chỉ chạy một phiên worker MoLab cho cùng bucket/prefix. Notebook phải được khởi động và duy trì phiên chạy để nhận job. Actions chỉ poll Telegram và kiểm tra kết nối; phiên MoLab hết hạn sẽ làm job tiếp tục ở `QUEUED` cho đến khi notebook chạy lại. Checkpoint và thư mục đầu ra được đồng bộ lên R2 để tiếp tục sau khi phiên worker bị ngắt. Không có kiểm thử tích hợp trực tiếp với Telegram, R2 hoặc MoLab nếu chưa cung cấp tài khoản và credentials.
