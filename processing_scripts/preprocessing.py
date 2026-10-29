#!/usr/bin/env python3
"""Process one participant directly from the server command line."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import sys
import time

from . import preprocessing_worker as worker
from .preprocessing_system import initialize, start_participant_run, update_job


def process_participant(
    participant_code: str, workers: int, parser: argparse.ArgumentParser
) -> int:
    initialize(worker.DB_PATH)
    worker.preflight()
    try:
        job = start_participant_run(
            worker.DB_PATH, worker.CLEAN_ROOT, participant_code, worker.WORKER_ID
        )
    except (LookupError, ValueError, FileNotFoundError, RuntimeError) as error:
        parser.error(str(error))
    run_id = job["id"]

    if os.getenv("ASR_API_URL"):
        print(f"[{participant_code}] Connecting to persistent ASR service…", flush=True)
    else:
        print(f"[{participant_code}] Loading {worker.WHISPER_MODEL}…", flush=True)
    try:
        model = worker.load_asr_model(workers=workers)
        print(
            f"[{participant_code}] Processing {job['total_files']} recording(s) "
            f"with {workers} worker(s). Run: {run_id}",
            flush=True,
        )
        worker.process_job(job, model, workers=workers)
    except KeyboardInterrupt:
        update_job(
            worker.DB_PATH, run_id, worker.WORKER_ID, status="failed",
            stage="Interrupted from command line", error="Interrupted by operator.",
            finished_at=time.time(), heartbeat_at=time.time(),
        )
        print("Processing interrupted.", file=sys.stderr)
        return 130
    except Exception as error:
        update_job(
            worker.DB_PATH, run_id, worker.WORKER_ID, status="failed",
            stage="Command failed", error=str(error)[:2000],
            finished_at=time.time(), heartbeat_at=time.time(),
        )
        print(f"Processing failed: {error}", file=sys.stderr)
        return 1

    print(
        f"[{participant_code}] Finished. Results: "
        f"{worker.CLEAN_ROOT / participant_code}",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Denoise, assess, and transcribe one participant's recordings."
    )
    parser.add_argument(
        "--participant_id", "--participant-id", required=True,
        help="Participant code, for example P0001.",
    )
    parser.add_argument(
        "--workers", type=int, default=2,
        help="Recordings to process concurrently (default: 2, maximum: 16).",
    )
    args = parser.parse_args()
    participant_code = args.participant_id.strip().upper()
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be between 1 and 16.")

    participant_key = hashlib.sha256(participant_code.encode("utf-8")).hexdigest()[:16]
    lock_path = worker.DB_PATH.parent / f"preprocessing-{participant_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as process_lock:
        try:
            fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another participant preprocessing command is already running.")
        return process_participant(participant_code, args.workers, parser)


if __name__ == "__main__":
    raise SystemExit(main())
