"""Small Google Drive JSON/file store used by the Actions bot and MoLab notebook."""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPE = "https://www.googleapis.com/auth/drive"


def _credential_info(raw: str) -> dict[str, Any]:
    value = raw.strip()
    if value.startswith("GDRIVE_OAUTH_TOKEN_JSON="):
        value = value.split("=", 1)[1].strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1].strip()
    value = value.strip().lstrip("\ufeff")
    if value.startswith("```") and value.endswith("```"):
        value = value.removeprefix("```").removesuffix("```").strip()
        if value.lower().startswith("json"):
            value = value[4:].lstrip()

    is_data_uri = value.lower().startswith("data:application/json;base64,")
    if is_data_uri:
        value = value.split(",", 1)[1]
    elif value.lower().startswith(("base64:", "base64,")):
        value = value[7:] if value[6] == ":" else value[7:]

    try:
        info = json.loads(value)
    except json.JSONDecodeError:
        info = None
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except json.JSONDecodeError:
            value = info
            info = None
    if isinstance(info, dict):
        return info

    # Base64 can be line-wrapped or URL-safe, depending on how it was copied.
    encoded = "".join(value.split()).strip("\"'")
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        info = json.loads(decoded.decode("utf-8-sig"))
        if isinstance(info, str):
            info = json.loads(info)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            "GDRIVE_OAUTH_TOKEN_JSON is not valid JSON or Base64 JSON. Store the credential file contents "
            "(not its path or an API key); wrapped Base64 and data:application/json;base64 values are supported."
        ) from exc
    if not isinstance(info, dict):
        raise RuntimeError("GDRIVE_OAUTH_TOKEN_JSON must decode to a JSON object")
    return info


def credentials_from_json(raw: str):
    info = _credential_info(raw)
    try:
        if info.get("type") == "service_account":
            return service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
        return Credentials.from_authorized_user_info(info, scopes=[SCOPE])
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(info.get("installed"), dict) or isinstance(info.get("web"), dict):
            raise RuntimeError(
                "GDRIVE_OAUTH_TOKEN_JSON contains OAuth client configuration only. Authorize it once and use the "
                "resulting token JSON with refresh_token, or use a service-account JSON."
            ) from exc
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
