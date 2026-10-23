# NovelReader Auto Run

Hệ thống tự động hóa xử lý tiểu thuyết Trung → Việt và chuyển văn bản thành giọng đọc (TTS) trên **MoLab** (Marimo).

## Kiến trúc hệ thống

1. **Telegram Bot**: Nhận file truyện gốc (`.txt` hoặc `.epub`), quản lý cấu hình wizard/preset và gửi job.
2. **GitHub Actions**: Poll Telegram trên GitHub-hosted runner; chạy MoLab Browser Controller trên Windows self-hosted runner với profile đăng nhập mã hóa trong Hugging Face.
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

## Lưu trữ Hugging Face và đăng nhập MoLab lần đầu

GitHub Actions nhận Telegram job; job trình duyệt chạy trên Windows self-hosted runner để có thể mở màn hình đăng nhập MoLab. Sản phẩm, job và checkpoint được lưu trong bucket `uri410/audio`. Bucket đang công khai, vì vậy profile Chromium chỉ được tải lên sau khi mã hóa; khóa giải mã chỉ lưu trong GitHub Actions Secrets.

### Secrets và biến cần cấu hình

Trong GitHub repository → **Settings → Secrets and variables → Actions**, thêm:

- Secret `HF_TOKEN`: Hugging Face token có quyền ghi bucket.
- Secret `MOLAB_PROFILE_ENCRYPTION_KEY`: tạo một lần bằng lệnh sau trên máy cá nhân rồi lưu nguyên chuỗi kết quả vào secret. Không đăng chuỗi này lên repo hoặc HF:

```powershell
python -m pip install cryptography
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

- Secret `TELEGRAM_BOT_TOKEN` và `ALLOWED_TELEGRAM_USER_IDS`.
- Repository variable `HF_BUCKET_ID` để đổi bucket khỏi `uri410/audio` (không bắt buộc).
- Repository variable `MOLAB_NOTEBOOK_URL` để dùng notebook khác URL mặc định (không bắt buộc).

Đặt `HF_TOKEN` trong Secrets của notebook MoLab nữa, vì notebook chạy trong môi trường MoLab riêng. Token cần quyền ghi bucket.

### Cài Windows runner có màn hình

1. Dùng Windows VM có Remote Desktop. Cài Python 3.12 và đăng ký VM làm GitHub self-hosted Actions runner với labels `self-hosted`, `Windows`, `molab-vm`.
2. Đăng nhập Windows bằng Remote Desktop và chạy agent runner trong desktop người dùng hiện tại, ví dụ `run.cmd`. Không chạy runner dưới dạng Windows Service cho bước đăng nhập đầu tiên; service chạy trong session nền nên cửa sổ Chromium không hiện trên desktop.
3. Giữ runner online và đăng nhập VM bằng Remote Desktop. Gửi truyện cho Telegram bot rồi bấm **Bắt đầu**. GitHub-hosted poller nhận nút bấm, lưu job lên HF rồi phát `repository_dispatch`.
4. Lần chạy đầu, Windows runner tự mở cửa sổ Chromium trên desktop VM. Đăng nhập MoLab; job sẽ tự mã hóa profile lên HF rồi tiếp tục chạy. Những lần sau runner tự khôi phục profile, mở notebook và chờ job hoàn tất.

Nếu MoLab hết phiên đăng nhập, lần bấm **Bắt đầu** tiếp theo sẽ mở lại Chromium để bạn đăng nhập. Phải giữ nguyên `MOLAB_PROFILE_ENCRYPTION_KEY` để khôi phục profile. Nếu mất khóa, xóa `novelreader/auth/molab-profile.enc` khỏi bucket rồi gửi job mới và đăng nhập lại. Do bucket công khai, người khác có thể tải ciphertext; họ vẫn cần khóa bí mật để giải mã.
