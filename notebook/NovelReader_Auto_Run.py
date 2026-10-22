"""MoLab notebook worker. Attach Drive OAuth as GDRIVE_OAUTH_TOKEN_JSON before running."""
import marimo

app = marimo.App(width="full")


@app.cell
def _(mo):
    mo.md(r"""
    # NovelReader Auto Run

    Notebook worker cho job Telegram. Worker chỉ đọc manifest đã xác nhận trong Google Drive;
    checkpoint SQLite và artifact đều được đồng bộ lại Drive. Hãy cấu hình biến bí mật
    `GDRIVE_OAUTH_TOKEN_JSON` trong runtime MoLab trước khi chạy notebook.
    """)
    return


@app.cell
def _(mo):
    import hashlib
    import json
    import logging
    import os
    import shutil
    import sqlite3
    import subprocess
    import sys
    import threading
    import time
    import traceback
    import uuid
    from datetime import datetime, timezone
    from pathlib import Path

    import marimo as mo
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload, MediaIoBaseDownload

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker_log = logging.getLogger("novelreader.worker")
    worker_stop = threading.Event()
    worker_lock = threading.Lock()
    worker_state = {"status": "initializing", "job_id": None, "message": ""}
    WORK_ROOT = Path("/tmp/novelreader-auto-run")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    NOVELREADER_REPO = "dungbeommm/NovelReader_Translate"
    NOVELREADER_COMMIT = os.getenv("NOVELREADER_COMMIT", "920a1d4464e377d6c03fd53da017f2a00b709b66")
    BOT_APP_FOLDER = "NovelReader_Auto_Run"

    def drive_client():
        raw = os.environ.get("GDRIVE_OAUTH_TOKEN_JSON", "")
        if not raw:
            raise RuntimeError("Thiếu GDRIVE_OAUTH_TOKEN_JSON trong biến môi trường MoLab.")
        creds = Credentials.from_authorized_user_info(json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"])
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def drive_list(api, query):
        return api.files().list(q=query, fields="files(id,name,mimeType,size,webViewLink)", pageSize=100,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])

    def drive_find(api, parent_id, name):
        esc = name.replace("'", "\\'")
        rows = drive_list(api, f"'{parent_id}' in parents and name='{esc}' and trashed=false")
        return rows[0] if rows else None

    def drive_json_get(api, parent_id, name, default=None):
        file = drive_find(api, parent_id, name)
        if not file:
            return default
        return json.loads(api.files().get_media(fileId=file["id"], supportsAllDrives=True).execute().decode("utf-8"))

    def drive_json_put(api, parent_id, name, value):
        payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        media = MediaInMemoryUpload(payload, mimetype="application/json", resumable=True)
        existing = drive_find(api, parent_id, name)
        if existing:
            api.files().update(fileId=existing["id"], media_body=media, fields="id", supportsAllDrives=True).execute()
        else:
            api.files().create(body={"name": name, "parents": [parent_id]}, media_body=media, fields="id", supportsAllDrives=True).execute()

    def drive_upload(api, local_path, parent_id):
        path = Path(local_path)
        existing = drive_find(api, parent_id, path.name)
        media = MediaFileUpload(str(path), resumable=True, chunksize=8 * 1024 * 1024)
        if existing:
            return api.files().update(fileId=existing["id"], media_body=media, fields="id,name,size", supportsAllDrives=True).execute()
        return api.files().create(body={"name": path.name, "parents": [parent_id]}, media_body=media,
            fields="id,name,size", supportsAllDrives=True).execute()

    def drive_download(api, file_id, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        request = api.files().get_media(fileId=file_id, supportsAllDrives=True)
        with path.open("wb") as stream:
            downloader = MediaIoBaseDownload(stream, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return path

    def drive_folder(api, parent_id, name):
        existing = drive_find(api, parent_id, name)
        if existing:
            return existing["id"]
        return api.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
            fields="id", supportsAllDrives=True).execute()["id"]

    def drive_paths(api, local_root, parent_id):
        root = Path(local_root)
        for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(root)
            dest = parent_id
            for segment in rel.parts[:-1]:
                dest = drive_folder(api, dest, segment)
            drive_upload(api, path, dest)

    def now_iso():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def ensure_novelreader():
        repo_dir = WORK_ROOT / "NovelReader"
        if not (repo_dir / ".git").exists():
            subprocess.run(["git", "clone", f"https://github.com/{NOVELREADER_REPO}.git", str(repo_dir)], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "fetch", "--depth", "1", "origin", NOVELREADER_COMMIT], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "checkout", "--detach", NOVELREADER_COMMIT], check=True)
        marker = repo_dir / ".auto_run_requirements_installed"
        if not marker.exists():
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(repo_dir / "requirements.txt")], check=True)
            marker.touch()
        sys.path.insert(0, str(repo_dir))
        return repo_dir

    def map_config(config):
        t, a, m = config["translation"], config["audio"], config["merge"]
        return {
            "backend": t["backend"], "engine": t["model"], "beam_size": t["beam_size"],
            "chunk_mode": t["chunk_mode"], "normalize_mode": t["normalize_mode"],
            "glossary": t.get("glossary", ""), "honorific_mode": t["honorific_mode"],
            "pronoun_harmonizer": t["pronoun_harmonizer"], "generate_audio": a["enabled"],
            "audio_pipeline": a["enabled"], "auto_group_audio": bool(a["enabled"] and m["enabled"]),
            "group_size": m["chapters_per_file"], "audio_workers": a["workers"],
            "tts_engine": a["engine"], "voice_id": a["voice"], "audio_speed": a["speed"],
            "audio_format": a["format"], "audio_bitrate": a["bitrate"], "audio_sample_rate": a["sample_rate"],
            "audio_workers": a["workers"], "noise_scale": a["noise_scale"], "noise_w_scale": a["noise_w_scale"],
            "vieneu_temperature": a["vieneu_temperature"], "vieneu_top_p": a["vieneu_top_p"],
            "vieneu_batch_size": a["vieneu_batch_size"],
        }

    def backup_database(store, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(store.path)) as source, sqlite3.connect(str(target)) as destination:
            source.backup(destination)

    def update_manifest(api, folder_id, manifest):
        manifest["updated_at"] = now_iso()
        drive_json_put(api, folder_id, "job.json", manifest)

    def process_job(api, folder_id, manifest, repo_dir):
        job_id = manifest["job_id"]
        worker_state.update(status="running", job_id=job_id, message="Đang tải EPUB/checkpoint")
        home = WORK_ROOT / job_id
        home.mkdir(parents=True, exist_ok=True)
        source_path = drive_download(api, manifest["input"]["drive_file_id"], home / "source.epub")
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != manifest["input"]["sha256"]:
            raise ValueError("SHA-256 EPUB trên Drive không khớp manifest")

        data_root = home / "data"
        data_root.mkdir(exist_ok=True)
        checkpoint = drive_find(api, folder_id, "checkpoint.sqlite3")
        db_path = data_root / "novel.sqlite3"
        if checkpoint:
            drive_download(api, checkpoint["id"], db_path)

        os.environ["NOVEL_OUTPUT_TEXT"] = str(data_root / "output_text")
        os.environ["NOVEL_OUTPUT_AUDIO"] = str(data_root / "output_audio")
        from storage.store import Store
        from translation.core.novel_files import parse_file
        from workflows.jobs import Job

        store = Store(db_path, text_dir=data_root / "output_text", audio_dir=data_root / "output_audio")
        if not store.book():
            chapters, encoding = parse_file(source_path.name, source_path.read_bytes())
            store.replace(source_path.name, encoding, chapters)
        chapters = store.list()
        failed_existing = [c for c in chapters if c["status"] == "error"]
        if failed_existing:
            manifest.setdefault("errors", [])
            seen = {e.get("chapter_id") for e in manifest["errors"]}
            for chapter in failed_existing:
                if chapter["id"] not in seen:
                    manifest["errors"].append({"chapter_id": chapter["id"], "chapter": chapter["position"],
                        "stage": "translate", "detail": chapter.get("error", "translation error")})
        manifest["status"] = "RUNNING"
        manifest["progress"] = {"total_chapters": len(chapters),
            "done_chapters": sum(c["status"] == "done" for c in chapters),
            "failed_chapters": sum(c["status"] == "error" for c in chapters),
            "last_chapter": max((c["position"] for c in chapters if c["status"] == "done"), default=None)}
        update_manifest(api, folder_id, manifest)

        config = map_config(manifest["config"])
        store.configure(config, reset=False)
        job = Job(store)
        job.start(config, reset=False)
        last_snapshot = 0.0
        observed = {}
        while job.active:
            time.sleep(20)
            current = store.list()
            signature = tuple((c["id"], c["status"], c.get("audio_status")) for c in current)
            if signature != observed or time.monotonic() - last_snapshot >= 90:
                observed = signature
                backup_database(store, data_root / "checkpoint.sqlite3")
                drive_upload(api, data_root / "checkpoint.sqlite3", folder_id)
                progress = {"total_chapters": len(current),
                    "done_chapters": sum(c["status"] == "done" for c in current),
                    "failed_chapters": sum(c["status"] == "error" for c in current),
                    "last_chapter": max((c["position"] for c in current if c["status"] == "done"), default=None)}
                manifest["progress"] = progress
                known = {e.get("chapter_id") for e in manifest.get("errors", [])}
                for chapter in current:
                    if chapter["status"] == "error" and chapter["id"] not in known:
                        manifest.setdefault("errors", []).append({"chapter_id": chapter["id"], "chapter": chapter["position"],
                            "stage": "translate", "detail": chapter.get("error", "translation error")})
                        known.add(chapter["id"])
                    if chapter.get("audio_status") == "error" and ("audio", chapter["id"]) not in known:
                        manifest.setdefault("errors", []).append({"chapter_id": chapter["id"], "chapter": chapter["position"],
                            "stage": "audio", "detail": "TTS thất bại; xem log MoLab"})
                        known.add(("audio", chapter["id"]))
                update_manifest(api, folder_id, manifest)
                last_snapshot = time.monotonic()

        final_chapters = store.list()
        backup_database(store, data_root / "checkpoint.sqlite3")
        drive_upload(api, data_root / "checkpoint.sqlite3", folder_id)
        book = store.book()
        try:
            from translation.core.novel_files import export_txt, export_epub
            if manifest["config"]["export"].get("txt"):
                (data_root / "translated.txt").write_bytes(export_txt(book, [store.get(c["id"]) for c in final_chapters]))
            if manifest["config"]["export"].get("epub"):
                (data_root / "translated.epub").write_bytes(export_epub(book, [store.get(c["id"]) for c in final_chapters]))
        except Exception:
            worker_log.exception("Book export failed")
            manifest.setdefault("errors", []).append({"stage": "export", "detail": traceback.format_exc()[-3000:]})
        result_folder = drive_folder(api, folder_id, "result")
        drive_paths(api, data_root / "output_text", result_folder)
        drive_paths(api, data_root / "output_audio", result_folder)
        for artifact in (data_root / "translated.txt", data_root / "translated.epub"):
            if artifact.exists():
                drive_upload(api, artifact, result_folder)
        if manifest["config"]["drive"].get("sharing") == "link_reader":
            api.permissions().create(fileId=folder_id, body={"type": "anyone", "role": "reader"}, supportsAllDrives=True).execute()
        result_link = f"https://drive.google.com/drive/folders/{folder_id}"
        progress = {"total_chapters": len(final_chapters), "done_chapters": sum(c["status"] == "done" for c in final_chapters),
                    "failed_chapters": sum(c["status"] == "error" for c in final_chapters),
                    "last_chapter": max((c["position"] for c in final_chapters if c["status"] == "done"), default=None)}
        manifest.update(status="COMPLETED_WITH_ERRORS" if manifest.get("errors") or progress["failed_chapters"] else "COMPLETED",
            progress=progress, result={"drive_link": result_link, "result_folder_id": result_folder,
                "completed_at": now_iso(), "artifacts": [p.name for p in (data_root / "translated.txt", data_root / "translated.epub") if p.exists()]})
        update_manifest(api, folder_id, manifest)
        worker_state.update(status="idle", job_id=None, message=f"Hoàn tất {job_id}")

    def worker_loop():
        while not worker_stop.is_set():
            try:
                with worker_lock:
                    api = drive_client()
                    app_folders = drive_list(api, "name='NovelReader_Auto_Run' and mimeType='application/vnd.google-apps.folder' and trashed=false")
                    if not app_folders:
                        raise RuntimeError("Không tìm thấy thư mục NovelReader_Auto_Run trên Drive")
                    app_id = app_folders[0]["id"]
                    jobs_folders = drive_list(api, f"'{app_id}' in parents and name='jobs' and mimeType='application/vnd.google-apps.folder' and trashed=false")
                    if not jobs_folders:
                        raise RuntimeError("Bot chưa tạo thư mục jobs trên Drive")
                    rows = drive_list(api, f"'{jobs_folders[0]['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false")
                    pending = None
                    for row in rows:
                        manifest = drive_json_get(api, row["id"], "job.json")
                        if manifest and manifest.get("status") in ("QUEUED", "RUNNING"):
                            pending = (row["id"], manifest)
                            break
                    if pending:
                        repo_dir = ensure_novelreader()
                        process_job(api, pending[0], pending[1], repo_dir)
                    else:
                        worker_state.update(status="idle", job_id=None, message="Đang chờ job trên Drive")
            except Exception as exc:
                worker_log.exception("Worker cycle error")
                worker_state.update(status="error", message=f"{type(exc).__name__}: {exc}")
            worker_stop.wait(35)

    if os.environ.get("GDRIVE_OAUTH_TOKEN_JSON") and not getattr(threading, "_novelreader_worker_started", False):
        thread = threading.Thread(target=worker_loop, name="novelreader-drive-worker", daemon=True)
        thread.start()
        threading._novelreader_worker_started = True
        worker_state.update(status="waiting", message="Worker nền đã khởi động; đang chờ job Google Drive")
    elif not os.environ.get("GDRIVE_OAUTH_TOKEN_JSON"):
        worker_state.update(status="needs_setup", message="Thêm biến môi trường GDRIVE_OAUTH_TOKEN_JSON vào MoLab để worker kết nối Drive")

    mo.md(f"**Trạng thái worker:** `{worker_state['status']}`  \n{worker_state['message']}  \n**Job:** `{worker_state['job_id'] or '—'}`")
    return


if __name__ == "__main__":
    app.run()
