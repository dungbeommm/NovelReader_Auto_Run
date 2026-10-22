"""Select the configured object storage provider."""
import os

from app.pixeldrain import PixeldrainStore
from app.r2 import R2Store


def create_store():
    backend = os.environ.get("STORAGE_BACKEND", "r2").strip().lower()
    if backend == "r2":
        return R2Store()
    if backend == "pixeldrain":
        return PixeldrainStore()
    raise ValueError("STORAGE_BACKEND must be r2 or pixeldrain")
