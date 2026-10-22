"""Cloudflare R2 object storage shared by the Telegram bot and worker."""
from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


class R2Store:
    def __init__(self, client=None, bucket: str | None = None, root_id: str | None = None) -> None:
        self.bucket = bucket or os.environ.get("R2_BUCKET", "").strip()
        if not self.bucket:
            raise RuntimeError("R2_BUCKET is missing")
        self.root_id = (root_id or os.environ.get("R2_ROOT_PREFIX") or "novelreader").strip("/")
        if not self.root_id or any(p in ("", ".", "..") for p in self.root_id.split("/")):
            raise ValueError("R2_ROOT_PREFIX must contain safe path components")
        if client is None:
            account = os.environ.get("R2_ACCOUNT_ID", "").strip()
            key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
            secret = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
            if not all((account, key, secret)):
                raise RuntimeError("R2_ACCOUNT_ID, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY are required")
            client = boto3.client(
                "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                aws_access_key_id=key, aws_secret_access_key=secret,
                region_name="auto", config=Config(signature_version="s3v4"),
            )
        self.client = client

    @staticmethod
    def child(parent: str, name: str) -> str:
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError(f"Invalid R2 path component: {name!r}")
        return parent.rstrip("/") + "/" + name

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        return self.child(parent_id or self.root_id, name)

    def list_children(self, parent_id: str) -> list[dict]:
        prefix = parent_id.rstrip("/") + "/"
        rows = []
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix, Delimiter="/"
        )
        for page in pages:
            for entry in page.get("CommonPrefixes", []):
                path = entry["Prefix"].rstrip("/")
                rows.append({"name": path[len(prefix):], "path": path, "type": "dir"})
            for entry in page.get("Contents", []):
                path = entry["Key"]
                if path != prefix:
                    rows.append({"name": path[len(prefix):], "path": path, "type": "file"})
        return rows

    def find(self, parent_id: str, name: str) -> dict | None:
        self.child(parent_id, name)
        return next((row for row in self.list_children(parent_id) if row["name"] == name), None)

    def put_json(self, parent_id: str, name: str, data) -> str:
        path = self.child(parent_id, name)
        self.client.put_object(Bucket=self.bucket, Key=path,
                               Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                               ContentType="application/json")
        return path

    def get_json(self, parent_id: str, name: str, default=None):
        path = self.child(parent_id, name)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=path)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return default
            raise
        return json.loads(response["Body"].read())

    def upload(self, path: Path, parent_id: str, name: str | None = None) -> dict:
        target = self.child(parent_id, name or path.name)
        self.client.upload_file(str(path), self.bucket, target)
        return {"path": target}

    def download(self, file_id: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, file_id, str(target))
        return target

    def share_link_reader(self, path: str) -> str:
        """Give a temporary private download link for result.zip inside a result prefix."""
        key = self.child(path, "result.zip")
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=604800
        )
