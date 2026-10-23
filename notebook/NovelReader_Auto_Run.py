"""Public MoLab app worker and interactive runner for NovelReader.
Designed for GitHub Actions browser control and direct execution without a MoLab sign-in.
"""
import marimo

__generated_with = "0.25.2"
app = marimo.App(
    width="full",
    app_title="NovelReader AI — MoLab Auto Run Edition"
)


@app.cell
def title_cell(mo):
    mo.md(r"""
    # ⚡ NovelReader AI — MoLab Auto Run Edition

    Hệ thống dịch thuật & tạo audio tự động: **Telegram Bot ➔ GitHub Actions ➔ MoLab App công khai ➔ Hugging Face**.
    
    *App có thể mở công khai không cần đăng nhập. Worker nền nhận job từ Telegram và lưu tiến trình/kết quả lên Hugging Face.*
    """)
    return


@app.cell
def import_marimo():
    import marimo as mo
    return (mo,)


@app.cell
def install_deps():
    import importlib.util
    import subprocess as _subprocess
    import sys as _sys

    packages = ["requests", "boto3", "huggingface_hub>=1.6,<2"]
    for package in packages:
        pkg_name = package.split(">=")[0].split("<")[0]
        if importlib.util.find_spec(pkg_name) is None:
            _subprocess.run([_sys.executable, "-m", "pip", "install", "-q", package], check=False)

    for g_pkg in ["google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib"]:
        pkg_clean = g_pkg.replace("-", "_")
        if importlib.util.find_spec(pkg_clean) is None:
            _subprocess.run([_sys.executable, "-m", "pip", "install", "-q", g_pkg], check=False)

    deps_ready = True
    return (deps_ready,)


@app.cell
def shared_imports(deps_ready):
    assert deps_ready
    import hashlib
    import json
    import logging
    import os
    import shutil
    import sqlite3
    import subprocess
    import sys
    import tempfile
    import threading
    import time
    import traceback
    import uuid
    import zipfile
    from datetime import datetime, timezone
    from pathlib import Path
    from urllib.parse import quote

    import requests
    import boto3
    from botocore.client import Config
    from botocore.exceptions import ClientError
    from huggingface_hub import batch_bucket_files, bucket_info, download_bucket_files, list_bucket_tree
    from huggingface_hub.errors import EntryNotFoundError

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    worker_log = logging.getLogger("novelreader.molab")

    WORK_ROOT = Path("/tmp/novelreader-auto-run")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    NOVELREADER_REPO = "dungbeommm/NovelReader_Translate"
    NOVELREADER_COMMIT = os.getenv("NOVELREADER_COMMIT", "920a1d4464e377d6c03fd53da017f2a00b709b66")
    BOT_APP_FOLDER = "NovelReader_Auto_Run"

    DEFAULT_JOB_CONFIG = {
        "source_language": "zh",
        "target_language": "vi",
        "compute": {"provider": "molab", "device": "auto"},
        "translation": {
            "enabled": True,
            "backend": "ct2",
            "model": "HachimiMT-60-QT",
            "beam_size": 2,
            "chunk_mode": "sentence",
            "normalize_mode": "auto",
            "glossary": "",
            "honorific_mode": "safe",
            "pronoun_harmonizer": True,
        },
        "audio": {
            "enabled": True,
            "engine": "piper",
            "voice": "ngochuyennew",
            "speed": 1.0,
            "format": "mp3",
            "bitrate": "96k",
            "sample_rate": 0,
            "workers": 1,
            "noise_scale": 0.667,
            "noise_w_scale": 0.8,
            "vieneu_temperature": 0.8,
            "vieneu_top_p": 0.95,
            "vieneu_batch_size": 8,
        },
        "merge": {"enabled": True, "chapters_per_file": 50},
        "export": {"txt": True, "epub": True, "chapter_audio": True, "keep_chapter_audio": True},
        "storage": {"sharing": "link_reader"},
    }

    return (
        BOT_APP_FOLDER,
        ClientError,
        Config,
        DEFAULT_JOB_CONFIG,
        EntryNotFoundError,
        NOVELREADER_COMMIT,
        NOVELREADER_REPO,
        Path,
        WORK_ROOT,
        batch_bucket_files,
        boto3,
        bucket_info,
        datetime,
        download_bucket_files,
        hashlib,
        json,
        list_bucket_tree,
        logging,
        os,
        quote,
        requests,
        shutil,
        sqlite3,
        subprocess,
        sys,
        tempfile,
        threading,
        time,
        timezone,
        traceback,
        uuid,
        worker_log,
        zipfile,
    )


@app.cell
def storage_classes(
    ClientError,
    Config,
    EntryNotFoundError,
    Path,
    batch_bucket_files,
    boto3,
    bucket_info,
    download_bucket_files,
    json,
    list_bucket_tree,
    os,
    quote,
    requests,
    tempfile,
):
    class PixeldrainStore:
        def __init__(self, api_key: str | None = None, root_id: str | None = None) -> None:
            self.session = requests.Session()
            key = api_key or os.environ.get("PIXELDRAIN_API_KEY", "").strip()
            if not key:
                raise RuntimeError("PIXELDRAIN_API_KEY is missing")
            self.session.auth = ("", key)
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
            node = self._request("POST", path, files={"action": (None, "update"), "shared": (None, "true")}).json()
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

    class DriveStore:
        def __init__(self):
            token = os.environ.get("GDRIVE_OAUTH_TOKEN_JSON", "").strip()
            if not token:
                raise RuntimeError("GDRIVE_OAUTH_TOKEN_JSON is missing")
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            data = json.loads(token)
            self.credentials = Credentials.from_authorized_user_info(data, scopes=["https://www.googleapis.com/auth/drive"])
            if self.credentials.expired and self.credentials.refresh_token:
                self.credentials.refresh(Request())
            self.api = build("drive", "v3", credentials=self.credentials, cache_discovery=False)
            self.root_id = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "").strip() or "root"

        def _list(self, query):
            return self.api.files().list(
                q=query, fields="files(id,name,mimeType,webViewLink)", pageSize=100,
                supportsAllDrives=True, includeItemsFromAllDrives=True
            ).execute().get("files", [])

        def list_children(self, parent_id):
            rows = self._list(f"'{parent_id}' in parents and trashed=false")
            return [{"name": f["name"], "path": f["id"], "type": "dir" if f.get("mimeType") == "application/vnd.google-apps.folder" else "file", "id": f["id"]} for f in rows]

        def ensure_folder(self, name, parent_id=None):
            parent = parent_id or self.root_id
            safe = name.replace("'", "\\'")
            found = self._list(f"'{parent}' in parents and name='{safe}' and mimeType='application/vnd.google-apps.folder' and trashed=false")
            if found:
                return found[0]["id"]
            created = self.api.files().create(
                body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]},
                fields="id", supportsAllDrives=True
            ).execute()
            return created["id"]

        def child(self, parent_id, name):
            return parent_id.rstrip("/") + "/" + name

        def find(self, parent_id, name):
            safe = name.replace("'", "\\'")
            rows = self._list(f"'{parent_id}' in parents and name='{safe}' and trashed=false")
            if not rows:
                return None
            f = rows[0]
            return {"name": f["name"], "path": f["id"], "type": "dir" if f.get("mimeType") == "application/vnd.google-apps.folder" else "file", "id": f["id"]}

        def put_json(self, parent_id, name, data):
            from googleapiclient.http import MediaInMemoryUpload
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            media = MediaInMemoryUpload(body, mimetype="application/json", resumable=True)
            existing = self.find(parent_id, name)
            if existing:
                return self.api.files().update(fileId=existing["id"], media_body=media, fields="id", supportsAllDrives=True).execute()["id"]
            return self.api.files().create(body={"name": name, "parents": [parent_id]}, media_body=media, fields="id", supportsAllDrives=True).execute()["id"]

        def get_json(self, parent_id, name, default=None):
            found = self.find(parent_id, name)
            if not found:
                return default
            content = self.api.files().get_media(fileId=found["id"], supportsAllDrives=True).execute()
            return json.loads(content.decode("utf-8"))

        def upload(self, path, parent_id, name=None):
            from googleapiclient.http import MediaFileUpload
            actual_name = name or path.name
            media = MediaFileUpload(str(path), resumable=True, chunksize=8 * 1024 * 1024)
            existing = self.find(parent_id, actual_name)
            if existing:
                res = self.api.files().update(fileId=existing["id"], media_body=media, fields="id,name,webViewLink", supportsAllDrives=True).execute()
            else:
                res = self.api.files().create(body={"name": actual_name, "parents": [parent_id]}, media_body=media, fields="id,name,webViewLink", supportsAllDrives=True).execute()
            return {"path": res["id"], "id": res["id"], "webViewLink": res.get("webViewLink", "")}

        def download(self, file_id, target):
            from googleapiclient.http import MediaIoBaseDownload
            request = self.api.files().get_media(fileId=file_id, supportsAllDrives=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as stream:
                downloader = MediaIoBaseDownload(stream, request, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return target

        def share_link_reader(self, folder_id):
            result_zip = self.find(folder_id, "result.zip")
            file_id = result_zip["id"] if result_zip else folder_id
            try:
                self.api.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}, supportsAllDrives=True).execute()
            except Exception:
                pass
            return self.api.files().get(fileId=file_id, fields="webViewLink", supportsAllDrives=True).execute().get("webViewLink", "")

    class LocalStore:
        def __init__(self, base_dir: Path | None = None):
            self.root_id = str(base_dir or Path("/tmp/novelreader-local-store"))
            Path(self.root_id).mkdir(parents=True, exist_ok=True)

        @staticmethod
        def child(parent, name):
            return str(Path(parent) / name)

        def ensure_folder(self, name, parent_id=None):
            p = Path(parent_id or self.root_id) / name
            p.mkdir(parents=True, exist_ok=True)
            return str(p)

        def list_children(self, parent_id):
            p = Path(parent_id)
            if not p.exists():
                return []
            return [{"name": f.name, "path": str(f), "type": "dir" if f.is_dir() else "file"} for f in p.iterdir()]

        def find(self, parent_id, name):
            target = Path(parent_id) / name
            if not target.exists():
                return None
            return {"name": target.name, "path": str(target), "type": "dir" if target.is_dir() else "file"}

        def put_json(self, parent_id, name, data):
            p = Path(parent_id) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(p)

        def get_json(self, parent_id, name, default=None):
            p = Path(parent_id) / name
            if not p.exists():
                return default
            return json.loads(p.read_text(encoding="utf-8"))

        def upload(self, path, parent_id, name=None):
            dest = Path(parent_id) / (name or path.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(dest))
            return {"path": str(dest)}

        def download(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_id, str(target))
            return target

        def share_link_reader(self, path):
            zip_target = Path(path) / "result.zip"
            return f"file://{zip_target.resolve()}"

    def create_store(backend: str | None = None):
        selected = (backend or os.environ.get("STORAGE_BACKEND", "huggingface")).strip().lower()
        if selected in ("huggingface", "hf"):
            return HuggingFaceStore()
        if selected in ("gdrive", "drive", "google_drive"):
            return DriveStore()
        if selected == "r2":
            return R2Store()
        if selected == "pixeldrain":
            return PixeldrainStore()
        if selected == "local":
            return LocalStore()
        raise ValueError(f"STORAGE_BACKEND '{selected}' must be huggingface, gdrive, r2, pixeldrain, or local")

    return DriveStore, HuggingFaceStore, LocalStore, PixeldrainStore, R2Store, create_store


@app.cell
def pipeline_helpers(
    NOVELREADER_COMMIT,
    NOVELREADER_REPO,
    Path,
    WORK_ROOT,
    datetime,
    os,
    sqlite3,
    subprocess,
    sys,
    timezone,
    worker_log,
):
    def now_iso():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

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

    def backup_database(store, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(store.path)) as source, sqlite3.connect(str(target)) as destination:
            source.backup(destination)

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
        if str(repo_dir) not in sys.path:
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

    def update_manifest(api, folder_id, manifest):
        manifest["updated_at"] = now_iso()
        api.put_json(folder_id, "job.json", manifest)

    return (
        backup_database,
        ensure_novelreader,
        map_config,
        now_iso,
        restore_paths,
        update_manifest,
        upload_paths,
    )


@app.cell
def execution_core(
    WORK_ROOT,
    backup_database,
    ensure_novelreader,
    hashlib,
    map_config,
    now_iso,
    os,
    restore_paths,
    time,
    traceback,
    update_manifest,
    upload_paths,
    worker_log,
    zipfile,
):
    def process_job_pipeline(api, folder_id, manifest, progress_callback=None):
        job_id = manifest["job_id"]
        if progress_callback:
            progress_callback(status="RUNNING", message="Đang khởi tạo NovelReader core và tải truyện", progress=0, done=0, total=0)
        repo_dir = ensure_novelreader()
        home = WORK_ROOT / job_id
        home.mkdir(parents=True, exist_ok=True)
        
        orig_name = manifest["input"].get("name", "source.epub")
        ext = ".txt" if orig_name.lower().endswith(".txt") else ".epub"
        source_target = home / f"source{ext}"

        raw_storage_path = manifest["input"].get("storage_path") or manifest["input"].get("pixeldrain_path")
        if raw_storage_path and os.path.exists(raw_storage_path):
            source_path = source_target
            if str(Path(raw_storage_path).resolve()) != str(source_target.resolve()):
                source_target.write_bytes(Path(raw_storage_path).read_bytes())
        else:
            source_path = api.download(raw_storage_path, source_target)

        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if manifest["input"].get("sha256") and actual_hash != manifest["input"]["sha256"]:
            raise ValueError("SHA-256 truyện gốc không khớp với manifest")

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
        total_chapters = len(chapters)
        manifest["status"] = "RUNNING"
        manifest["progress"] = {
            "total_chapters": total_chapters,
            "done_chapters": sum(c["status"] == "done" for c in chapters),
            "failed_chapters": sum(c["status"] == "error" for c in chapters),
            "last_chapter": max((c["position"] for c in chapters if c["status"] == "done"), default=None),
        }
        update_manifest(api, folder_id, manifest)

        config = map_config(manifest["config"])
        job = Job(store)
        job.start(config, reset=False)

        last_snapshot = 0.0
        observed = None
        synced = {}

        while job.active:
            time.sleep(3)
            current = store.list()
            done_count = sum(c["status"] == "done" for c in current)
            failed_count = sum(c["status"] == "error" for c in current)
            pct = int((done_count / total_chapters * 100)) if total_chapters else 0
            
            if progress_callback:
                progress_callback(status="RUNNING", message=f"Đang dịch & tạo audio: {done_count}/{total_chapters} chương", progress=pct, done=done_count, total=total_chapters)

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

                manifest["progress"] = {
                    "total_chapters": total_chapters,
                    "done_chapters": done_count,
                    "failed_chapters": failed_count,
                    "last_chapter": max((c["position"] for c in current if c["status"] == "done"), default=None),
                }
                update_manifest(api, folder_id, manifest)
                last_snapshot = time.monotonic()

        final_chapters = store.list()
        done_final = sum(c["status"] == "done" for c in final_chapters)
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
        if progress_callback:
            progress_callback(status="UPLOADING", message="Đang nén kết quả và đồng bộ lên Cloud Storage...", progress=95, done=done_final, total=total_chapters)
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
        sharing = manifest["config"].get("storage", {}).get("sharing", "link_reader")
        result_link = api.share_link_reader(result_folder) if sharing == "link_reader" else None

        progress_summary = {
            "total_chapters": total_chapters,
            "done_chapters": done_final,
            "failed_chapters": sum(c["status"] == "error" for c in final_chapters),
            "last_chapter": max((c["position"] for c in final_chapters if c["status"] == "done"), default=None),
        }
        final_status = "COMPLETED_WITH_ERRORS" if manifest.get("errors") or progress_summary["failed_chapters"] else "COMPLETED"
        manifest.update(
            status=final_status,
            progress=progress_summary,
            result={
                "download_link": result_link,
                "result_path": result_folder,
                "completed_at": now_iso(),
                "artifacts": [p.name for p in (data_root / "translated.txt", data_root / "translated.epub") if p.exists()] + ["result.zip"]
            }
        )
        update_manifest(api, folder_id, manifest)
        
        if progress_callback:
            progress_callback(status=final_status, message="Hoàn thành xuất sắc!", progress=100, done=done_final, total=total_chapters, result_link=result_link)
        return result_link, manifest

    return (process_job_pipeline,)


@app.cell
def ui_controls(DEFAULT_JOB_CONFIG, json, mo):
    file_uploader = mo.ui.file(
        filetypes=[".txt", ".epub"],
        multiple=False,
        label="📂 1. Chọn file truyện gốc (.txt hoặc .epub):"
    )

    config_input = mo.ui.text_area(
        value=json.dumps(DEFAULT_JOB_CONFIG, indent=2, ensure_ascii=False),
        rows=10,
        label="⚙️ 2. Cấu hình xử lý (JSON):"
    )

    storage_dropdown = mo.ui.dropdown(
        options={
            "Hugging Face Storage Bucket": "huggingface",
            "Google Drive (OAuth Token)": "gdrive",
            "Cloudflare R2 (S3 API)": "r2",
            "Pixeldrain Filesystem": "pixeldrain",
            "Tải trực tiếp ZIP (Local Session)": "local",
        },
        value="Hugging Face Storage Bucket",
        label="☁️ 3. Dịch vụ lưu trữ kết quả:"
    )

    run_btn = mo.ui.run_button(
        label="🚀 BẮT ĐẦU CHẠY NOVELREADER (RUN)",
        kind="success"
    )

    stop_btn = mo.ui.button(
        label="🛑 DỪNG PHIÊN MOLAB (STOP)",
        kind="danger"
    )

    return (
        config_input,
        file_uploader,
        run_btn,
        stop_btn,
        storage_dropdown,
    )


@app.cell
def render_dashboard(
    config_input,
    file_uploader,
    mo,
    run_btn,
    stop_btn,
    storage_dropdown,
):
    ui_dom = mo.Html(f"""
    <div id="novelreader-container" style="border: 1px solid #e0e0e0; border-radius: 8px; padding: 18px; margin-bottom: 20px; background: #fafafa;">
        <h3 style="margin-top: 0; color: #1976d2;">🎛️ Bảng điều khiển NovelReader MoLab</h3>
        <p style="color: #666; font-size: 14px;">
            Hệ thống hỗ trợ tự động hóa thông qua Browser Playwright hoặc thao tác trực tiếp.
        </p>
        <div id="novelreader-file-input" style="margin-bottom: 14px;">
            {file_uploader}
        </div>
        <div id="novelreader-storage-select" style="margin-bottom: 14px;">
            {storage_dropdown}
        </div>
        <div id="novelreader-config-input" style="margin-bottom: 14px;">
            {config_input}
        </div>
        <div style="display: flex; gap: 12px; margin-top: 16px;">
            <div id="novelreader-run-btn">
                {run_btn}
            </div>
            <div id="novelreader-stop-btn">
                {stop_btn}
            </div>
        </div>
    </div>
    """)
    mo.output.replace(ui_dom)
    return (ui_dom,)


@app.cell
def execution_and_background_worker(
    BOT_APP_FOLDER,
    DEFAULT_JOB_CONFIG,
    WORK_ROOT,
    backend_selector=None,
    config_input=None,
    create_store=None,
    file_uploader=None,
    hashlib=None,
    json=None,
    mo=None,
    now_iso=None,
    os=None,
    process_job_pipeline=None,
    run_btn=None,
    stop_btn=None,
    storage_dropdown=None,
    threading=None,
    uuid=None,
    worker_log=None,
):
    selected_backend = storage_dropdown.value if storage_dropdown else "huggingface"
    runner_state = {
        "status": "IDLE",
        "progress": 0,
        "done": 0,
        "total": 0,
        "message": "Sẵn sàng nhận lệnh chạy",
        "result_link": "",
        "error": "",
    }

    if stop_btn and stop_btn.value:
        runner_state.update(status="STOPPED", message="Đã dừng phiên MoLab theo yêu cầu.")
        status_html = mo.Html(f"""
        <div id="novelreader-status" data-status="STOPPED" data-progress="0" data-done="0" data-total="0" data-result-link="" style="padding: 12px; background: #ffebee; border-left: 4px solid #f44336; border-radius: 4px;">
            <strong>Phiên MoLab đã ngưng hoạt động.</strong>
        </div>
        """)
        mo.output.append(status_html)

    elif run_btn and run_btn.value:
        try:
            uploaded_file = file_uploader.value if file_uploader else None
            file_bytes = None
            filename = "source.txt"

            if uploaded_file and len(uploaded_file) > 0:
                first_file = uploaded_file[0]
                filename = first_file.name
                file_bytes = first_file.contents
            else:
                for candidate in (WORK_ROOT / "source.txt", WORK_ROOT / "source.epub"):
                    if candidate.exists():
                        filename = candidate.name
                        file_bytes = candidate.read_bytes()
                        break

            if not file_bytes:
                runner_state.update(status="FAILED", error="Vui lòng tải lên file truyện gốc (.txt hoặc .epub)")
            else:
                config_data = json.loads(config_input.value) if config_input and config_input.value else DEFAULT_JOB_CONFIG
                job_id = str(uuid.uuid4())
                api = create_store(selected_backend)
                app_folder = api.ensure_folder(BOT_APP_FOLDER)
                jobs_folder = api.ensure_folder("jobs", app_folder)
                folder_id = api.ensure_folder(job_id, jobs_folder)

                ext = ".txt" if filename.lower().endswith(".txt") else ".epub"
                temp_local = WORK_ROOT / f"{job_id}{ext}"
                temp_local.write_bytes(file_bytes)
                uploaded_meta = api.upload(temp_local, folder_id, f"source{ext}")

                manifest = {
                    "schema_version": 1,
                    "job_id": job_id,
                    "owner_id": "browser_runner",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "status": "QUEUED",
                    "input": {
                        "storage_path": uploaded_meta.get("path") or str(temp_local),
                        "name": filename,
                        "sha256": hashlib.sha256(file_bytes).hexdigest(),
                        "size": len(file_bytes),
                    },
                    "config": config_data,
                    "progress": {"total_chapters": 0, "done_chapters": 0, "failed_chapters": 0, "last_chapter": None},
                    "errors": [],
                    "result": None,
                }
                api.put_json(folder_id, "job.json", manifest)

                def _cb(status, message, progress, done, total, result_link=None):
                    runner_state.update(status=status, message=message, progress=progress, done=done, total=total)
                    if result_link:
                        runner_state["result_link"] = result_link

                res_link, fin_manifest = process_job_pipeline(api, folder_id, manifest, progress_callback=_cb)
                runner_state.update(status=fin_manifest["status"], result_link=res_link or "", progress=100)
        except Exception as exc:
            worker_log.exception("Interactive job execution failed")
            runner_state.update(status="FAILED", error=str(exc))

    else:
        # Background worker polling loop if credentials are configured
        storage_backend = os.environ.get("STORAGE_BACKEND", "huggingface").strip().lower()
        if storage_backend in ("huggingface", "hf"):
            has_token = bool(os.environ.get("HF_TOKEN"))
        elif storage_backend in ("gdrive", "drive", "google_drive"):
            has_token = bool(os.environ.get("GDRIVE_OAUTH_TOKEN_JSON"))
        elif storage_backend == "r2":
            has_token = all(os.environ.get(name) for name in (
                "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"
            ))
        elif storage_backend == "pixeldrain":
            has_token = bool(os.environ.get("PIXELDRAIN_API_KEY"))
        else:
            has_token = False
        if has_token:
            try:
                probe_api = create_store(storage_backend)
                if storage_backend in ("huggingface", "hf"):
                    probe_folder = probe_api.ensure_folder(BOT_APP_FOLDER)
                    probe_value = {"ok": True}
                    probe_api.put_json(probe_folder, "runtime_healthcheck.json", probe_value)
                    if probe_api.get_json(probe_folder, "runtime_healthcheck.json") != probe_value:
                        raise RuntimeError("Hugging Face write/read health check returned unexpected data")
            except Exception as err:
                has_token = False
                worker_log.exception("Storage startup health check failed")
                runner_state.update(
                    status="FAILED",
                    error=f"Storage startup check failed ({type(err).__name__}): {err}",
                )
        if has_token and not getattr(threading, "_novelreader_auto_worker_started", False):
            def _bg_loop():
                worker_log.info("Background storage poller started.")
                while True:
                    try:
                        api = create_store(storage_backend)
                        app_id = api.child(api.root_id, BOT_APP_FOLDER)
                        jobs_id = api.child(app_id, "jobs")
                        rows = api.list_children(jobs_id)
                        pending = None
                        for row in rows:
                            if row.get("type") != "dir":
                                continue
                            m = api.get_json(row["path"], "job.json")
                            if m and m.get("status") in ("QUEUED", "RUNNING"):
                                pending = (row["path"], m)
                                break
                        if pending:
                            worker_log.info(f"Processing job {pending[1]['job_id']}")
                            try:
                                process_job_pipeline(api, pending[0], pending[1])
                            except Exception as err:
                                worker_log.exception("Job %s failed", pending[1].get("job_id"))
                                failed_manifest = pending[1]
                                failed_manifest["status"] = "FAILED"
                                failed_manifest["updated_at"] = now_iso()
                                failed_manifest.setdefault("errors", []).append({
                                    "stage": "worker",
                                    "detail": f"{type(err).__name__}: {err}"[-3000:],
                                })
                                try:
                                    api.put_json(pending[0], "job.json", failed_manifest)
                                except Exception:
                                    worker_log.exception(
                                        "Could not persist FAILED status for job %s",
                                        failed_manifest.get("job_id"),
                                    )
                    except Exception as err:
                        worker_log.error(f"Worker cycle error: {err}")
                    time.sleep(30)

            t = threading.Thread(target=_bg_loop, name="novelreader-bg-poller", daemon=True)
            t.start()
            threading._novelreader_auto_worker_started = True
            runner_state.update(status="LISTENING", message="Công nhân nền đang lắng nghe job từ Telegram...")
        elif not has_token and runner_state["status"] != "FAILED":
            runner_state.update(
                status="FAILED",
                error=f"Storage credentials are missing for backend '{storage_backend}'. "
                      "Configure HF_TOKEN in the MoLab runtime.",
            )

    cur_status = runner_state["status"]
    cur_prog = runner_state["progress"]
    cur_done = runner_state["done"]
    cur_tot = runner_state["total"]
    cur_msg = runner_state["message"]
    cur_link = runner_state["result_link"]
    cur_err = runner_state["error"]
    safe_err = str(cur_err).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

    status_color = "#4caf50" if "COMPLETED" in cur_status else ("#f44336" if "FAIL" in cur_status else "#2196f3")

    dashboard_html = mo.Html(f"""
    <div id="novelreader-status" 
         data-status="{cur_status}" 
         data-progress="{cur_prog}" 
         data-done="{cur_done}" 
         data-total="{cur_tot}" 
         data-result-link="{cur_link}"
         data-error="{safe_err}"
         style="margin-top: 16px; padding: 16px; border: 1px solid #ccc; border-radius: 8px; background: #fff;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-weight: bold; font-size: 16px;">Trạng thái tiến trình:</span>
            <span style="background: {status_color}; color: white; padding: 4px 12px; border-radius: 12px; font-weight: bold; font-size: 14px;">
                {cur_status}
            </span>
        </div>
        <p style="margin: 6px 0; color: #444;">{cur_msg}</p>
        <div style="background: #e0e0e0; border-radius: 6px; height: 16px; overflow: hidden; margin-top: 8px;">
            <div style="background: {status_color}; height: 100%; width: {cur_prog}%; transition: width 0.4s ease;"></div>
        </div>
        <div style="margin-top: 6px; font-size: 13px; color: #666; display: flex; justify-content: space-between;">
            <span>Tiến độ: {cur_done}/{cur_tot} chương ({cur_prog}%)</span>
        </div>
        {f'<div id="novelreader-result" style="margin-top: 14px; padding: 12px; background: #e8f5e9; border: 1px solid #a5d6a7; border-radius: 6px;"><strong style="color: #2e7d32;">🎉 Kết quả đã sẵn sàng:</strong> <a id="novelreader-download-link" href="{cur_link}" target="_blank" style="font-weight: bold; color: #1565c0; word-break: break-all;">{cur_link}</a></div>' if cur_link else ''}
        {f'<div style="margin-top: 12px; padding: 10px; background: #ffebee; color: #c62828; border-radius: 4px;"><strong>Lỗi:</strong> {safe_err}</div>' if cur_err else ''}
    </div>
    """)
    mo.output.append(dashboard_html)
    return (runner_state,)


if __name__ == "__main__":
    app.run()
