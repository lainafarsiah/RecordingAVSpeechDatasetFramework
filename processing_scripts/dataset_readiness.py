#!/usr/bin/env python3
"""Export one participant into the AVSE-ready modality structure."""

import argparse
import fcntl
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

from . import dataset_readiness_worker as worker
from .dataset_readiness_system import (
    initialize,
    recover_interrupted_run,
    start_run,
    update_job,
)

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("APP_DB_PATH", BASE_DIR / "data" / "app.db"))
OUTPUT_ROOT = Path(os.getenv("DATASET_READY_DIR", BASE_DIR / "dataset_ready"))


def main():
    parser = argparse.ArgumentParser(description="Create an AVSE-ready export for one participant.")
    parser.add_argument("--participant_id", "--participant-id", required=True)
    parser.add_argument("--mode", choices=("link", "copy"), default="link")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    code = args.participant_id.strip().upper()
    if not 1 <= args.workers <= 8: parser.error("--workers must be between 1 and 8.")
    if shutil.which("ffmpeg") is None: parser.error("ffmpeg is required.")
    initialize(DB_PATH)
    lock_path = DB_PATH.parent / f"dataset-readiness-{hashlib.sha256(code.encode()).hexdigest()[:16]}.lock"
    with lock_path.open("w") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: parser.error("Another dataset export is running for this participant.")
        recovered = recover_interrupted_run(DB_PATH, code)
        if recovered:
            print(f"[{code}] Recovered {recovered} interrupted export run.", flush=True)
        try: job = start_run(DB_PATH, OUTPUT_ROOT, code, args.mode)
        except (ValueError, FileNotFoundError, RuntimeError) as error: parser.error(str(error))
        print(f"[{code}] Exporting {job['total_files']} recording(s) in {args.mode} mode.", flush=True)
        try: failed = worker.process_job(DB_PATH, job, args.workers)
        except KeyboardInterrupt:
            update_job(DB_PATH, job["id"], status="failed", stage="Interrupted",
                       error="Interrupted by operator.", finished_at=time.time())
            return 130
        except Exception as error:
            update_job(DB_PATH, job["id"], status="failed", stage="Command failed",
                       error=str(error)[:2500], finished_at=time.time())
            print(f"[{code}] Failed: {error}", file=sys.stderr); return 1
        print(f"[{code}] Finished with {failed} failed recording(s): {OUTPUT_ROOT / code}", flush=True)
        return 1 if failed else 0


if __name__ == "__main__": raise SystemExit(main())
