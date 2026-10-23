"""Google Drive JSON/file store used by Telegram bot, GitHub Actions, and MoLab notebook."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCOPE = ["https://www.googleapis.com/auth/drive"]


class DriveStore:
    def __init__(self, oauth_token_json: str | None = None, root_folder_id: str | None = None) -> None:
        token = (oauth_token_json or os.environ.get("GDRIVE_OAUTH_TOKEN_JSON", "")).strip()
        if not token:
            raise RuntimeError("GDRIVE_OAUTH_TOKEN_JSON is missing")
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        data = json.loads(token)
        self.credentials = Credentials.from_authorized_user_info(data, scopes=SCOPE)
        if self.credentials.expired and self.credentials.refresh_token:
            self.credentials.refresh(Request())
        self.api = build("drive", "v3", credentials=self.credentials, cache_discovery=False)
        self.root_id = (root_folder_id or os.environ.get("GDRIVE_ROOT_FOLDER_ID", "")).strip() or "root"

    def _list(self, query: str, fields: str = "files(id,name,mimeType,modifiedTime,size,webViewLink)") -> list[dict[str, Any]]:
        result = self.api.files().list(
            q=query, fields=fields, pageSize=100, supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        return result.get("files", [])

    def list_children(self, parent_id: str) -> list[dict[str, Any]]:
        rows = self._list(f"'{parent_id}' in parents and trashed=false")
        result = []
        for f in rows:
            is_dir = f.get("mimeType") == "application/vnd.google-apps.folder"
            result.append({
                "name": f["name"],
                "path": f["id"],
                "type": "dir" if is_dir else "file",
                "id": f["id"],
                "webViewLink": f.get("webViewLink", "")
            })
        return result

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
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

    def _find(self, name: str, parent_id: str) -> dict[str, Any] | None:
        safe = name.replace("'", "\\'")
        rows = self._list(f"'{parent_id}' in parents and name='{safe}' and trashed=false")
        return rows[0] if rows else None

    def find(self, parent_id: str, name: str) -> dict[str, Any] | None:
        f = self._find(name, parent_id)
        if not f:
            return None
        is_dir = f.get("mimeType") == "application/vnd.google-apps.folder"
        return {"name": f["name"], "path": f["id"], "type": "dir" if is_dir else "file", "id": f["id"]}

    def child(self, parent_id: str, name: str) -> str:
        f = self._find(name, parent_id)
        if f:
            return f["id"]
        return parent_id.rstrip("/") + "/" + name

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
        from googleapiclient.http import MediaFileUpload
        actual_name = name or path.name
        media = MediaFileUpload(str(path), resumable=True, chunksize=8 * 1024 * 1024)
        existing = self._find(actual_name, parent_id)
        if existing:
            result = self.api.files().update(
                fileId=existing["id"], media_body=media,
                fields="id,name,size,webViewLink", supportsAllDrives=True
            ).execute()
        else:
            result = self.api.files().create(
                body={"name": actual_name, "parents": [parent_id]}, media_body=media,
                fields="id,name,size,webViewLink", supportsAllDrives=True
            ).execute()
        return {"path": result["id"], "id": result["id"], "webViewLink": result.get("webViewLink", "")}

    def download(self, file_id: str, target: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload
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

    def share_link_reader(self, file_id: str) -> str:
        try:
            self.api.permissions().create(
                fileId=file_id, body={"type": "anyone", "role": "reader"}, supportsAllDrives=True
            ).execute()
        except Exception:
            pass
        return self.link(file_id)
