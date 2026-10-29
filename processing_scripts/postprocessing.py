#!/usr/bin/env python3
"""Run post-processing immediately for one participant."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import sys
import time
from pathlib import Path

from . import postprocessing_worker as worker
from .postprocessing_system import initialize, start_run, update_job


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("APP_DB_PATH", BASE_DIR / "data" / "app.db"))
OUTPUT_ROOT = Path(os.getenv("PROCESSED_RECORDINGS_DIR", BASE_DIR / "recording_processed"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize and crop one participant's clean videos.")
    parser.add_argument("--participant_id", "--participant-id", required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    code = args.participant_id.strip().upper()
    if not 1 <= args.workers <= 8:
        parser.error("--workers must be between 1 and 8.")
    initialize(DB_PATH)
    worker.preflight()
    key = hashlib.sha256(code.encode()).hexdigest()[:16]
    lock_path = DB_PATH.parent / f"postprocessing-{key}.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another post-processing command is already running for this participant.")
        try:
            job = start_run(DB_PATH, OUTPUT_ROOT, code)
        except (ValueError, FileNotFoundError, RuntimeError) as error:
            parser.error(str(error))
        print(f"[{code}] Processing {job['total_files']} clean recording(s) with {args.workers} worker(s).", flush=True)
        try:
            failed = worker.process_job(DB_PATH, job, args.workers)
        except KeyboardInterrupt:
            update_job(DB_PATH, job["id"], status="failed", stage="Interrupted",
                       error="Interrupted by operator.", finished_at=time.time())
            return 130
        except Exception as error:
            update_job(DB_PATH, job["id"], status="failed", stage="Command failed",
                       error=str(error)[:2500], finished_at=time.time())
            print(f"[{code}] Failed: {error}", file=sys.stderr)
            return 1
        print(f"[{code}] Finished with {failed} failed recording(s). Results: {OUTPUT_ROOT / code}", flush=True)
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
