"""Playwright-based browser controller for automating NovelReader on MoLab (https://molab.marimo.io).

Implements Steps 4, 5, 6, 10, 11, 12 of the architecture:
- Starts Chromium browser (headless or headed).
- Opens MoLab notebook URL.
- Uploads story file (.txt or .epub).
- Enters configuration JSON.
- Clicks Run button.
- Polls real-time progress via DOM data attributes (#novelreader-status).
- Extracts public download link upon completion.
- Sends notification to Telegram Bot.
- Gracefully stops MoLab runtime session to release compute resources.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("novelreader.browser")
PROFILE_OBJECT_NAME = "molab-profile.enc"


def _profile_store():
    from app.storage import create_store

    store = create_store()
    root = store.ensure_folder("NovelReader_Auto_Run")
    auth = store.ensure_folder("auth", root)
    return store, auth


def _sync_profile(profile_dir: Path, *, download: bool = False) -> bool:
    """Store only an authenticated encrypted Chromium profile in the HF bucket."""
    from cryptography.fernet import Fernet

    key = os.getenv("MOLAB_PROFILE_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("Set Actions secret MOLAB_PROFILE_ENCRYPTION_KEY to a Fernet key")
    cipher = Fernet(key.encode("ascii"))
    store, auth_folder = _profile_store()
    profile_dir.mkdir(parents=True, exist_ok=True)
    if download:
        with tempfile.TemporaryDirectory(prefix="novelreader-profile-") as tmp:
            encrypted = Path(tmp) / PROFILE_OBJECT_NAME
            try:
                store.download(store.child(auth_folder, PROFILE_OBJECT_NAME), encrypted)
            except Exception as exc:
                # A missing object is expected on first setup; all other errors should fail closed.
                from huggingface_hub.errors import EntryNotFoundError
                if isinstance(exc, EntryNotFoundError):
                    logger.info("No saved HF browser profile yet; using local profile directory.")
                    return False
                raise
            archive = Path(tmp) / "profile.zip"
            try:
                archive.write_bytes(cipher.decrypt(encrypted.read_bytes()))
            except Exception as exc:
                raise RuntimeError("Cannot decrypt saved MoLab profile; check MOLAB_PROFILE_ENCRYPTION_KEY") from exc
            resolved = profile_dir.resolve()
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    target = (resolved / member.filename).resolve()
                    if target != resolved and resolved not in target.parents:
                        raise RuntimeError("Unsafe path in encrypted browser profile archive")
                bundle.extractall(resolved)
        logger.info("Restored encrypted MoLab profile from Hugging Face.")
        return True

    excluded_dirs = {"Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "Service Worker"}
    with tempfile.TemporaryDirectory(prefix="novelreader-profile-") as tmp:
        archive = Path(tmp) / "profile.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in profile_dir.rglob("*"):
                if not path.is_file() or any(part in excluded_dirs for part in path.relative_to(profile_dir).parts):
                    continue
                if path.name.endswith((".lock", ".sock")) or path.name in {"SingletonLock", "SingletonSocket", "SingletonCookie"}:
                    continue
                bundle.write(path, path.relative_to(profile_dir).as_posix())
        encrypted = Path(tmp) / PROFILE_OBJECT_NAME
        encrypted.write_bytes(cipher.encrypt(archive.read_bytes()))
        store.upload(encrypted, auth_folder, PROFILE_OBJECT_NAME)
    logger.info("Saved encrypted MoLab browser profile to Hugging Face.")
    return True


async def _notebook_surface(page, timeout_ms: int = 90000):
    """Find the notebook frame, or use the page itself for a public /app URL."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for frame in page.frames:
            if "preview.sb.molab.run" in frame.url:
                return frame
            try:
                if await frame.locator(
                    "#novelreader-container, .marimo-app, [data-marimo-app]"
                ).count():
                    return frame
            except Exception:
                continue
        await asyncio.sleep(1)
    return page


async def _interactive_login(p, notebook_url: str, profile_dir: Path) -> None:
    """Show the first-login/re-authentication window on the interactive Windows runner."""
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir.resolve()),
        headless=False,
        viewport={"width": 1440, "height": 900},
    )
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
        print("Sign in to MoLab in the visible Chromium window. The job will continue automatically.", flush=True)
        await _notebook_surface(page, timeout_ms=15 * 60 * 1000)
        if not any("preview.sb.molab.run" in frame.url for frame in page.frames):
            raise RuntimeError("MoLab sign-in was not completed before timeout; rerun the Telegram job.")
        surface = await _notebook_surface(page)
        await surface.wait_for_selector("#novelreader-container, .marimo-app, [data-marimo-app]", timeout=180000)
    finally:
        await context.close()
    _sync_profile(profile_dir, download=False)


async def run_molab_automation(
    notebook_url: str,
    file_path: Path | None,
    config_data: dict[str, Any] | None,
    storage_backend: str = "huggingface",
    headless: bool = True,
    poll_interval: int = 15,
    max_duration_seconds: int = 18000,  # 5 hours max
    telegram_bot_token: str | None = None,
    telegram_chat_id: int | None = None,
    profile_dir: Path | None = None,
    job_id: str | None = None,
    sync_profile: bool = False,
) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright chưa được cài đặt. Chạy: pip install playwright && playwright install chromium")
        raise RuntimeError("Playwright is required for browser automation")

    logger.info(f"Khởi động trình duyệt Chromium tự động (headless={headless})...")
    profile_available = bool(sync_profile and profile_dir and _sync_profile(profile_dir, download=True))
    async with async_playwright() as p:
        if sync_profile and profile_dir and not profile_available:
            await _interactive_login(p, notebook_url, profile_dir)
            profile_available = True
        launch_options = {
            "headless": headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
            ],
        }
        if profile_dir:
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir.resolve()),
                viewport={"width": 1920, "height": 1080},
                **launch_options,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            browser = None
        else:
            browser = await p.chromium.launch(**launch_options)
            context = await browser.new_context(viewport={"width": 1920, "height": 1080})
            page = await context.new_page()

        logger.info(f"Đang điều hướng tới MoLab Notebook: {notebook_url}")
        try:
            await page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:
            logger.warning(f"Điều hướng ban đầu: {exc}. Đang thử đợi nội dung render...")

        surface = await _notebook_surface(page, timeout_ms=90000 if profile_available else 15 * 60 * 1000)
        public_app_url = page.url.rstrip("/").endswith("/app")
        preview_loaded = any("preview.sb.molab.run" in frame.url for frame in page.frames)
        if public_app_url and not preview_loaded:
            surface = page
            try:
                await surface.wait_for_selector(
                    "#novelreader-container, .marimo-app, [data-marimo-app]", timeout=30000
                )
                preview_loaded = True
            except Exception:
                logger.warning("Public MoLab app did not expose the NovelReader UI yet.")
        if sync_profile and profile_dir and not preview_loaded:
            await context.close()
            await _interactive_login(p, notebook_url, profile_dir)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir.resolve()),
                viewport={"width": 1920, "height": 1080},
                **launch_options,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
            surface = await _notebook_surface(page, timeout_ms=90000)
            preview_loaded = any("preview.sb.molab.run" in frame.url for frame in page.frames)
        logger.info("Chờ giao diện NovelReader hoặc Marimo app tải...")
        try:
            await surface.wait_for_selector("#novelreader-container, .marimo-app, [data-marimo-app]", timeout=90000)
            logger.info("✅ Giao diện Marimo Notebook đã sẵn sàng.")
        except Exception:
            logger.warning("Không tìm thấy #novelreader-container ngay, đợi thêm 10s...")
            await asyncio.sleep(10)

        if (any(part in page.url.lower() for part in ("/login", "/signin", "accounts.google.com"))
                or (not preview_loaded and not public_app_url)):
            await context.close()
            if browser:
                await browser.close()
            raise RuntimeError(
                "MoLab sign-in is required. Run this job on the interactive Windows runner with --sync-profile enabled."
            )

        # 1. Upload story file if provided
        if file_path and file_path.exists():
            logger.info(f"Đang tải file truyện lên notebook: {file_path.name}")
            file_input_selector = "#novelreader-file-input input[type='file'], input[type='file']"
            try:
                await surface.wait_for_selector(file_input_selector, timeout=30000)
                await surface.set_input_files(file_input_selector, str(file_path.resolve()))
                logger.info("✅ Đã đưa file truyện vào file input widget.")
            except Exception as exc:
                logger.warning(f"Không thể đặt file trực tiếp vào input: {exc}")

        # 2. Input JSON configuration
        if config_data:
            logger.info("Đang điền cấu hình JSON...")
            config_selector = "#novelreader-config-input textarea, textarea"
            try:
                config_str = json.dumps(config_data, indent=2, ensure_ascii=False)
                await surface.wait_for_selector(config_selector, timeout=20000)
                await surface.fill(config_selector, config_str)
                logger.info("✅ Đã cập nhật cấu hình JSON.")
            except Exception as exc:
                logger.warning(f"Không thể điền cấu hình vào textarea: {exc}")

        # 3. Select storage backend if dropdown exists
        storage_selector = "#novelreader-storage-select select, select"
        try:
            if await surface.query_selector(storage_selector):
                await surface.select_option(storage_selector, storage_backend)
                logger.info(f"✅ Đã chọn dịch vụ lưu trữ: {storage_backend}")
        except Exception:
            pass

        # Telegram jobs are queued in HF and started by the notebook poller.
        if job_id:
            logger.info("Waiting for the MoLab background worker to process the queued Telegram job.")
        else:
            logger.info("Clicking the Run button for the manually uploaded file.")
            run_btn_selector = "#novelreader-run-btn button, button:has-text('RUN')"
            try:
                await surface.wait_for_selector(run_btn_selector, timeout=30000)
                await surface.click(run_btn_selector)
            except Exception as exc:
                logger.warning(f"Could not click the Run button: {exc}")

        # 5. Monitor progress loop
        job_store = None
        job_folder = None
        if job_id:
            from app.storage import create_store

            job_store = create_store()
            app_folder = job_store.ensure_folder("NovelReader_Auto_Run")
            jobs_folder = job_store.ensure_folder("jobs", app_folder)
            job_folder = job_store.ensure_folder(job_id, jobs_folder)
        start_time = time.monotonic()
        final_result = {"status": "UNKNOWN", "result_link": "", "progress": 0}

        while True:
            elapsed = time.monotonic() - start_time
            if elapsed > max_duration_seconds:
                logger.warning(f"⚠️ Đã đạt thời gian chạy tối đa ({max_duration_seconds}s). Chuẩn bị lưu state và lên lịch tiếp...")
                final_result["status"] = "TIMEOUT"
                break

            if job_store and job_folder:
                manifest = job_store.get_json(job_folder, "job.json")
                if manifest:
                    cur_status = manifest.get("status", "UNKNOWN")
                    progress = manifest.get("progress", {})
                    cur_done = progress.get("done_chapters", 0)
                    cur_total = progress.get("total_chapters", 0)
                    logger.info(f"[{int(elapsed)}s] Job {job_id}: {cur_status} | {cur_done}/{cur_total} chapters")
                    if cur_status in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"):
                        result = manifest.get("result") or {}
                        link = result.get("download_link", "")
                        if not link and result.get("result_path"):
                            link = job_store.share_link_reader(result["result_path"])
                        final_result = {
                            "status": cur_status,
                            "result_link": link,
                            "progress": 100 if cur_status.startswith("COMPLETED") else 0,
                            "done": cur_done,
                            "total": cur_total,
                        }
                        break

            status_el = await surface.query_selector("#novelreader-status")
            if status_el:
                cur_status = (await status_el.get_attribute("data-status")) or "RUNNING"
                cur_prog = (await status_el.get_attribute("data-progress")) or "0"
                cur_done = (await status_el.get_attribute("data-done")) or "0"
                cur_tot = (await status_el.get_attribute("data-total")) or "0"
                cur_link = (await status_el.get_attribute("data-result-link")) or ""

                logger.info(f"[{int(elapsed)}s] Trạng thái: {cur_status} | Tiến độ: {cur_done}/{cur_tot} chương ({cur_prog}%)")

                if job_id and cur_status == "FAILED":
                    detail = (await status_el.get_attribute("data-error")) or "MoLab worker failed to start."
                    raise RuntimeError(detail)

                if not job_id and cur_status in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
                    logger.info(f"🎉 Xử lý hoàn tất! Link tải: {cur_link}")
                    final_result = {
                        "status": cur_status,
                        "result_link": cur_link,
                        "progress": 100,
                        "done": cur_done,
                        "total": cur_tot,
                    }
                    break
                elif not job_id and cur_status in ("FAILED", "STOPPED"):
                    logger.error(f"❌ Xử lý kết thúc với trạng thái: {cur_status}")
                    final_result = {"status": cur_status, "result_link": "", "progress": cur_prog}
                    break

            await asyncio.sleep(poll_interval)

        # 6. Notify Telegram Bot if configured
        if telegram_bot_token and telegram_chat_id and final_result.get("result_link"):
            try:
                bot_url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                text_msg = (
                    f"🎉 <b>Xử lý tiểu thuyết thành công!</b>\n\n"
                    f"🔗 <b>Link tải kết quả:</b> {final_result['result_link']}\n\n"
                    f"Phiên MoLab đã hoàn thành nhiệm vụ."
                )
                requests.post(
                    bot_url,
                    json={"chat_id": telegram_chat_id, "text": text_msg, "parse_mode": "HTML"},
                    timeout=15,
                )
                logger.info("✅ Đã gửi link kết quả về Telegram.")
            except Exception as tg_err:
                logger.warning(f"Không thể gửi thông báo Telegram: {tg_err}")

        # 7. Step 12: Stop MoLab Session
        logger.info("Thực hiện ngưng phiên MoLab để tiết kiệm tài nguyên...")
        stop_btn_selector = "#novelreader-stop-btn button, button:has-text('STOP'), button:has-text('DỪNG')"
        try:
            if await surface.query_selector(stop_btn_selector):
                await surface.click(stop_btn_selector)
                logger.info("🛑 Đã bấm nút Stop trên phiên MoLab.")
        except Exception:
            pass

        await context.close()
        if browser:
            await browser.close()
        if sync_profile and profile_dir:
            _sync_profile(profile_dir, download=False)
        return final_result


def main():
    parser = argparse.ArgumentParser(description="NovelReader MoLab Browser Automation Controller")
    parser.add_argument(
        "--notebook-url",
        default=os.getenv(
            "MOLAB_NOTEBOOK_URL",
            "https://molab.marimo.io/notebooks/nb_Xbo9yBAiW4wEx3M96hNnz4/app",
        ),
    )
    parser.add_argument("--file", type=Path, default=None, help="Đường dẫn file truyện (.txt hoặc .epub)")
    parser.add_argument("--config", default=None, help="Đường dẫn file JSON cấu hình hoặc chuỗi JSON")
    parser.add_argument("--storage", default=os.getenv("STORAGE_BACKEND", "huggingface"))
    parser.add_argument("--profile-dir", type=Path, default=None)
    parser.add_argument("--job-id", default=os.getenv("NOVELREADER_JOB_ID") or None)
    parser.add_argument("--sync-profile", action="store_true", help="Restore and save an encrypted profile in HF storage")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", action="store_false", dest="headless")
    parser.add_argument("--poll-interval", type=int, default=15)
    parser.add_argument("--max-seconds", type=int, default=18000)

    args = parser.parse_args()

    profile_dir = args.profile_dir or (
        Path(os.environ["MOLAB_BROWSER_PROFILE"])
        if os.getenv("MOLAB_BROWSER_PROFILE")
        else Path.home() / ".novelreader" / "molab-chromium-profile"
    )
    config_data = None
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            try:
                config_data = json.loads(args.config)
            except Exception:
                pass

    result = asyncio.run(
        run_molab_automation(
            notebook_url=args.notebook_url,
            file_path=args.file,
            config_data=config_data,
            storage_backend=args.storage,
            headless=args.headless,
            poll_interval=args.poll_interval,
            max_duration_seconds=args.max_seconds,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=int(os.getenv("TELEGRAM_CHAT_ID", "0")) if os.getenv("TELEGRAM_CHAT_ID") else None,
            profile_dir=profile_dir,
            job_id=args.job_id,
            sync_profile=args.sync_profile,
        )
    )

    print(json.dumps(result, indent=2))
    if result["status"] not in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
        sys.exit(1)


if __name__ == "__main__":
    main()
