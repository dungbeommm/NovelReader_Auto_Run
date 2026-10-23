"""Continuously consume queued Hugging Face jobs from the Windows Actions runner."""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from app.storage import create_store

log = logging.getLogger("novelreader.molab_worker")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def next_job(store, job_id: str | None = None):
    app_folder = store.ensure_folder("NovelReader_Auto_Run")
    jobs_folder = store.ensure_folder("jobs", app_folder)
    candidates = []
    for row in store.list_children(jobs_folder):
        if row.get("type") != "dir":
            continue
        manifest = store.get_json(row["path"], "job.json")
        if manifest and manifest.get("status") in ("QUEUED", "RUNNING") and (not job_id or manifest.get("job_id") == job_id):
            candidates.append((manifest.get("created_at", ""), row["path"], manifest))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1], candidates[0][2]


def record_failure(store, folder: str, manifest: dict, detail: str) -> None:
    manifest["status"] = "FAILED"
    manifest["updated_at"] = utcnow()
    manifest.setdefault("errors", []).append({"stage": "browser_worker", "detail": detail[-3000:]})
    store.put_json(folder, "job.json", manifest)


def run(notebook_url: str, idle_seconds: int, max_seconds: int, job_id: str | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    deadline = time.monotonic() + max_seconds
    log.info("MoLab worker started for %s", job_id or "oldest queued job")

    while time.monotonic() < deadline:
        store = create_store()
        pending = next_job(store, job_id)
        if not pending:
            if job_id:
                log.error("Requested job %s was not found in QUEUED/RUNNING state", job_id)
                return 1
            time.sleep(min(idle_seconds, max(1, int(deadline - time.monotonic()))))
            continue

        folder, manifest = pending
        job_id = manifest["job_id"]
        story_name = manifest.get("input", {}).get("name", "story")
        log.info("Starting story '%s' (job %s)", story_name, job_id)
        command = [
            sys.executable,
            "-m",
            "browser.molab_controller",
            "--notebook-url",
            notebook_url,
            "--storage",
            "huggingface",
            "--job-id",
            job_id,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.stdout:
                log.info("MoLab controller output:\n%s", result.stdout[-6000:])
            if result.stderr:
                log.warning("MoLab controller errors:\n%s", result.stderr[-6000:])
            refreshed = store.get_json(folder, "job.json") or manifest
            if refreshed.get("status") not in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"):
                detail = (result.stderr or result.stdout).strip()[-2500:]
                reason = (
                    f"MoLab controller exited with code {result.returncode} before reaching a terminal job status. "
                    f"{detail}"
                ).strip()
                record_failure(store, folder, refreshed, reason)
            elif result.returncode and refreshed.get("status") != "FAILED":
                detail = (result.stderr or result.stdout).strip()[-2500:]
                record_failure(
                    store,
                    folder,
                    refreshed,
                    f"MoLab controller exited with code {result.returncode}. {detail}".strip(),
                )
            if job_id:
                return result.returncode
        except Exception as exc:
            log.exception("Could not process story '%s'", story_name)
            refreshed = store.get_json(folder, "job.json") or manifest
            if refreshed.get("status") not in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"):
                record_failure(store, folder, refreshed, f"{type(exc).__name__}: {exc}")

    log.info("MoLab queue worker reached its handover window")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll Hugging Face and run queued MoLab jobs")
    parser.add_argument(
        "--notebook-url",
        default=os.getenv(
            "MOLAB_NOTEBOOK_URL",
            "https://molab.marimo.io/notebooks/nb_Xbo9yBAiW4wEx3M96hNnz4/app",
        ),
    )
    parser.add_argument("--idle-seconds", type=int, default=20)
    parser.add_argument("--max-seconds", type=int, default=19000)
    parser.add_argument("--job-id", default=os.getenv("NOVELREADER_JOB_ID"))
    args = parser.parse_args()
    return run(args.notebook_url, max(5, args.idle_seconds), max(60, args.max_seconds), args.job_id)


if __name__ == "__main__":
    raise SystemExit(main())
