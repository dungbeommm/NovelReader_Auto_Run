"""MoLab notebook worker. Configure Hugging Face bucket access before running."""
import marimo

__generated_with = "0.25.2"
app = marimo.App(width="full")


@app.cell
def _(mo):
    mo.md(r"""
    # NovelReader Auto Run

    Notebook worker cho job Telegram. Worker đọc manifest và đồng bộ checkpoint, kết quả
    vào Hugging Face Storage Bucket `uri410/audio`. Hãy cấu hình `HF_TOKEN` có quyền ghi
    trong runtime MoLab trước khi chạy notebook.
    """)
    return


@app.cell
def _():
    import importlib.util
    import subprocess as _subprocess
    import sys as _sys
    for package in ("requests", "boto3"):
        if importlib.util.find_spec(package) is None:
            _subprocess.run([_sys.executable, "-m", "pip", "install", package], check=True)
    _subprocess.run([_sys.executable, "-m", "pip", "install", "huggingface_hub>=1.6,<2"], check=True)
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
    import zipfile
    from datetime import datetime, timezone
    from pathlib import Path

    import marimo as mo
    import requests
    import boto3
    from botocore.client import Config
    from botocore.exceptions import ClientError
    from huggingface_hub import batch_bucket_files, bucket_info, download_bucket_files, list_bucket_tree
    from huggingface_hub.errors import EntryNotFoundError
    import tempfile
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
            if response.status_code == 403:
                raise RuntimeError(
                    "Pixeldrain Filesystem access denied (HTTP 403). "
                    "The account must have a Pro/Prepaid subscription and the API key must belong to that account."
                )
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

    class R2Store:
        def __init__(self):
            self.bucket = os.environ.get("R2_BUCKET", "").strip()
            account = os.environ.get("R2_ACCOUNT_ID", "").strip()
            key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
            secret = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
            if not all((self.bucket, account, key, secret)):
                raise RuntimeError("R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY are required")
            self.root_id = (os.environ.get("R2_ROOT_PREFIX") or "novelreader").strip("/")
            if not self.root_id or any(p in ("", ".", "..") for p in self.root_id.split("/")):
                raise ValueError("R2_ROOT_PREFIX must contain safe path components")
            self.client = boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                aws_access_key_id=key, aws_secret_access_key=secret, region_name="auto",
                config=Config(signature_version="s3v4"))

        @staticmethod
        def child(parent, name):
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                raise ValueError(f"Invalid R2 path component: {name!r}")
            return parent.rstrip("/") + "/" + name

        def ensure_folder(self, name, parent_id=None):
            return self.child(parent_id or self.root_id, name)

        def list_children(self, parent_id):
            prefix = parent_id.rstrip("/") + "/"
            rows = []
            pages = self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix, Delimiter="/")
            for page in pages:
                for entry in page.get("CommonPrefixes", []):
                    path = entry["Prefix"].rstrip("/")
                    rows.append({"name": path[len(prefix):], "path": path, "type": "dir"})
                for entry in page.get("Contents", []):
                    path = entry["Key"]
                    if path != prefix:
                        rows.append({"name": path[len(prefix):], "path": path, "type": "file"})
            return rows

        def find(self, parent_id, name):
            self.child(parent_id, name)
            return next((row for row in self.list_children(parent_id) if row["name"] == name), None)

        def put_json(self, parent_id, name, data):
            path = self.child(parent_id, name)
            self.client.put_object(Bucket=self.bucket, Key=path,
                Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                ContentType="application/json")
            return path

        def get_json(self, parent_id, name, default=None):
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=self.child(parent_id, name))
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    return default
                raise
            return json.loads(response["Body"].read())

        def upload(self, path, parent_id, name=None):
            target = self.child(parent_id, name or path.name)
            self.client.upload_file(str(path), self.bucket, target)
            return {"path": target}

        def download(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_file(self.bucket, file_id, str(target))
            return target

        def share_link_reader(self, path):
            return self.client.generate_presigned_url("get_object",
                Params={"Bucket": self.bucket, "Key": self.child(path, "result.zip")}, ExpiresIn=604800)

    class HuggingFaceStore:
        def __init__(self):
            self.bucket = (os.environ.get("HF_BUCKET_ID") or "uri410/audio").strip("/")
            self.token = os.environ.get("HF_TOKEN", "").strip()
            self.root_id = (os.environ.get("HF_ROOT_PREFIX") or "novelreader").strip("/")
            if not self.token:
                raise RuntimeError("HF_TOKEN is missing in the MoLab runtime")
            if len(self.bucket.split("/")) != 2 or not all(self.bucket.split("/")):
                raise ValueError("HF_BUCKET_ID must be namespace/bucket")
            if not self.root_id or any(p in ("", ".", "..") or "\\" in p for p in self.root_id.split("/")):
                raise ValueError("HF_ROOT_PREFIX must contain safe path components")
            bucket_info(self.bucket, token=self.token)

        @staticmethod
        def child(parent, name):
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                raise ValueError(f"Invalid bucket path component: {name!r}")
            return parent.rstrip("/") + "/" + name

        def ensure_folder(self, name, parent_id=None):
            return self.child(parent_id or self.root_id, name)

        def list_children(self, parent_id):
            prefix = parent_id.rstrip("/") + "/"
            rows = []
            for item in list_bucket_tree(self.bucket, prefix=prefix, recursive=False, token=self.token):
                path = item.path.rstrip("/")
                if path == prefix.rstrip("/") or not path.startswith(prefix):
                    continue
                rows.append({"name": path[len(prefix):], "path": path,
                    "type": "dir" if item.type == "directory" else "file"})
            return rows

        def find(self, parent_id, name):
            self.child(parent_id, name)
            return next((row for row in self.list_children(parent_id) if row["name"] == name), None)

        def put_json(self, parent_id, name, data):
            path = self.child(parent_id, name)
            batch_bucket_files(self.bucket,
                add=[(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), path)], token=self.token)
            return path

        def get_json(self, parent_id, name, default=None):
            path = self.child(parent_id, name)
            with tempfile.TemporaryDirectory(prefix="novelreader-hf-") as directory:
                target = Path(directory) / name
                try:
                    download_bucket_files(self.bucket, files=[(path, target)],
                        raise_on_missing_files=True, token=self.token)
                except EntryNotFoundError:
                    return default
                return json.loads(target.read_text(encoding="utf-8"))

        def upload(self, path, parent_id, name=None):
            target = self.child(parent_id, name or path.name)
            batch_bucket_files(self.bucket, add=[(path, target)], token=self.token)
            return {"path": target}

        def download(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            download_bucket_files(self.bucket, files=[(file_id, target)],
                raise_on_missing_files=True, token=self.token)
            return target

        def share_link_reader(self, path):
            key = self.child(path, "result.zip")
            encoded = "/".join(quote(segment, safe="") for segment in key.split("/"))
            return f"https://huggingface.co/buckets/{self.bucket}/resolve/{encoded}"

    def create_store():
        backend = os.environ.get("STORAGE_BACKEND", "huggingface").strip().lower()
        if backend in ("huggingface", "hf"):
            return HuggingFaceStore()
        if backend == "r2":
            return R2Store()
        if backend == "pixeldrain":
            return PixeldrainStore()
        raise ValueError("STORAGE_BACKEND must be huggingface, r2 or pixeldrain")

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

    def restore_paths(api, remote_root, local_root):
        def walk(remote, local):
            for row in api.list_children(remote):
                target = local / row["name"]
                if row["type"] == "dir":
                    walk(row["path"], target)
                elif row["type"] == "file":
                    api.download(row["path"], target)
        walk(remote_root, Path(local_root))

    def now_iso():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def ensure_novelreader():
        repo_dir = WORK_ROOT / "NovelReader"
        if not (repo_dir / ".git").exists():
            subprocess.run(["git", "clone", f"https://github.com/{NOVELREADER_REPO}.git", str(repo_dir)], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "fetch", "--depth", "1", "origin", NOVELREADER_COMMIT], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "checkout", "--detach", NOVELREADER_COMMIT], check=True)
        marker = repo_dir / f".auto_run_requirements_installed_{NOVELREADER_COMMIT}"
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
            "noise_scale": a["noise_scale"], "noise_w_scale": a["noise_w_scale"],
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
        source_path = api.download(manifest["input"].get("storage_path", manifest["input"].get("pixeldrain_path")), home / "source.epub")
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != manifest["input"]["sha256"]:
            raise ValueError("SHA-256 EPUB does not match manifest")

        data_root = home / "data"
        data_root.mkdir(exist_ok=True)
        checkpoint = api.find(folder_id, "checkpoint.sqlite3")
        db_path = data_root / "novel.sqlite3"
        if checkpoint:
            api.download(checkpoint["path"], db_path)
            for output_name in ("output_text", "output_audio"):
                restore_paths(api, api.child(folder_id, output_name), data_root / output_name)

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
            seen = {(e.get("stage"), e.get("chapter_id")) for e in manifest["errors"]}
            for chapter in failed_existing:
                if ("translate", chapter["id"]) not in seen:
                    manifest["errors"].append({"chapter_id": chapter["id"], "chapter": chapter["position"],
                        "stage": "translate", "detail": chapter.get("error", "translation error")})
        manifest["status"] = "RUNNING"
        manifest["progress"] = {"total_chapters": len(chapters),
            "done_chapters": sum(c["status"] == "done" for c in chapters),
            "failed_chapters": sum(c["status"] == "error" for c in chapters),
            "last_chapter": max((c["position"] for c in chapters if c["status"] == "done"), default=None)}
        update_manifest(api, folder_id, manifest)

        config = map_config(manifest["config"])
        job = Job(store)
        job.start(config, reset=False)
        last_snapshot = 0.0
        observed = None
        synced = {}
        while job.active:
            time.sleep(5)
            current = store.list()
            signature = tuple((c["id"], c["status"], c.get("audio_status")) for c in current)
            if signature != observed or time.monotonic() - last_snapshot >= 90:
                observed = signature
                backup_database(store, data_root / "checkpoint.sqlite3")
                api.upload(data_root / "checkpoint.sqlite3", folder_id)
                for output_name in ("output_text", "output_audio"):
                    root = data_root / output_name
                    for path in root.rglob("*") if root.exists() else ():
                        if not path.is_file() or path.is_symlink():
                            continue
                        signature_file = (path.stat().st_size, path.stat().st_mtime_ns)
                        if synced.get(str(path)) == signature_file:
                            continue
                        remote = api.child(folder_id, output_name)
                        for segment in path.relative_to(root).parts[:-1]:
                            remote = api.ensure_folder(segment, remote)
                        api.upload(path, remote)
                        synced[str(path)] = signature_file
                progress = {"total_chapters": len(current),
                    "done_chapters": sum(c["status"] == "done" for c in current),
                    "failed_chapters": sum(c["status"] == "error" for c in current),
                    "last_chapter": max((c["position"] for c in current if c["status"] == "done"), default=None)}
                manifest["progress"] = progress
                known = {(e.get("stage"), e.get("chapter_id")) for e in manifest.get("errors", [])}
                for chapter in current:
                    if chapter["status"] == "error" and ("translate", chapter["id"]) not in known:
                        manifest.setdefault("errors", []).append({"chapter_id": chapter["id"], "chapter": chapter["position"],
                            "stage": "translate", "detail": chapter.get("error", "translation error")})
                        known.add(("translate", chapter["id"]))
                    if chapter.get("audio_status") == "error" and ("audio", chapter["id"]) not in known:
                        manifest.setdefault("errors", []).append({"chapter_id": chapter["id"], "chapter": chapter["position"],
                            "stage": "audio", "detail": "TTS thất bại; xem log MoLab"})
                        known.add(("audio", chapter["id"]))
                update_manifest(api, folder_id, manifest)
                last_snapshot = time.monotonic()

        final_chapters = store.list()
        known = {(e.get("stage"), e.get("chapter_id")) for e in manifest.get("errors", [])}
        for chapter in final_chapters:
            if chapter["status"] == "error" and ("translate", chapter["id"]) not in known:
                manifest.setdefault("errors", []).append({"chapter_id": chapter["id"], "chapter": chapter["position"],
                    "stage": "translate", "detail": chapter.get("error", "translation error")})
            if chapter.get("audio_status") == "error" and ("audio", chapter["id"]) not in known:
                manifest.setdefault("errors", []).append({"chapter_id": chapter["id"], "chapter": chapter["position"],
                    "stage": "audio", "detail": "TTS thất bại; xem log MoLab"})
        if job.status == "error" and not any(c["status"] == "error" or c.get("audio_status") == "error" for c in final_chapters):
            manifest.setdefault("errors", []).append({"stage": "worker", "detail": job.message or "NovelReader job failed"})
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
        manifest["status"] = "UPLOADING"
        update_manifest(api, folder_id, manifest)
        result_folder = api.ensure_folder("result", folder_id)
        upload_paths(api, data_root / "output_text", result_folder)
        output_audio = data_root / "output_audio"
        export_audio = manifest["config"]["export"]
        for path in sorted(p for p in output_audio.rglob("*") if p.is_file() and not p.is_symlink()) if output_audio.exists() else ():
            rel = path.relative_to(output_audio)
            is_group = "audio_gop" in rel.parts
            if not is_group and not (export_audio.get("chapter_audio") and export_audio.get("keep_chapter_audio")):
                continue
            dest = result_folder
            for segment in rel.parts[:-1]:
                dest = api.ensure_folder(segment, dest)
            api.upload(path, dest)
        for artifact in (data_root / "translated.txt", data_root / "translated.epub"):
            if artifact.exists():
                api.upload(artifact, result_folder)
        zip_path = data_root / "result.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for output_name in ("output_text", "output_audio"):
                root = data_root / output_name
                if root.exists():
                    for path in root.rglob("*"):
                        if path.is_file() and not path.is_symlink():
                            if output_name == "output_audio":
                                rel = path.relative_to(root)
                                if "audio_gop" not in rel.parts and not (export_audio.get("chapter_audio") and export_audio.get("keep_chapter_audio")):
                                    continue
                            archive.write(path, path.relative_to(data_root))
            for artifact in (data_root / "translated.txt", data_root / "translated.epub"):
                if artifact.exists():
                    archive.write(artifact, artifact.name)
        api.upload(zip_path, result_folder)
        sharing = manifest["config"].get("storage", manifest["config"].get("pixeldrain", {})).get("sharing")
        result_link = api.share_link_reader(result_folder) if sharing == "link_reader" else None
        progress = {"total_chapters": len(final_chapters), "done_chapters": sum(c["status"] == "done" for c in final_chapters),
                    "failed_chapters": sum(c["status"] == "error" for c in final_chapters),
                    "last_chapter": max((c["position"] for c in final_chapters if c["status"] == "done"), default=None)}
        manifest.update(status="COMPLETED_WITH_ERRORS" if manifest.get("errors") or progress["failed_chapters"] else "COMPLETED",
            progress=progress, result={"download_link": result_link, "result_path": result_folder,
                "completed_at": now_iso(), "artifacts": [p.name for p in (data_root / "translated.txt", data_root / "translated.epub") if p.exists()] + ["result.zip"]})
        update_manifest(api, folder_id, manifest)
        worker_state.update(status="idle", job_id=None, message=f"Hoàn tất {job_id}")

    def worker_loop():
        while not worker_stop.is_set():
            try:
                with worker_lock:
                    api = create_store()
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
                        try:
                            repo_dir = ensure_novelreader()
                            process_job(api, pending[0], pending[1], repo_dir)
                        except Exception as exc:
                            worker_log.exception("Job failed: %s", pending[1]["job_id"])
                            manifest = api.get_json(pending[0], "job.json", pending[1])
                            manifest["status"] = "FAILED"
                            manifest.setdefault("errors", []).append({"stage": "worker", "detail": f"{type(exc).__name__}: {exc}"[:2000]})
                            update_manifest(api, pending[0], manifest)
                    else:
                        worker_state.update(status="idle", job_id=None, message="Waiting for a job")
            except Exception as exc:
                worker_log.exception("Worker cycle error")
                worker_state.update(status="error", message=f"{type(exc).__name__}: {exc}")
            worker_stop.wait(35)


    storage_backend = os.environ.get("STORAGE_BACKEND", "huggingface").strip().lower()
    if storage_backend in ("huggingface", "hf"):
        configured = bool(os.environ.get("HF_TOKEN"))
    elif storage_backend == "pixeldrain":
        configured = bool(os.environ.get("PIXELDRAIN_API_KEY"))
    else:
        configured = all(os.environ.get(name) for name in
            ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"))
    if configured and not getattr(threading, "_novelreader_worker_started", False):
        thread = threading.Thread(target=worker_loop, name="novelreader-storage-worker", daemon=True)
        thread.start()
        threading._novelreader_worker_started = True
        worker_state.update(status="waiting", message="Worker nền đã khởi động; đang chờ job")
    elif not configured:
        worker_state.update(status="needs_setup", message="Configure storage credentials in the MoLab runtime")

    mo.md(f"**Trạng thái worker:** `{worker_state['status']}`  \n{worker_state['message']}  \n**Job:** `{worker_state['job_id'] or '—'}`")
    return


if __name__ == "__main__":
    app.run()
