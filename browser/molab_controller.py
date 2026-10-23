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
import time
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("novelreader.browser")


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
) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright chưa được cài đặt. Chạy: pip install playwright && playwright install chromium")
        raise RuntimeError("Playwright is required for browser automation")

    logger.info(f"Khởi động trình duyệt Chromium tự động (headless={headless})...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
            ],
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        logger.info(f"Đang điều hướng tới MoLab Notebook: {notebook_url}")
        try:
            await page.goto(notebook_url, wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:
            logger.warning(f"Điều hướng ban đầu: {exc}. Đang thử đợi nội dung render...")

        logger.info("Chờ giao diện NovelReader hoặc Marimo app tải...")
        try:
            await page.wait_for_selector("#novelreader-container, .marimo-app, [data-marimo-app]", timeout=90000)
            logger.info("✅ Giao diện Marimo Notebook đã sẵn sàng.")
        except Exception:
            logger.warning("Không tìm thấy #novelreader-container ngay, đợi thêm 10s...")
            await asyncio.sleep(10)

        # 1. Upload story file if provided
        if file_path and file_path.exists():
            logger.info(f"Đang tải file truyện lên notebook: {file_path.name}")
            file_input_selector = "#novelreader-file-input input[type='file'], input[type='file']"
            try:
                await page.wait_for_selector(file_input_selector, timeout=30000)
                await page.set_input_files(file_input_selector, str(file_path.resolve()))
                logger.info("✅ Đã đưa file truyện vào file input widget.")
            except Exception as exc:
                logger.warning(f"Không thể đặt file trực tiếp vào input: {exc}")

        # 2. Input JSON configuration
        if config_data:
            logger.info("Đang điền cấu hình JSON...")
            config_selector = "#novelreader-config-input textarea, textarea"
            try:
                config_str = json.dumps(config_data, indent=2, ensure_ascii=False)
                await page.wait_for_selector(config_selector, timeout=20000)
                await page.fill(config_selector, config_str)
                logger.info("✅ Đã cập nhật cấu hình JSON.")
            except Exception as exc:
                logger.warning(f"Không thể điền cấu hình vào textarea: {exc}")

        # 3. Select storage backend if dropdown exists
        storage_selector = "#novelreader-storage-select select, select"
        try:
            if await page.query_selector(storage_selector):
                await page.select_option(storage_selector, storage_backend)
                logger.info(f"✅ Đã chọn dịch vụ lưu trữ: {storage_backend}")
        except Exception:
            pass

        # 4. Click Run Button
        logger.info("Kích hoạt nút Run trên giao diện MoLab...")
        run_btn_selector = "#novelreader-run-btn button, button:has-text('RUN'), button:has-text('BẮT ĐẦU')"
        try:
            await page.wait_for_selector(run_btn_selector, timeout=30000)
            await page.click(run_btn_selector)
            logger.info("🚀 Đã nhấn nút Run. Tiến trình NovelReader bắt đầu hoạt động.")
        except Exception as exc:
            logger.warning(f"Không thể bấm nút Run: {exc}. Kiểm tra xem worker có tự chạy ngầm không...")

        # 5. Monitor progress loop
        start_time = time.monotonic()
        final_result = {"status": "UNKNOWN", "result_link": "", "progress": 0}

        while True:
            elapsed = time.monotonic() - start_time
            if elapsed > max_duration_seconds:
                logger.warning(f"⚠️ Đã đạt thời gian chạy tối đa ({max_duration_seconds}s). Chuẩn bị lưu state và lên lịch tiếp...")
                final_result["status"] = "TIMEOUT"
                break

            status_el = await page.query_selector("#novelreader-status")
            if status_el:
                cur_status = (await status_el.get_attribute("data-status")) or "RUNNING"
                cur_prog = (await status_el.get_attribute("data-progress")) or "0"
                cur_done = (await status_el.get_attribute("data-done")) or "0"
                cur_tot = (await status_el.get_attribute("data-total")) or "0"
                cur_link = (await status_el.get_attribute("data-result-link")) or ""

                logger.info(f"[{int(elapsed)}s] Trạng thái: {cur_status} | Tiến độ: {cur_done}/{cur_tot} chương ({cur_prog}%)")

                if cur_status in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
                    logger.info(f"🎉 Xử lý hoàn tất! Link tải: {cur_link}")
                    final_result = {
                        "status": cur_status,
                        "result_link": cur_link,
                        "progress": 100,
                        "done": cur_done,
                        "total": cur_tot,
                    }
                    break
                elif cur_status in ("FAILED", "STOPPED"):
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
            if await page.query_selector(stop_btn_selector):
                await page.click(stop_btn_selector)
                logger.info("🛑 Đã bấm nút Stop trên phiên MoLab.")
        except Exception:
            pass

        await browser.close()
        return final_result


def main():
    parser = argparse.ArgumentParser(description="NovelReader MoLab Browser Automation Controller")
    parser.add_argument("--notebook-url", default=os.getenv("MOLAB_NOTEBOOK_URL", "https://molab.marimo.io/notebooks"))
    parser.add_argument("--file", type=Path, default=None, help="Đường dẫn file truyện (.txt hoặc .epub)")
    parser.add_argument("--config", default=None, help="Đường dẫn file JSON cấu hình hoặc chuỗi JSON")
    parser.add_argument("--storage", default=os.getenv("STORAGE_BACKEND", "huggingface"))
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", action="store_false", dest="headless")
    parser.add_argument("--poll-interval", type=int, default=15)
    parser.add_argument("--max-seconds", type=int, default=18000)

    args = parser.parse_args()

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
        )
    )

    print(json.dumps(result, indent=2))
    if result["status"] not in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
        sys.exit(1)


if __name__ == "__main__":
    main()
