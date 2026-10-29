#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"
MODE="run"
WORKERS="2"
GPU_ID="0"

if [[ "${1:-}" == "--list" || "${1:-}" == "--pending" || "${1:-}" == "--force" ]]; then
    MODE="$1"
    shift
fi
if [[ $# -gt 2 ]]; then
    echo "Usage: $0 [--list|--pending|--force] [WORKERS] [GPU_ID]" >&2
    exit 2
fi
WORKERS="${1:-$WORKERS}"
GPU_ID="${2:-$GPU_ID}"

if [[ ! -x "$PYTHON" ]]; then
    echo "Missing shared environment: $PYTHON" >&2
    exit 1
fi
if [[ ! "$WORKERS" =~ ^([1-9]|1[0-6])$ ]]; then
    echo "WORKERS must be between 1 and 16." >&2
    exit 2
fi
if [[ ! "$GPU_ID" =~ ^[0-9]+$ ]]; then
    echo "GPU_ID must be a non-negative integer." >&2
    exit 2
fi

export PREPROCESSING_BATCH_MODE="$MODE"
export PREPROCESSING_BATCH_WORKERS="$WORKERS"
export PREPROCESSING_BATCH_GPU="$GPU_ID"
export PREPROCESSING_PROJECT_ROOT="$PROJECT_ROOT"

cd "$PROJECT_ROOT"
exec "$PYTHON" -u - <<'PY'
import json
import os
import sqlite3
import subprocess
from pathlib import Path

root = Path(os.environ["PREPROCESSING_PROJECT_ROOT"])
db_path = Path(os.getenv("APP_DB_PATH", root / "data" / "app.db"))
clean_root = Path(os.getenv("CLEAN_RECORDINGS_DIR", root / "recording_clean"))
mode = os.environ["PREPROCESSING_BATCH_MODE"]
workers = os.environ["PREPROCESSING_BATCH_WORKERS"]
gpu_id = os.environ["PREPROCESSING_BATCH_GPU"]


def connect():
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def current_recordings(connection, participant_code):
    return [
        dict(row)
        for row in connection.execute(
            """SELECT sentence_id, original_mp4_path
               FROM recordings
               WHERE participant_code=? AND quality_status!='retake_requested'
               ORDER BY sentence_id""",
            (participant_code,),
        )
    ]


def completed_preprocessing_is_current(participant_code):
    with connect() as connection:
        recordings = current_recordings(connection, participant_code)
        runs = connection.execute(
            """SELECT id, source_snapshot, total_files, completed_files,
                      failed_files, result_dir
               FROM preprocessing_jobs
               WHERE participant_code=? AND status='completed'
               ORDER BY created_at DESC""",
            (participant_code,),
        ).fetchall()

    current = {}
    for item in recordings:
        path = Path(item["original_mp4_path"])
        if not path.is_file():
            return False
        stat = path.stat()
        current[item["sentence_id"]] = (
            str(path.resolve()), stat.st_size, stat.st_mtime_ns
        )

    for run in runs:
        if (
            run["total_files"] != len(current)
            or run["completed_files"] != len(current)
            or run["failed_files"] != 0
        ):
            continue
        try:
            snapshot = json.loads(run["source_snapshot"])
        except (TypeError, json.JSONDecodeError):
            continue
        recorded = {
            item["sentence_id"]: (
                str(Path(item["original_mp4_path"]).resolve()),
                item.get("source_size"),
                item.get("source_mtime_ns"),
            )
            for item in snapshot
        }
        manifest_path = Path(run["result_dir"]).parent / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if recorded == current and manifest.get("job_id") == run["id"]:
            return True
    return False


sentences_path = root / "sentences.csv"
with sentences_path.open(encoding="utf-8-sig") as source:
    # The first nonempty row is a header in the deployed sentence file.
    expected = sum(1 for line in source if line.strip()) - 1
if expected <= 0:
    raise SystemExit("No sentences were found in sentences.csv.")

with connect() as connection:
    participant_codes = [
        row["participant_code"]
        for row in connection.execute(
            """SELECT p.participant_code
               FROM participants p
               JOIN recordings r ON r.participant_code=p.participant_code
                                  AND r.quality_status!='retake_requested'
               GROUP BY p.id, p.participant_code
               HAVING COUNT(DISTINCT r.sentence_id)=?
               ORDER BY p.id""",
            (expected,),
        )
    ]

print(
    f"Found {len(participant_codes)} participants with "
    f"{expected}/{expected} current recordings.",
    flush=True,
)
if mode == "--list":
    print(" ".join(participant_codes))
    raise SystemExit(0)

pending = [
    code for code in participant_codes
    if mode == "--force" or not completed_preprocessing_is_current(code)
]
if mode == "--pending":
    print(f"{len(pending)} participants need preprocessing.")
    print(" ".join(pending))
    raise SystemExit(0)

completed = failed = skipped = 0
for position, participant_code in enumerate(participant_codes, 1):
    if participant_code not in pending:
        skipped += 1
        print(
            f"[{position}/{len(participant_codes)}] Skipping {participant_code}: "
            "current completed preprocessing exists.",
            flush=True,
        )
        continue

    with connect() as connection:
        active = connection.execute(
            """SELECT id, status FROM preprocessing_jobs
               WHERE participant_code=? AND status IN ('queued','running')
               ORDER BY created_at DESC LIMIT 1""",
            (participant_code,),
        ).fetchone()
        current_count = connection.execute(
            """SELECT COUNT(DISTINCT sentence_id) FROM recordings
               WHERE participant_code=? AND quality_status!='retake_requested'""",
            (participant_code,),
        ).fetchone()[0]
        upload = connection.execute(
            """SELECT 1 FROM media_jobs WHERE participant_code=?
               AND status IN ('queued','processing') LIMIT 1""",
            (participant_code,),
        ).fetchone()

    if active or upload or current_count != expected:
        skipped += 1
        reason = "an active preprocessing run exists" if active else (
            "an upload is pending" if upload else f"recording count changed to {current_count}"
        )
        print(
            f"[{position}/{len(participant_codes)}] Skipping {participant_code}: {reason}.",
            flush=True,
        )
        continue

    print(
        f"[{position}/{len(participant_codes)}] Processing {participant_code}...",
        flush=True,
    )
    result = subprocess.run(
        [str(root / "processing_scripts" / "preprocessing_backend.sh"), participant_code, workers, gpu_id],
        cwd=root,
    )
    if result.returncode == 0:
        completed += 1
    else:
        failed += 1
        print(
            f"  {participant_code}: failed with exit code {result.returncode}; "
            "continuing to the next participant.",
            flush=True,
        )

print(
    f"Batch finished: {completed} completed, {failed} failed, {skipped} skipped.",
    flush=True,
)
raise SystemExit(1 if failed else 0)
PY
