#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 PARTICIPANT_ID [WORKERS]" >&2
    echo "Example: $0 P0001 2" >&2
    exit 2
fi
PARTICIPANT_ID="${1^^}"
WORKERS="${2:-2}"
if [[ ! -x "$PYTHON" ]]; then
    echo "Missing shared environment: $PYTHON" >&2
    exit 1
fi
if [[ ! "$WORKERS" =~ ^[1-8]$ ]]; then
    echo "WORKERS must be between 1 and 8." >&2
    exit 2
fi

export APP_DB_PATH="${APP_DB_PATH:-$PROJECT_ROOT/data/app.db}"
export PROCESSED_RECORDINGS_DIR="${PROCESSED_RECORDINGS_DIR:-$PROJECT_ROOT/recording_processed}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/recordingav-matplotlib}"
mkdir -p "$MPLCONFIGDIR"
cd "$PROJECT_ROOT"
echo "Starting post-processing for $PARTICIPANT_ID with $WORKERS worker(s)."
exec ionice -c 2 -n 7 nice -n 10 "$PYTHON" -m processing_scripts.postprocessing \
    --participant_id "$PARTICIPANT_ID" --workers "$WORKERS"
