"""Hugging Face Storage Bucket adapter for bot and notebook worker."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from huggingface_hub import batch_bucket_files, bucket_info, download_bucket_files, list_bucket_tree
from huggingface_hub.errors import EntryNotFoundError


class HuggingFaceStore:
    def __init__(self, bucket: str | None = None, token: str | None = None,
                 root_id: str | None = None, *, check_bucket: bool = True) -> None:
        self.bucket = (bucket or os.environ.get("HF_BUCKET_ID") or "uri410/audio").strip("/")
        self.token = token or os.environ.get("HF_TOKEN", "").strip()
        self.root_id = (root_id or os.environ.get("HF_ROOT_PREFIX") or "novelreader").strip("/")
        if not self.token:
            raise RuntimeError("HF_TOKEN is missing; create a Hugging Face write token and set it in Actions and MoLab")
        if len(self.bucket.split("/")) != 2 or not all(self.bucket.split("/")):
            raise ValueError("HF_BUCKET_ID must be namespace/bucket, for example uri410/audio")
        if not self.root_id or any(p in ("", ".", "..") or "\\" in p for p in self.root_id.split("/")):
            raise ValueError("HF_ROOT_PREFIX must contain safe path components")
        if check_bucket:
            info = bucket_info(self.bucket, token=self.token)
            self.private = info.private
        else:
            self.private = None

    @staticmethod
    def child(parent: str, name: str) -> str:
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError(f"Invalid bucket path component: {name!r}")
        return parent.rstrip("/") + "/" + name

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        return self.child(parent_id or self.root_id, name)

    def list_children(self, parent_id: str) -> list[dict]:
        prefix = parent_id.rstrip("/") + "/"
        rows = []
        for item in list_bucket_tree(self.bucket, prefix=prefix, recursive=False, token=self.token):
            path = item.path.rstrip("/")
            if path == prefix.rstrip("/") or not path.startswith(prefix):
                continue
            rows.append({"name": path[len(prefix):], "path": path,
                         "type": "dir" if item.type == "directory" else "file"})
        return rows

    def find(self, parent_id: str, name: str) -> dict | None:
        self.child(parent_id, name)
        return next((row for row in self.list_children(parent_id) if row["name"] == name), None)

    def put_json(self, parent_id: str, name: str, data) -> str:
        path = self.child(parent_id, name)
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        batch_bucket_files(self.bucket, add=[(payload, path)], token=self.token)
        return path

    def get_json(self, parent_id: str, name: str, default=None):
        path = self.child(parent_id, name)
        with tempfile.TemporaryDirectory(prefix="novelreader-hf-") as directory:
            target = Path(directory) / name
            try:
                download_bucket_files(self.bucket, files=[(path, target)],
                                      raise_on_missing_files=True, token=self.token)
            except EntryNotFoundError:
                return default
            return json.loads(target.read_text(encoding="utf-8"))

    def upload(self, path: Path, parent_id: str, name: str | None = None) -> dict:
        target = self.child(parent_id, name or path.name)
        batch_bucket_files(self.bucket, add=[(path, target)], token=self.token)
        return {"path": target}

    def download(self, file_id: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        download_bucket_files(self.bucket, files=[(file_id, target)],
                              raise_on_missing_files=True, token=self.token)
        return target

    def share_link_reader(self, path: str) -> str:
        key = self.child(path, "result.zip")
        encoded = "/".join(quote(segment, safe="") for segment in key.split("/"))
        return f"https://huggingface.co/buckets/{self.bucket}/resolve/{encoded}"
