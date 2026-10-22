"""MoLab notebook worker. Set PIXELDRAIN_API_KEY before running."""
import marimo

__generated_with = "0.25.2"
app = marimo.App(width="full")


@app.cell
def _(mo):
    mo.md(r"""
    # NovelReader Auto Run

    Notebook worker cho job Telegram. Worker chỉ đọc manifest đã xác nhận trong Pixeldrain;
    checkpoint SQLite và artifact đều được đồng bộ lại Pixeldrain. Hãy cấu hình biến bí mật
    `PIXELDRAIN_API_KEY` trong runtime MoLab trước khi chạy notebook.
    """)
    return


@app.cell
def _():
    import requests as _requests
    requests_ready = True
    return (requests_ready,)


@app.cell
def _(requests_ready, mo):
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
    import requests
    from urllib.parse import quote

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

    def require_api_key():
        key = os.environ.get("PIXELDRAIN_API_KEY", "").strip()
        if not key:
            raise RuntimeError("PIXELDRAIN_API_KEY is missing")
        return key

    class PixeldrainStore:
        def __init__(self, api_key: str | None = None, root_id: str | None = None) -> None:
            self.session = requests.Session()
            self.session.auth = ("", api_key or require_api_key())
            self.root_id = (root_id or os.environ.get("PIXELDRAIN_ROOT_PATH") or "/me").rstrip("/")
            if not self.root_id.startswith("/me") or (self.root_id != "/me" and not self.root_id.startswith("/me/")):
                raise ValueError("PIXELDRAIN_ROOT_PATH must be under /me")

        @staticmethod
        def child(parent: str, name: str) -> str:
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                raise ValueError(f"Invalid Pixeldrain path component: {name!r}")
            return parent.rstrip("/") + "/" + name

        def _url(self, path: str) -> str:
            if not path.startswith("/me/") and path != "/me":
                raise ValueError("Pixeldrain path must be under /me")
            return "https://pixeldrain.com/api/filesystem" + "/".join(quote(p, safe="") for p in path.split("/"))

        def _request(self, method: str, path: str, **kwargs):
            response = self.session.request(method, self._url(path), timeout=(20, 300), **kwargs)
            response.raise_for_status()
            return response

        def stat(self, path: str) -> dict:
            return self._request("GET", path, params={"stat": ""}).json()

        def list_children(self, parent_id: str) -> list[dict]:
            return self.stat(parent_id)["children"]

        def find(self, parent_id: str, name: str) -> dict | None:
            return next((row for row in self.list_children(parent_id) if row["name"] == name), None)

        def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
            path = self.child(parent_id or self.root_id, name)
            try:
                self._request("POST", path, files={"action": (None, "mkdirall")})
            except requests.HTTPError:
                info = self.stat(path)
                if info["path"][info["base_index"]]["type"] != "dir":
                    raise
            return path

        def put_json(self, parent_id: str, name: str, data) -> str:
            path = self.child(parent_id, name)
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self._request("PUT", path, params={"make_parents": "true"}, data=body,
                          headers={"Content-Type": "application/json"})
            return path

        def get_json(self, parent_id: str, name: str, default=None):
            if self.find(parent_id, name) is None:
                return default
            return self._request("GET", self.child(parent_id, name)).json()

        def upload(self, path: Path, parent_id: str, name: str | None = None) -> dict:
            target = self.child(parent_id, name or path.name)
            with path.open("rb") as stream:
                node = self._request("PUT", target, params={"make_parents": "true"}, data=stream).json()
            return node

        def download(self, file_id: str, target: Path) -> Path:
            target.parent.mkdir(parents=True, exist_ok=True)
            with self.session.get(self._url(file_id), stream=True, timeout=(20, 300)) as response:
                response.raise_for_status()
                with target.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        stream.write(chunk)
            return target

        def share_link_reader(self, path: str) -> str:
            node = self._request("POST", path, files={"action": (None, "update"),
                                                      "shared": (None, "true")}).json()
            return f"https://pixeldrain.com/d/{node['id']}"

    def upload_paths(api, local_root, parent_id):
        root = Path(local_root)
        if not root.exists():
            return
        for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(root)
            dest = parent_id
            for segment in rel.parts[:-1]:
                dest = api.ensure_folder(segment, dest)
            api.upload(path, dest)

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
        api.put_json(folder_id, "job.json", manifest)

    def process_job(api, folder_id, manifest, repo_dir):
        job_id = manifest["job_id"]
        worker_state.update(status="running", job_id=job_id, message="Đang tải EPUB/checkpoint")
        home = WORK_ROOT / job_id
        home.mkdir(parents=True, exist_ok=True)
        source_path = api.download(manifest["input"]["pixeldrain_path"], home / "source.epub")
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != manifest["input"]["sha256"]:
            raise ValueError("SHA-256 EPUB on Pixeldrain does not match manifest")

        data_root = home / "data"
        data_root.mkdir(exist_ok=True)
        checkpoint = api.find(folder_id, "checkpoint.sqlite3")
        db_path = data_root / "novel.sqlite3"
        if checkpoint:
            api.download(checkpoint["path"], db_path)

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
                api.upload(data_root / "checkpoint.sqlite3", folder_id)
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
        api.upload(data_root / "checkpoint.sqlite3", folder_id)
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
        result_folder = api.ensure_folder("result", folder_id)
        upload_paths(api, data_root / "output_text", result_folder)
        upload_paths(api, data_root / "output_audio", result_folder)
        for artifact in (data_root / "translated.txt", data_root / "translated.epub"):
            if artifact.exists():
                api.upload(artifact, result_folder)
        result_link = api.share_link_reader(result_folder) if manifest["config"]["pixeldrain"].get("sharing") == "link_reader" else None
        progress = {"total_chapters": len(final_chapters), "done_chapters": sum(c["status"] == "done" for c in final_chapters),
                    "failed_chapters": sum(c["status"] == "error" for c in final_chapters),
                    "last_chapter": max((c["position"] for c in final_chapters if c["status"] == "done"), default=None)}
        manifest.update(status="COMPLETED_WITH_ERRORS" if manifest.get("errors") or progress["failed_chapters"] else "COMPLETED",
            progress=progress, result={"pixeldrain_link": result_link, "result_path": result_folder,
                "completed_at": now_iso(), "artifacts": [p.name for p in (data_root / "translated.txt", data_root / "translated.epub") if p.exists()]})
        update_manifest(api, folder_id, manifest)
        worker_state.update(status="idle", job_id=None, message=f"Hoàn tất {job_id}")

    def worker_loop():
        while not worker_stop.is_set():
            try:
                with worker_lock:
                    api = PixeldrainStore()
                    app_id = api.child(api.root_id, BOT_APP_FOLDER)
                    jobs_id = api.child(app_id, "jobs")
                    rows = api.list_children(jobs_id)
                    pending = None
                    for row in rows:
                        if row.get("type") != "dir":
                            continue
                        manifest = api.get_json(row["path"], "job.json")
                        if manifest and manifest.get("status") in ("QUEUED", "RUNNING"):
                            pending = (row["path"], manifest)
                            break
                    if pending:
                        repo_dir = ensure_novelreader()
                        process_job(api, pending[0], pending[1], repo_dir)
                    else:
                        worker_state.update(status="idle", job_id=None, message="Waiting for a Pixeldrain job")
            except Exception as exc:
                worker_log.exception("Worker cycle error")
                worker_state.update(status="error", message=f"{type(exc).__name__}: {exc}")
            worker_stop.wait(35)


    if os.environ.get("PIXELDRAIN_API_KEY") and not getattr(threading, "_novelreader_worker_started", False):
        thread = threading.Thread(target=worker_loop, name="novelreader-pixeldrain-worker", daemon=True)
        thread.start()
        threading._novelreader_worker_started = True
        worker_state.update(status="waiting", message="Worker nền đã khởi động; đang chờ job Pixeldrain")
    elif not os.environ.get("PIXELDRAIN_API_KEY"):
        worker_state.update(status="needs_setup", message="Set PIXELDRAIN_API_KEY in the MoLab runtime")

    mo.md(f"**Trạng thái worker:** `{worker_state['status']}`  \n{worker_state['message']}  \n**Job:** `{worker_state['job_id'] or '—'}`")
    return


if __name__ == "__main__":
    app.run()
