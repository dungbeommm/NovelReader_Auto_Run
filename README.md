# NovelReader Auto Run

Hệ thống tự động hóa xử lý tiểu thuyết Trung → Việt và chuyển văn bản thành giọng đọc (TTS) trên **MoLab** (Marimo).

## Kiến trúc hệ thống

1. **Telegram Bot**: Nhận file truyện gốc (`.txt` hoặc `.epub`), quản lý cấu hình wizard/preset và gửi job.
2. **GitHub Actions**: Chạy bot Telegram trên Ubuntu và worker hàng đợi trên Windows self-hosted runner song song trong cùng lịch workflow; cả hai phối hợp qua Hugging Face, bot tự bàn giao trước giới hạn 6 giờ.
3. **MoLab Notebook (`https://molab.marimo.io/notebooks`)**:
   - Truy cập notebook `notebook/NovelReader_Auto_Run.py`.
   - Nạp file truyện và JSON cấu hình trực tiếp hoặc qua bucket.
   - Nhấn **Run** kích hoạt pipeline: phân tích cấu trúc, dịch thuật (HachimiMT), tạo audio (Piper / VieNeu TTS), gộp chương.
   - Hiển thị tiến trình thời gian thực và đồng bộ SQLite checkpoint định kỳ (mỗi 90s).
   - Tự động nén `result.zip` và lưu cùng job/checkpoint trên Hugging Face bucket.
4. **Trả kết quả & Giải phóng tài nguyên**:
   - Browser controller lấy link tải công khai và gửi thông báo qua Telegram.
   - Bấm nút **Stop** trên MoLab để ngưng phiên tính toán, tiết kiệm hạn mức tài nguyên GPU/CPU.

---

## Cách sử dụng trên MoLab (`https://molab.marimo.io`)

1. Truy cập [MoLab Marimo](https://molab.marimo.io/notebooks) hoặc tạo notebook mới.
2. Nhập mã nguồn từ tệp [`notebook/NovelReader_Auto_Run.py`](notebook/NovelReader_Auto_Run.py).
3. Có 2 cách vận hành:
   - **Chế độ giao diện trực quan (UI Mode / Browser Controller)**:
     - Chọn hoặc kéo thả file `.txt` / `.epub` vào ô tải file.
     - Chọn nơi lưu kết quả (Hugging Face / Google Drive / R2 / Pixeldrain / Local).
     - Điều chỉnh JSON cấu hình (nếu cần).
     - Bấm nút **🚀 BẮT ĐẦU CHẠY NOVELREADER (RUN)**.
     - Sau khi xong, nhận link tải trực tiếp tại khung kết quả và bấm **🛑 DỪNG PHIÊN (STOP)**.
   - **Chế độ công nhân nền (Background Poller Mode)**:
     - Cấu hình biến môi trường (`HF_TOKEN`, `GDRIVE_OAUTH_TOKEN_JSON`, v.v.) trong MoLab runtime.
     - Worker của notebook nhận job từ Hugging Face nếu app runtime đang mở.
   - Windows queue worker trong GitHub Actions tự poll các job `QUEUED` mà bot ghi vào Hugging Face, rồi mở URL app công khai để xử lý.

---

## Điều khiển trình duyệt tự động (Browser Controller)

Sử dụng script Playwright để tự động hóa hoàn toàn các bước mở MoLab, nhập liệu và theo dõi:

```bash
pip install -r requirements-dev.txt
playwright install chromium
python -m browser.molab_controller --notebook-url "https://molab.marimo.io/notebooks/..." --file "truyen_goc.txt"
```

---

## Telegram Bot

- Gửi `.epub` hoặc `.txt` để khởi tạo job.
- Lệnh: `/config` (xem cấu hình), `/set <nhóm.trường> <giá trị>` (chỉnh cấu hình), `/status` (xem tiến độ), `/result` (lấy link tải), `/cancel` (hủy bản nháp).

---

## Kiểm tra chất lượng mã nguồn cục bộ

```bash
pip install -r requirements-dev.txt
python -m compileall -q app notebook browser tests
python -m unittest discover -s tests -v
marimo check notebook/NovelReader_Auto_Run.py
```

## Lưu trữ Hugging Face và MoLab app công khai

GitHub Actions giữ bot Telegram hoạt động. Khi người dùng bấm **Bắt đầu**, bot gửi `repository_dispatch` kèm mã job; một workflow Windows riêng lần lượt tải file/cấu hình từ Hugging Face, mở URL app MoLab công khai, chạy notebook, lưu kết quả và bot gửi link về Telegram.

### Secrets và biến cần cấu hình

Trong GitHub repository → **Settings → Secrets and variables → Actions**, thêm:

- Secret `HF_TOKEN`: Hugging Face token có quyền ghi bucket.
- Bot dùng `GITHUB_TOKEN` do Actions cấp với quyền `Actions: write` và `Contents: write` để khởi chạy job và tự bàn giao; không cần lưu GitHub PAT riêng.
- Secret `TELEGRAM_BOT_TOKEN` và `ALLOWED_TELEGRAM_USER_IDS`.
- Repository variable `HF_BUCKET_ID` để đổi bucket khỏi `uri410/audio` (không bắt buộc).
- Repository variable `MOLAB_NOTEBOOK_URL` để dùng app URL khác (mặc định là app công khai NovelReader).

`HF_TOKEN` phải được cấu hình riêng trong runtime MoLab và trong GitHub Actions; hai môi trường không chia sẻ biến môi trường. Token cần quyền ghi bucket. Nếu thiếu token hoặc worker gặp lỗi khi xử lý job, manifest sẽ chuyển sang `FAILED` và bot gửi chi tiết lỗi.

### Chạy workflow phối hợp

1. Bấm **Run workflow** để khởi động bot Telegram.
2. Gửi truyện, chỉnh cấu hình nếu cần rồi bấm **Bắt đầu**. Bot lưu job vào HF và gửi mã job sang GitHub.
3. Workflow job mở [URL app MoLab công khai](https://molab.marimo.io/notebooks/nb_LUac1bjo4xszP8PzayfLSN/app), chạy notebook, lưu kết quả; bot gửi link về Telegram. Không cần VM self-hosted hay đăng nhập MoLab.

### Sơ đồ chữ: một nút Bắt đầu chạy toàn bộ job

```text
Push main / lịch 5 giờ / Run workflow (không có menu mode)
                       │
                       ▼
        GitHub Actions chạy Telegram bot liên tục
        (Ubuntu, watchdog tự restart và bàn giao)
                       │
     Gửi EPUB/TXT + bấm nút Bắt đầu trong Telegram
                       │
                       ▼
    Bot ghi QUEUED + file + cấu hình vào Hugging Face
                       ▼
       Bot gửi repository_dispatch(job_id) sang GitHub
                       │
                       ▼
       Workflow Windows xử lý đúng job theo trình tự
       Mở MoLab → chạy notebook → lưu kết quả lên HF
                       │
                       ▼
       Bot Telegram gửi trạng thái và link kết quả
```

Bot khởi động khi push lên `main`, theo lịch dự phòng mỗi 5 giờ hoặc khi bấm **Run workflow**. Watchdog xếp lượt bot tiếp theo ở phút 320, chờ chu kỳ poll hiện tại kết thúc rồi bàn giao; trạng thái đã lưu trên HF nên không mất khi runner đổi lượt.
