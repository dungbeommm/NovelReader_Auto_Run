# NovelReader Auto Run

Hệ thống tự động hóa xử lý tiểu thuyết Trung → Việt và chuyển văn bản thành giọng đọc (TTS) trên **MoLab** (Marimo).

## Kiến trúc hệ thống

1. **Telegram Bot**: Nhận file truyện gốc (`.txt` hoặc `.epub`), quản lý cấu hình wizard/preset và gửi job.
2. **GitHub Actions**: Khởi động máy ảo Ubuntu, cài Chromium và công cụ điều khiển trình duyệt tự động (**Playwright**).
3. **MoLab Notebook (`https://molab.marimo.io/notebooks`)**:
   - Truy cập notebook `notebook/NovelReader_Auto_Run.py`.
   - Nạp file truyện và JSON cấu hình trực tiếp hoặc qua bucket.
   - Nhấn **Run** kích hoạt pipeline: phân tích cấu trúc, dịch thuật (HachimiMT), tạo audio (Piper / VieNeu TTS), gộp chương.
   - Hiển thị tiến trình thời gian thực và đồng bộ SQLite checkpoint định kỳ (mỗi 90s).
   - Tự động nén `result.zip` và đẩy lên Cloud Storage (**Google Drive**, **Hugging Face**, **Cloudflare R2**, **Pixeldrain**).
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
     - Worker sẽ tự động poll các job `QUEUED` do Telegram bot đẩy lên.

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
