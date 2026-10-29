#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"
MODE="run"
if [[ "${1:-}" == "--list" || "${1:-}" == "--pending" || "${1:-}" == "--force" ]]; then
    MODE="$1"; shift
fi
WORKERS="${1:-2}"
if [[ $# -gt 1 || ! "$WORKERS" =~ ^[1-8]$ ]]; then
    echo "Usage: $0 [--list|--pending|--force] [WORKERS]" >&2
    exit 2
fi
if [[ ! -x "$PYTHON" ]]; then echo "Missing environment: $PYTHON" >&2; exit 1; fi

export POSTPROCESSING_BATCH_MODE="$MODE"
export POSTPROCESSING_BATCH_WORKERS="$WORKERS"
export POSTPROCESSING_PROJECT_ROOT="$PROJECT_ROOT"
export MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/recordingav-matplotlib}"
mkdir -p "$MPLCONFIGDIR"
cd "$PROJECT_ROOT"
exec "$PYTHON" -u - <<'PY'
import os
import sqlite3
import subprocess
from pathlib import Path

from processing_scripts.postprocessing_system import current_completed_run, initialize

root = Path(os.environ["POSTPROCESSING_PROJECT_ROOT"])
db_path = Path(os.getenv("APP_DB_PATH", root / "data" / "app.db"))
output_root = Path(os.getenv("PROCESSED_RECORDINGS_DIR", root / "recording_processed"))
mode = os.environ["POSTPROCESSING_BATCH_MODE"]
workers = os.environ["POSTPROCESSING_BATCH_WORKERS"]
with (root / "sentences.csv").open(encoding="utf-8-sig") as source:
    expected = sum(1 for line in source if line.strip()) - 1
if expected <= 0:
    raise SystemExit("No sentences were found in sentences.csv.")
initialize(db_path)
connection = sqlite3.connect(db_path); connection.row_factory = sqlite3.Row
codes = [row["participant_code"] for row in connection.execute(
    """SELECT j.participant_code FROM preprocessing_jobs j
       WHERE j.status='completed' AND j.failed_files=0
         AND j.id=(SELECT id FROM preprocessing_jobs
                   WHERE participant_code=j.participant_code
                   ORDER BY created_at DESC LIMIT 1)
         AND j.total_files=? AND j.completed_files=?
       ORDER BY j.created_at""", (expected, expected))]
connection.close()
print(f"Found {len(codes)} participants with current completed {expected}/{expected} preprocessing.", flush=True)
if mode == "--list":
    print(" ".join(codes)); raise SystemExit(0)
pending = [code for code in codes if mode == "--force" or not current_completed_run(db_path, output_root, code)]
if mode == "--pending":
    print(f"{len(pending)} participants need post-processing.")
    print(" ".join(pending)); raise SystemExit(0)
completed = failed = skipped = 0
for position, code in enumerate(codes, 1):
    if code not in pending:
        skipped += 1
        print(f"[{position}/{len(codes)}] Skipping {code}: current output exists.", flush=True)
        continue
    print(f"[{position}/{len(codes)}] Post-processing {code}...", flush=True)
    result = subprocess.run([str(root / "processing_scripts" / "postprocessing_backend.sh"), code, workers], cwd=root)
    if result.returncode == 0: completed += 1
    else:
        failed += 1
        print(f"  {code}: failed with exit code {result.returncode}; continuing.", flush=True)
print(f"Batch finished: {completed} completed, {failed} failed, {skipped} skipped.", flush=True)
raise SystemExit(1 if failed else 0)
PY
