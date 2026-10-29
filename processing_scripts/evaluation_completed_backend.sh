#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"
TOKEN_FILE="$PROJECT_ROOT/data/asr_api.token"
MODE="${1:-run}"

if [[ "$MODE" != "run" && "$MODE" != "--list" && "$MODE" != "--pending" && "$MODE" != "--force" ]]; then
    echo "Usage: $0 [--list|--pending|--force]" >&2
    exit 2
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "Missing shared environment: $PYTHON" >&2
    exit 1
fi
if [[ ! -s "$TOKEN_FILE" ]]; then
    echo "Missing ASR API token. Start ./processing_scripts/asr_api_backend.sh first." >&2
    exit 1
fi

PYTHON_VERSION="$($PYTHON -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
NVIDIA_ROOT="$PROJECT_ROOT/.venv-preprocessing/lib/$PYTHON_VERSION/site-packages/nvidia"
CUDA_LIBRARY_PATH="$NVIDIA_ROOT/cublas/lib:$NVIDIA_ROOT/cudnn/lib:$NVIDIA_ROOT/cuda_nvrtc/lib"

export ASR_API_URL="${ASR_API_URL:-http://127.0.0.1:8765}"
export ASR_API_TOKEN="${ASR_API_TOKEN:-$(cat "$TOKEN_FILE")}" 
export EVALUATION_REPORTS_DIR="${EVALUATION_REPORTS_DIR:-$PROJECT_ROOT/evaluation_reports}"
export EVALUATION_PYTHON="$PYTHON"
export LD_LIBRARY_PATH="$CUDA_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export EVALUATION_BATCH_MODE="$MODE"

cd "$PROJECT_ROOT"
exec "$PYTHON" -u - <<'PY'
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

import app
from processing_scripts import dataset_evaluation as evaluation


def asr_ready():
    try:
        with urllib.request.urlopen(
            os.getenv("ASR_API_URL", "http://127.0.0.1:8765").rstrip("/") + "/health",
            timeout=5,
        ) as response:
            return bool(json.loads(response.read()).get("ready"))
    except Exception as error:
        print(f"ASR API is unavailable: {error}", file=sys.stderr)
        return False


def completed_report_is_current(participant_code):
    with app.get_db() as connection:
        run = connection.execute(
            """SELECT summary_path FROM evaluation_runs
               WHERE participant_code=? AND status='completed'
               ORDER BY created_at DESC LIMIT 1""",
            (participant_code,),
        ).fetchone()
        recordings = {
            row["sentence_id"]: str(Path(row["original_mp4_path"]).resolve())
            for row in connection.execute(
                """SELECT sentence_id, original_mp4_path FROM recordings
                   WHERE participant_code=? AND quality_status!='retake_requested'""",
                (participant_code,),
            )
        }
    if run is None or not run["summary_path"]:
        return False
    summary_path = Path(run["summary_path"])
    if not summary_path.is_file():
        return False
    try:
        report = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    thresholds = report.get("thresholds_applied", {})
    expected_thresholds = {
        "max_wer_threshold": evaluation.MAX_WER_THRESHOLD,
        "cer_review_threshold": evaluation.CER_REVIEW_THRESHOLD,
        "cer_flag_threshold": evaluation.CER_FLAG_THRESHOLD,
        "min_brightness_threshold": evaluation.MIN_BRIGHTNESS_THRESHOLD,
        "max_brightness_threshold": evaluation.MAX_BRIGHTNESS_THRESHOLD,
        "min_volume_db_threshold": evaluation.MIN_VOLUME_DB_THRESHOLD,
    }
    if any(thresholds.get(key) != value for key, value in expected_thresholds.items()):
        return False
    reported_recordings = {
        item.get("sentence_id"): str(Path(item.get("file_paths", {}).get("video", "")).resolve())
        for item in report.get("records", [])
        if item.get("sentence_id") and item.get("file_paths", {}).get("video")
    }
    return len(recordings) == expected and reported_recordings == recordings


expected = len(app.load_sentences())
if expected <= 0:
    raise SystemExit("No sentences were found in sentences.csv.")

with app.get_db() as connection:
    participant_codes = [
        row[0]
        for row in connection.execute(
            """SELECT p.participant_code
               FROM participants p
               JOIN recordings r ON r.participant_code=p.participant_code
                                  AND r.quality_status!='retake_requested'
               GROUP BY p.participant_code
               HAVING COUNT(DISTINCT r.sentence_id)=?
               ORDER BY p.id""",
            (expected,),
        )
    ]

print(f"Found {len(participant_codes)} participants with {expected}/{expected} recordings.", flush=True)
if os.getenv("EVALUATION_BATCH_MODE") == "--list":
    print(" ".join(participant_codes))
    raise SystemExit(0)
if os.getenv("EVALUATION_BATCH_MODE") == "--pending":
    pending_codes = [code for code in participant_codes if not completed_report_is_current(code)]
    print(f"{len(pending_codes)} participants need evaluation.")
    print(" ".join(pending_codes))
    raise SystemExit(0)
if not asr_ready():
    raise SystemExit("Start ./processing_scripts/asr_api_backend.sh before batch evaluation.")
completed = failed = skipped = 0

for position, participant_code in enumerate(participant_codes, 1):
    if os.getenv("EVALUATION_BATCH_MODE") != "--force" and completed_report_is_current(participant_code):
        skipped += 1
        print(
            f"[{position}/{len(participant_codes)}] Skipping {participant_code}: "
            "current completed report already exists.",
            flush=True,
        )
        continue
    with app.get_db() as connection:
        current_count = connection.execute(
            """SELECT COUNT(DISTINCT sentence_id) FROM recordings
               WHERE participant_code=? AND quality_status!='retake_requested'""",
            (participant_code,),
        ).fetchone()[0]
        pending_upload = connection.execute(
            """SELECT 1 FROM media_jobs WHERE participant_code=?
               AND status IN ('queued','processing') LIMIT 1""",
            (participant_code,),
        ).fetchone()
        active = connection.execute(
            """SELECT id FROM evaluation_runs WHERE participant_code=?
               AND status='running' ORDER BY created_at DESC LIMIT 1""",
            (participant_code,),
        ).fetchone()

    if current_count != expected or pending_upload:
        skipped += 1
        print(
            f"[{position}/{len(participant_codes)}] Skipping {participant_code}: "
            f"currently {current_count}/{expected} recordings or upload pending.",
            flush=True,
        )
        continue

    if active:
        run_id = active["id"]
        print(f"[{position}/{len(participant_codes)}] Waiting for existing {participant_code} run {run_id}.", flush=True)
    else:
        result = app.api_start_participant_evaluation(participant_code, admin=True)
        run_id = result["run"]["id"]
        print(f"[{position}/{len(participant_codes)}] Started {participant_code}: {run_id}", flush=True)

    last_progress = None
    while True:
        with app.get_db() as connection:
            run = connection.execute(
                """SELECT status,total_recordings,completed_recordings,error
                   FROM evaluation_runs WHERE id=?""",
                (run_id,),
            ).fetchone()
        if run is None:
            failed += 1
            print(f"  {participant_code}: run disappeared from the database.", flush=True)
            break
        progress = (run["completed_recordings"], run["total_recordings"])
        if progress != last_progress:
            print(f"  {participant_code}: {progress[0]}/{progress[1]}", flush=True)
            last_progress = progress
        if run["status"] == "completed":
            completed += 1
            print(f"  {participant_code}: completed.", flush=True)
            break
        if run["status"] == "failed":
            failed += 1
            print(f"  {participant_code}: failed: {run['error']}", flush=True)
            break
        time.sleep(5)

print(f"Batch finished: {completed} completed, {failed} failed, {skipped} skipped.", flush=True)
raise SystemExit(1 if failed else 0)
PY
