#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"

if [[ $# -lt 1 || $# -gt 3 ]]; then
    echo "Usage: $0 PARTICIPANT_ID [WORKERS] [GPU_ID]" >&2
    echo "Example: $0 P0001 2 0" >&2
    exit 2
fi

PARTICIPANT_ID="${1^^}"
WORKERS="${2:-2}"
GPU_ID="${3:-0}"

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

PYTHON_VERSION="$($PYTHON -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
NVIDIA_ROOT="$PROJECT_ROOT/.venv-preprocessing/lib/$PYTHON_VERSION/site-packages/nvidia"
CUDA_LIBRARY_PATH="$NVIDIA_ROOT/cublas/lib:$NVIDIA_ROOT/cudnn/lib:$NVIDIA_ROOT/cuda_nvrtc/lib"

export APP_DB_PATH="${APP_DB_PATH:-$PROJECT_ROOT/data/app.db}"
export CLEAN_RECORDINGS_DIR="${CLEAN_RECORDINGS_DIR:-$PROJECT_ROOT/recording_clean}"
export RNNOISE_MODEL_PATH="${RNNOISE_MODEL_PATH:-$PROJECT_ROOT/models/rnnoise.rnnn}"
export WHISPER_MODEL="${WHISPER_MODEL:-large-v3}"
export WHISPER_DEVICE=cuda
export WHISPER_COMPUTE_TYPE="${WHISPER_COMPUTE_TYPE:-float16}"
export CUDA_VISIBLE_DEVICES="$GPU_ID"
export LD_LIBRARY_PATH="$CUDA_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

TOKEN_FILE="$PROJECT_ROOT/data/asr_api.token"
if [[ -s "$TOKEN_FILE" ]]; then
    export ASR_API_URL="${ASR_API_URL:-http://127.0.0.1:8765}"
    export ASR_API_TOKEN="${ASR_API_TOKEN:-$(cat "$TOKEN_FILE")}" 
    "$PYTHON" -c '
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1].rstrip("/") + "/health", timeout=3) as response:
    health = json.loads(response.read().decode("utf-8"))
if not health.get("ready"):
    raise SystemExit("Persistent ASR service is not ready.")
' "$ASR_API_URL" || { echo "Start ./processing_scripts/asr_api_backend.sh before preprocessing." >&2; exit 1; }
fi

cd "$PROJECT_ROOT"
echo "Starting preprocessing for $PARTICIPANT_ID on GPU $GPU_ID with $WORKERS worker(s)."
exec ionice -c 2 -n 7 nice -n 10 \
    "$PYTHON" -m processing_scripts.preprocessing \
    --participant_id "$PARTICIPANT_ID" \
    --workers "$WORKERS"
