"""Keep the Telegram poller alive on GitHub Actions and hand over before timeout."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger("novelreader.github_runner")
ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 220
HANDOVER_AFTER = int(os.getenv("RUNNER_MAX_SECONDS", "19200"))  # 5h 20m
HARD_STOP_AFTER = int(os.getenv("RUNNER_HARD_MAX_SECONDS", "20400"))  # 5h 40m
HANDOVER_DRAIN_SECONDS = POLL_SECONDS + 30


def dispatch_successor() -> bool:
    """Queue the next continuous poller run using this run's GITHUB_TOKEN."""
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    token = os.getenv("GITHUB_ACTIONS_TOKEN", "").strip()
    workflow = os.getenv("NOVELREADER_WORKFLOW_FILE", "novelreader.yml").strip()
    ref = os.getenv("GITHUB_REF_NAME", "main").strip()
    if not repository or not token:
        log.error("Cannot hand over: GITHUB_REPOSITORY or GITHUB_ACTIONS_TOKEN is missing")
        return False

    response = requests.post(
        f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": ref},
        timeout=30,
    )
    if response.status_code not in (200, 201, 204):
        log.error("Workflow handover failed: HTTP %s %s", response.status_code, response.text[:500])
        return False
    log.info("Queued successor workflow %s on ref %s", workflow, ref)
    return True


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    start = time.monotonic()
    next_log = 1800
    child: subprocess.Popen | None = None
    handover_sent = False
    last_handover_attempt = 0.0
    command = [sys.executable, "-m", "app.telegram_bot", "--poll-seconds", str(POLL_SECONDS)]
    log.info("Continuous Telegram poller started; handover target is %d minutes", HANDOVER_AFTER // 60)

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= HANDOVER_AFTER and not handover_sent and elapsed - last_handover_attempt >= 60:
                last_handover_attempt = elapsed
                handover_sent = dispatch_successor()
                if handover_sent:
                    drain_deadline = time.monotonic() + HANDOVER_DRAIN_SECONDS
                    log.info("Successor queued; letting the current Telegram poll cycle finish")
                    while child and child.poll() is None and time.monotonic() < drain_deadline:
                        time.sleep(5)
                    if child:
                        stop_process(child)
                    return 0

            if elapsed >= HARD_STOP_AFTER:
                if child:
                    stop_process(child)
                log.error("Could not dispatch successor before the hard handover deadline")
                return 1

            if child is None or child.poll() is not None:
                if child is not None:
                    log.error("Telegram poller exited with code %s; restarting in 5 seconds", child.returncode)
                    time.sleep(5)
                child = subprocess.Popen(command, cwd=ROOT)
                log.info("Telegram poller process started (pid=%s)", child.pid)

            if elapsed >= next_log:
                log.info("Continuous poller has run for %d minutes", int(elapsed // 60))
                next_log += 1800
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("Stopping Telegram poller on interrupt")
        if child:
            stop_process(child)
        return 0
    except Exception:
        log.exception("Continuous poller supervisor failed")
        if child:
            stop_process(child)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
