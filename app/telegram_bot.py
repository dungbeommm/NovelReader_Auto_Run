from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from app.drive import DriveStore

log = logging.getLogger("novelreader.bot")
DEFAULT_CONFIG: dict[str, Any] = {
    "source_language": "zh", "target_language": "vi",
    "compute": {"provider": "molab", "device": "auto"},
    "translation": {"enabled": True, "backend": "ct2", "model": "HachimiMT-60-QT", "beam_size": 2,
                    "chunk_mode": "sentence", "normalize_mode": "auto", "glossary": "",
                    "honorific_mode": "safe", "pronoun_harmonizer": True},
    "audio": {"enabled": True, "engine": "piper", "voice": "ngochuyennew", "speed": 1.0,
              "format": "mp3", "bitrate": "96k", "sample_rate": 0, "workers": 1,
              "noise_scale": 0.667, "noise_w_scale": 0.8, "vieneu_temperature": 0.8,
              "vieneu_top_p": 0.95, "vieneu_batch_size": 8},
    "merge": {"enabled": True, "chapters_per_file": 50},
    "export": {"txt": True, "epub": True, "chapter_audio": True, "keep_chapter_audio": True},
    "drive": {"folder_id": "", "sharing": "private"},
}
MAX_TELEGRAM_DOWNLOAD = 20 * 1024 * 1024


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_path(document: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cursor = document
    for key in keys[:-1]:
        if key not in cursor or not isinstance(cursor[key], dict):
            raise ValueError(f"Unknown setting: {dotted}")
        cursor = cursor[key]
    if keys[-1] not in cursor:
        raise ValueError(f"Unknown setting: {dotted}")
    old = cursor[keys[-1]]
    try:
        if isinstance(old, bool):
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized not in ("true", "yes", "1", "on", "false", "no", "0", "off"):
                    raise ValueError
                value = normalized in ("true", "yes", "1", "on")
            elif not isinstance(value, bool):
                raise ValueError
        elif isinstance(old, int) and not isinstance(old, bool):
            value = int(value)
        elif isinstance(old, float):
            value = float(value)
        elif isinstance(old, str):
            value = str(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid value for {dotted}: {value}") from exc
    cursor[keys[-1]] = value


def pretty_config(config: dict[str, Any]) -> str:
    t, a = config["translation"], config["audio"]
    m, e = config["merge"], config["export"]
    rows = [
        "CẤU HÌNH JOB — Trung → Việt",
        f"Compute: {config['compute']['provider']} / {config['compute']['device']}",
        f"Dịch: {'bật' if t['enabled'] else 'tắt'} | {t['backend']} | {t['model']} | beam={t['beam_size']} | chunk={t['chunk_mode']} | chuẩn hóa={t['normalize_mode']}",
        f"Glossary: {t['glossary'] or '(trống)'} | xưng hô={t['honorific_mode']} | hài hòa đại từ={t['pronoun_harmonizer']}",
        f"Audio: {'bật' if a['enabled'] else 'tắt'} | {a['engine']} | voice={a['voice']} | tốc độ={a['speed']} | {a['format']} {a['bitrate']} | sample rate={a['sample_rate']} | workers={a['workers']}",
        f"Thông số giọng đọc: noise={a['noise_scale']}, noise-w={a['noise_w_scale']}, VieNeu temperature={a['vieneu_temperature']}, top-p={a['vieneu_top_p']}, batch={a['vieneu_batch_size']}",
        f"Gộp audio: {'bật' if m['enabled'] else 'tắt'}, {m['chapters_per_file']} chương/file",
        f"Xuất: TXT={e['txt']}, EPUB={e['epub']}, audio chương={e['chapter_audio']}, giữ file lẻ={e['keep_chapter_audio']}",
        f"Drive folder: {config['drive']['folder_id'] or '(thư mục gốc cấu hình ở Actions)'} | chia sẻ={config['drive']['sharing']}",
        "",
        "Sửa trường: /set <nhóm.trường> <giá trị> — ví dụ /set audio.voice ngochuyennew",
        "Dùng /config để xem lại cấu hình. Gửi EPUB rồi bấm Bắt đầu để chạy.",
    ]
    return "\n".join(rows)


class TelegramBot:
    def __init__(self, token: str, allowed_users: set[str], drive: DriveStore) -> None:
        self.token = token
        self.allowed = allowed_users
        self.drive = drive
        self.http = requests.Session()
        self.base = f"https://api.telegram.org/bot{token}"
        self.files_base = f"https://api.telegram.org/file/bot{token}"
        self.app_folder = drive.ensure_folder("NovelReader_Auto_Run")
        self.users_folder = drive.ensure_folder("users", self.app_folder)
        self.jobs_folder = drive.ensure_folder("jobs", self.app_folder)
        self.shared_state = drive.get_json(self.app_folder, "bot_state.json", {"offset": 0, "users": {}})

    def call(self, method: str, **payload: Any) -> Any:
        response = self.http.post(f"{self.base}/{method}", json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method}: {body.get('description', 'API error')}")
        return body["result"]

    def send(self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        self.call("sendMessage", **payload)

    def _user_folder(self, user_id: str) -> str:
        return self.drive.ensure_folder(user_id, self.users_folder)

    def _user(self, user_id: str) -> dict[str, Any]:
        users = self.shared_state.setdefault("users", {})
        if user_id not in users:
            folder = self._user_folder(user_id)
            users[user_id] = {"folder_id": folder, "config": None, "draft": None, "last_job": None}
        return users[user_id]

    def save_state(self) -> None:
        self.drive.put_json(self.app_folder, "bot_state.json", self.shared_state)

    def _job(self, job_id: str) -> tuple[str, dict[str, Any] | None]:
        folder = self.drive.ensure_folder(job_id, self.jobs_folder)
        return folder, self.drive.get_json(folder, "job.json")

    def _handle_document(self, message: dict[str, Any], user_id: str) -> None:
        doc = message.get("document", {})
        name = doc.get("file_name", "")
        if not name.lower().endswith(".epub"):
            self.send(message["chat"]["id"], "Chỉ nhận file EPUB. Hãy gửi truyện gốc dạng .epub.")
            return
        if int(doc.get("file_size", 0)) > MAX_TELEGRAM_DOWNLOAD:
            self.send(message["chat"]["id"], "File vượt giới hạn tải Telegram 20 MB. Hãy dùng EPUB nhỏ hơn.")
            return
        meta = self.call("getFile", file_id=doc["file_id"])
        download = self.http.get(f"{self.files_base}/{meta['file_path']}", timeout=180)
        download.raise_for_status()
        data = download.content
        if len(data) > MAX_TELEGRAM_DOWNLOAD:
            raise ValueError("Downloaded file exceeds Telegram's 20 MB limit")
        job_id = str(uuid.uuid4())
        folder = self.drive.ensure_folder(job_id, self.jobs_folder)
        temp = Path("/tmp") / f"{job_id}.epub"
        temp.write_bytes(data)
        try:
            uploaded = self.drive.upload(temp, folder, "source.epub")
        finally:
            temp.unlink(missing_ok=True)
        user = self._user(user_id)
        config = deepcopy(user.get("config") or DEFAULT_CONFIG)
        config["drive"]["folder_id"] = config["drive"].get("folder_id") or self.drive.root_id
        digest = hashlib.sha256(data).hexdigest()
        draft = {"job_id": job_id, "folder_id": folder, "input": {"drive_file_id": uploaded["id"], "name": name,
                 "sha256": digest, "size": len(data)}, "config": config, "chat_id": message["chat"]["id"]}
        user["draft"] = draft
        self.drive.put_json(folder, "job.json", {"schema_version": 1, "job_id": job_id, "owner_id": user_id,
            "created_at": utcnow(), "updated_at": utcnow(), "status": "CONFIGURING", "input": draft["input"],
            "config": config, "progress": {"total_chapters": 0, "done_chapters": 0, "failed_chapters": 0, "last_chapter": None},
            "errors": [], "result": None})
        self.save_state()
        first_time = not user.get("config")
        message_text = f"Đã lưu EPUB {name} lên Google Drive ({len(data):,} byte; SHA256 {digest[:16]}…).\n\n"
        if first_time:
            message_text += "Đây là lần đầu: xem toàn bộ cấu hình mặc định bên dưới. Chỉnh bằng /set <nhóm.trường> <giá trị>; sau khi lưu, lần sau bot dùng preset này.\n\n"
        else:
            message_text += "Đã nạp preset đã lưu. Dùng /config hoặc /set để chỉnh trước khi chạy.\n\n"
        message_text += pretty_config(config)
        self.send(message["chat"]["id"], message_text,
                  {"inline_keyboard": [[{"text": "Bắt đầu", "callback_data": f"start:{job_id}"},
                                        {"text": "Hủy", "callback_data": f"cancel:{job_id}"}]]})

    def _dispatch(self, job_id: str, folder_id: str) -> None:
        token = os.environ.get("GH_DISPATCH_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "dungbeommm/NovelReader_Auto_Run")
        if not token:
            raise RuntimeError("GH_DISPATCH_TOKEN chưa cấu hình trong GitHub Secrets")
        response = self.http.post(f"https://api.github.com/repos/{repo}/dispatches",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"event_type": "novelreader_job", "client_payload": {"job_id": job_id, "drive_folder_id": folder_id}}, timeout=30)
        if response.status_code not in (200, 204):
            raise RuntimeError(f"GitHub dispatch HTTP {response.status_code}: {response.text[:300]}")

    def _callback(self, update: dict[str, Any], user_id: str) -> None:
        query = update["callback_query"]
        data = query.get("data", "")
        chat_id = query["message"]["chat"]["id"]
        self.call("answerCallbackQuery", callback_query_id=query["id"])
        user = self._user(user_id)
        draft = user.get("draft")
        if not draft or draft["job_id"] not in data:
            self.send(chat_id, "Bản nháp này không còn hoạt động. Hãy gửi EPUB lại.")
            return
        if data.startswith("cancel:"):
            user["draft"] = None
            self.save_state()
            self.send(chat_id, "Đã hủy bản nháp. File EPUB vẫn được giữ trong Drive.")
            return
        if data.startswith("start:"):
            folder, manifest = self._job(draft["job_id"])
            if not manifest:
                raise RuntimeError("Không tìm thấy manifest job trên Drive")
            if manifest["status"] != "CONFIGURING":
                self.send(chat_id, "Job đã được gửi chạy hoặc đang chạy; không tạo lượt trùng.")
                return
            import jsonschema
            schema_path = Path(__file__).resolve().parents[1] / "schemas" / "config.schema.json"
            jsonschema.validate(draft["config"], json.loads(schema_path.read_text(encoding="utf-8")))
            manifest.update(status="QUEUED", config=draft["config"], updated_at=utcnow())
            self.drive.put_json(folder, "job.json", manifest)
            self.drive.put_json(self._user_folder(user_id), "config.json", draft["config"])
            user["config"] = draft["config"]
            user["last_job"] = draft["job_id"]
            self.save_state()
            self._dispatch(draft["job_id"], folder)
            self.send(chat_id, f"Đã xếp job {draft['job_id']} vào hàng đợi. Bot sẽ gửi log lỗi và link Google Drive khi hoàn tất.")
            user["draft"] = None
            self.save_state()

    def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        user_obj = (message or update.get("callback_query", {}).get("from", {})).get("from", {})
        user_id = str(user_obj.get("id", ""))
        if not user_id or (self.allowed and user_id not in self.allowed):
            if message:
                self.send(message["chat"]["id"], "Tài khoản Telegram này chưa được cấp quyền sử dụng bot.")
            return
        if "callback_query" in update:
            self._callback(update, user_id)
            return
        if not message:
            return
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        if message.get("document"):
            self._handle_document(message, user_id)
            return
        if text.startswith("/start") or text.startswith("/help"):
            self.send(chat_id, "Gửi EPUB tiếng Trung để tạo job. Lệnh: /config, /set nhóm.trường giá_trị, /status, /result, /cancel.")
        elif text.startswith("/config"):
            user = self._user(user_id)
            user["config"] = user.get("config") or self.drive.get_json(user["folder_id"], "config.json") or deepcopy(DEFAULT_CONFIG)
            self.send(chat_id, pretty_config(user["config"]))
        elif text.startswith("/set "):
            parts = text.split(maxsplit=2)
            if len(parts) != 3:
                self.send(chat_id, "Cú pháp: /set nhóm.trường giá_trị (vd: /set audio.speed 1.1)")
                return
            user = self._user(user_id)
            config = deepcopy(user.get("config") or (user.get("draft") or {}).get("config") or self.drive.get_json(user["folder_id"], "config.json") or DEFAULT_CONFIG)
            try:
                set_path(config, parts[1], parts[2])
                user["config"] = config
                if user.get("draft"):
                    user["draft"]["config"] = config
                    folder, manifest = self._job(user["draft"]["job_id"])
                    manifest["config"] = config
                    self.drive.put_json(folder, "job.json", manifest)
                self.drive.put_json(user["folder_id"], "config.json", config)
                self.save_state()
                self.send(chat_id, "Đã cập nhật preset.\n\n" + pretty_config(config))
            except ValueError as exc:
                self.send(chat_id, str(exc))
        elif text.startswith("/status"):
            user = self._user(user_id)
            job_id = user.get("last_job") or (user.get("draft") or {}).get("job_id")
            if not job_id:
                self.send(chat_id, "Chưa có job nào.")
                return
            folder, manifest = self._job(job_id)
            if manifest:
                p = manifest.get("progress", {})
                self.send(chat_id, f"Job {job_id}\nTrạng thái: {manifest['status']}\nChương: {p.get('done_chapters', 0)}/{p.get('total_chapters', 0)}; lỗi: {p.get('failed_chapters', 0)}")
            else:
                self.send(chat_id, f"Không tìm thấy dữ liệu job {job_id}.")
        elif text.startswith("/result"):
            user = self._user(user_id)
            job_id = user.get("last_job")
            folder, manifest = self._job(job_id) if job_id else ("", None)
            result = manifest.get("result") if manifest else None
            self.send(chat_id, (result or {}).get("drive_link", "Chưa có kết quả Drive cho job gần nhất."))
        elif text.startswith("/cancel"):
            user = self._user(user_id)
            user["draft"] = None
            self.save_state()
            self.send(chat_id, "Đã hủy bản nháp chưa chạy. Job đang chạy cần được dừng từ notebook; dùng /status để xem trạng thái.")
        else:
            self.send(chat_id, "Gửi file .epub hoặc dùng /help.")

    def poll(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        last_scan = 0.0
        while time.monotonic() < deadline:
            try:
                rows = self.call("getUpdates", offset=int(self.shared_state.get("offset", 0)) + 1,
                                 timeout=20, allowed_updates=["message", "callback_query"])
                for update in rows:
                    self.shared_state["offset"] = update["update_id"]
                    try:
                        self.handle(update)
                    except Exception:
                        log.exception("Update handling failed; update_id=%s", update.get("update_id"))
                        msg = update.get("message", {})
                        if msg.get("chat", {}).get("id"):
                            try:
                                self.send(msg["chat"]["id"], f"Lỗi xử lý (update {update['update_id']}). Log đã được ghi vào Actions; vui lòng thử lại hoặc báo quản trị viên.")
                            except Exception:
                                log.exception("Could not send error notification")
                    self.save_state()
                if time.monotonic() - last_scan > 25:
                    self.notify_job_updates()
                    last_scan = time.monotonic()
            except Exception:
                log.exception("Telegram polling failed")
                time.sleep(5)
        self.save_state()

    def notify_job_updates(self) -> None:
        """Deliver chapter errors and finished Drive links persisted by the notebook worker."""
        for entry in self.drive.list_children(self.jobs_folder):
            if entry.get("mimeType") != "application/vnd.google-apps.folder":
                continue
            folder = entry["id"]
            manifest = self.drive.get_json(folder, "job.json")
            if not manifest:
                continue
            chat_id = manifest.get("owner_id")
            if not chat_id:
                continue
            notified = manifest.setdefault("notifications", {"errors": 0, "result": False})
            errors = manifest.get("errors", [])
            while notified.get("errors", 0) < len(errors):
                error = errors[notified.get("errors", 0)]
                snippet = str(error.get("detail", ""))[-2200:]
                self.send(int(chat_id), f"Lỗi job {manifest['job_id']} | chương {error.get('chapter', '?')} | {error.get('stage', 'worker')}\n{snippet}")
                notified["errors"] = notified.get("errors", 0) + 1
                self.drive.put_json(folder, "job.json", manifest)
            if manifest.get("status") in ("COMPLETED", "COMPLETED_WITH_ERRORS") and not notified.get("result"):
                result = manifest.get("result") or {}
                link = result.get("drive_link", "(chưa có link)")
                counts = manifest.get("progress", {})
                self.send(int(chat_id), f"Job {manifest['job_id']} hoàn tất: {manifest['status']}\nChương {counts.get('done_chapters', 0)}/{counts.get('total_chapters', 0)}; lỗi {counts.get('failed_chapters', 0)}\nGoogle Drive: {link}")
                notified["result"] = True
                self.drive.put_json(folder, "job.json", manifest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=220)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    allowed = {x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(",") if x.strip()}
    TelegramBot(token, allowed, DriveStore()).poll(max(30, min(args.poll_seconds, 230)))


if __name__ == "__main__":
    main()
