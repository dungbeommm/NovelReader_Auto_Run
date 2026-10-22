"""Small Google Drive JSON/file store used by the Actions bot and MoLab notebook."""
from __future__ import annotations

import json
import base64
import binascii
import os
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPE = "https://www.googleapis.com/auth/drive"


def credentials_from_json(raw: str):
    value = raw.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.removeprefix("```").removesuffix("```").strip()
        if value.lower().startswith("json"):
            value = value[4:].lstrip()

    try:
        info = json.loads(value)
        if isinstance(info, str):
            info = json.loads(info)
    except json.JSONDecodeError:
        # GitHub Secrets often use base64 to preserve multiline JSON exactly.
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("utf-8-sig")
            info = json.loads(decoded)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "GDRIVE_OAUTH_TOKEN_JSON must be raw Google credential JSON or its Base64 encoding. "
                "Use an authorized-user token JSON with refresh_token, or a service-account JSON; "
                "an API key, file path, or OAuth client-only credentials.json cannot access Drive."
            ) from exc
    if not isinstance(info, dict):
        raise RuntimeError("GDRIVE_OAUTH_TOKEN_JSON must be a JSON object")
    try:
        if info.get("type") == "service_account":
            return service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
        return Credentials.from_authorized_user_info(info, scopes=[SCOPE])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "GDRIVE_OAUTH_TOKEN_JSON is not an authorized-user token or service-account credential. "
            "For OAuth, provide the token JSON containing token, refresh_token, client_id, client_secret and token_uri."
        ) from exc


class DriveStore:
    def __init__(self) -> None:
        token = os.environ.get("GDRIVE_OAUTH_TOKEN_JSON", "").strip()
        if not token:
            raise RuntimeError("GDRIVE_OAUTH_TOKEN_JSON is missing")
        self.credentials = credentials_from_json(token)
        if self.credentials.expired and self.credentials.refresh_token:
            self.credentials.refresh(Request())
        self.api = build("drive", "v3", credentials=self.credentials, cache_discovery=False)
        self.root_id = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "").strip() or "root"

    def _list(self, query: str, fields: str = "files(id,name,mimeType,modifiedTime,size,webViewLink)") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_token = None
        while True:
            result = self.api.files().list(q=query, fields=f"nextPageToken,{fields}", pageSize=100,
                pageToken=page_token, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            rows.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                return rows

    def list_children(self, parent_id: str) -> list[dict[str, Any]]:
        return self._list(f"'{parent_id}' in parents and trashed=false")

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        parent = parent_id or self.root_id
        safe = name.replace("'", "\\'")
        found = self._list(f"'{parent}' in parents and name='{safe}' and mimeType='application/vnd.google-apps.folder' and trashed=false")
        if found:
            return found[0]["id"]
        created = self.api.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]},
                                          fields="id", supportsAllDrives=True).execute()
        return created["id"]

    def _find(self, name: str, parent_id: str) -> dict[str, Any] | None:
        safe = name.replace("'", "\\'")
        rows = self._list(f"'{parent_id}' in parents and name='{safe}' and trashed=false")
        return rows[0] if rows else None

    def put_json(self, parent_id: str, name: str, data: Any) -> str:
        from googleapiclient.http import MediaInMemoryUpload
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        media = MediaInMemoryUpload(body, mimetype="application/json", resumable=True)
        existing = self._find(name, parent_id)
        if existing:
            return self.api.files().update(fileId=existing["id"], media_body=media, fields="id", supportsAllDrives=True).execute()["id"]
        return self.api.files().create(body={"name": name, "parents": [parent_id]}, media_body=media, fields="id", supportsAllDrives=True).execute()["id"]

    def get_json(self, parent_id: str, name: str, default: Any = None) -> Any:
        found = self._find(name, parent_id)
        if not found:
            return default
        content = self.api.files().get_media(fileId=found["id"], supportsAllDrives=True).execute()
        return json.loads(content.decode("utf-8"))

    def upload(self, path: Path, parent_id: str, name: str | None = None) -> dict[str, Any]:
        actual_name = name or path.name
        media = MediaFileUpload(str(path), resumable=True, chunksize=8 * 1024 * 1024)
        existing = self._find(actual_name, parent_id)
        if existing:
            result = self.api.files().update(fileId=existing["id"], media_body=media,
                fields="id,name,size,webViewLink", supportsAllDrives=True).execute()
        else:
            result = self.api.files().create(body={"name": actual_name, "parents": [parent_id]}, media_body=media,
                fields="id,name,size,webViewLink", supportsAllDrives=True).execute()
        return result

    def download(self, file_id: str, target: Path) -> Path:
        request = self.api.files().get_media(fileId=file_id, supportsAllDrives=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            downloader = MediaIoBaseDownload(stream, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return target

    def link(self, file_id: str) -> str:
        return self.api.files().get(fileId=file_id, fields="webViewLink", supportsAllDrives=True).execute()["webViewLink"]

    def share_link_reader(self, file_id: str) -> None:
        self.api.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}, supportsAllDrives=True).execute()
