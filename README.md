# NovelReader Auto Run

Bot Telegram nhận EPUB tiếng Trung và lưu job vào [Hugging Face Storage Bucket `uri410/audio`](https://huggingface.co/buckets/uri410/audio). Notebook MoLab đang mở sẽ poll job, chạy NovelReader, đồng bộ SQLite checkpoint và tải kết quả lên cùng bucket. GitHub Actions poll Telegram mỗi 5 phút; Actions không thể tự mở hoặc giữ phiên MoLab chạy.

## Thiết lập Hugging Face Bucket

1. Tạo [Hugging Face User Access Token](https://huggingface.co/settings/tokens) có quyền **Write** với bucket `uri410/audio`.
2. Trong GitHub repository → Settings → Secrets and variables → Actions → Secrets, đặt `HF_TOKEN`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS` (danh sách Telegram user ID ngăn bằng dấu phẩy).
3. Workflow mặc định dùng `STORAGE_BACKEND=huggingface`, `HF_BUCKET_ID=uri410/audio`, `HF_ROOT_PREFIX=novelreader`. Nếu trước đó đã đặt Variable `STORAGE_BACKEND=r2`, hãy đổi thành `huggingface` (hoặc xóa Variable). Chỉ cần đặt các Variables khác khi muốn đổi giá trị mặc định.
4. Trong MoLab, nhập **bản notebook mới** từ `notebook/NovelReader_Auto_Run.py` (notebook đã lưu trước đây không tự nhận thay đổi trong repo). Đặt `HF_TOKEN` có quyền ghi trong runtime, đồng thời xóa hoặc đổi `STORAGE_BACKEND=r2` cũ. Nếu đổi bucket hoặc prefix, đặt cùng `HF_BUCKET_ID` và `HF_ROOT_PREFIX` như Actions. Chạy notebook và giữ phiên hoạt động.
5. Chạy Actions → **NovelReader automation** → **Run workflow** → `health-check`. Bước này ghi và đọc `novelreader/NovelReader_Auto_Run/healthcheck.json` để xác nhận token thực sự có quyền ghi. Sau đó gửi EPUB cho bot và thử `poll-telegram` nếu muốn chạy ngay.

Bucket `uri410/audio` hiện **công khai** theo lựa chọn của chủ bucket. EPUB gốc, manifest (gồm Telegram user ID), checkpoint, log lỗi và kết quả được lưu trong bucket nên có thể truy cập công khai qua đường dẫn tương ứng. Tùy chọn `storage.sharing=private` chỉ ngăn bot gửi link, **không đổi quyền của bucket**. Muốn giữ dữ liệu riêng tư, đổi bucket sang private trên Hugging Face và dùng token có quyền đọc/ghi; link kết quả khi đó yêu cầu người nhận đăng nhập tài khoản có quyền truy cập.

## Telegram

- Gửi `.epub`, chỉnh `/set <nhóm.trường> <giá trị>`, rồi bấm **Bắt đầu**.
- `/config` xem preset, `/status` xem tiến độ, `/result` lấy link tải kết quả, `/cancel` hủy bản nháp chưa chạy.
- EPUB trên 20 MB bị từ chối theo giới hạn tải của Telegram Bot API đang dùng.
- Kết quả gồm `result.zip` và các tệp riêng trong bucket. Với `storage.sharing=link_reader`, bot gửi link trực tiếp đến ZIP trong Hugging Face bucket.

## Kiểm tra cục bộ

```bash
pip install -r requirements-dev.txt
python -m compileall -q app notebook tests
python -m unittest discover -s tests -v
marimo check notebook/NovelReader_Auto_Run.py
```

## Giới hạn vận hành

Chỉ chạy một phiên worker MoLab cho cùng bucket/prefix. Notebook phải được khởi động và duy trì phiên chạy để nhận job. Actions chỉ poll Telegram và kiểm tra kết nối; phiên MoLab hết hạn sẽ làm job tiếp tục ở `QUEUED` cho đến khi notebook chạy lại. Checkpoint và thư mục đầu ra được đồng bộ lên bucket để tiếp tục sau khi phiên worker bị ngắt. Cần `HF_TOKEN` thực sự để kiểm thử tích hợp trực tiếp với bucket, Telegram và MoLab.

Nếu cần quay lại R2 hoặc Pixeldrain, đặt `STORAGE_BACKEND=r2` hoặc `STORAGE_BACKEND=pixeldrain` và các credentials cũ ở cả Actions lẫn MoLab. Job cũ trên nhà cung cấp trước không tự chuyển sang bucket mới.
