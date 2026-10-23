"""Select the configured object storage provider."""
import os

from app.pixeldrain import PixeldrainStore
from app.r2 import R2Store
from app.huggingface import HuggingFaceStore


def create_store():
    backend = os.environ.get("STORAGE_BACKEND", "huggingface").strip().lower()
    if backend in ("huggingface", "hf"):
        return HuggingFaceStore()
    if backend == "r2":
        return R2Store()
    if backend == "pixeldrain":
        return PixeldrainStore()
    if backend in ("gdrive", "drive", "google_drive"):
        from app.drive import DriveStore
        return DriveStore()
    raise ValueError("STORAGE_BACKEND must be huggingface, gdrive, r2 or pixeldrain")
