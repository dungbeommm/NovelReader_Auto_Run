"""Path-based Pixeldrain Filesystem storage for the bot and notebook worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

import requests


def require_api_key() -> str:
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
