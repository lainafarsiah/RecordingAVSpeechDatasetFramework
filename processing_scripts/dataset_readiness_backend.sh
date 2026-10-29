#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"
if [[ $# -lt 1 || $# -gt 3 ]]; then
    echo "Usage: $0 PARTICIPANT_ID [link|copy] [WORKERS]" >&2; exit 2
fi
PARTICIPANT_ID="${1^^}"; MODE="${2:-link}"; WORKERS="${3:-2}"
if [[ "$MODE" != "link" && "$MODE" != "copy" ]]; then echo "Mode must be link or copy." >&2; exit 2; fi
if [[ ! "$WORKERS" =~ ^[1-8]$ ]]; then echo "WORKERS must be between 1 and 8." >&2; exit 2; fi
if [[ ! -x "$PYTHON" ]]; then echo "Missing environment: $PYTHON" >&2; exit 1; fi
export APP_DB_PATH="${APP_DB_PATH:-$PROJECT_ROOT/data/app.db}"
export DATASET_READY_DIR="${DATASET_READY_DIR:-$PROJECT_ROOT/dataset_ready}"
cd "$PROJECT_ROOT"
# Dataset export is an offline task. Idle I/O priority and a low CPU priority
# keep participant upload conversion responsive while collection is active.
exec ionice -c 3 nice -n 15 "$PYTHON" -m processing_scripts.dataset_readiness \
    --participant_id "$PARTICIPANT_ID" --mode "$MODE" --workers "$WORKERS"
