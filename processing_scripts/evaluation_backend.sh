#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 PARTICIPANT_ID [GPU_ID]" >&2
    echo "Example: $0 P0001 0" >&2
    exit 2
fi

PARTICIPANT_ID="${1^^}"
GPU_ID="${2:-0}"

if [[ ! -x "$PYTHON" ]]; then
    echo "Missing shared environment: $PYTHON" >&2
    exit 1
fi
if [[ ! "$GPU_ID" =~ ^[0-9]+$ ]]; then
    echo "GPU_ID must be a non-negative integer." >&2
    exit 2
fi

PYTHON_VERSION="$($PYTHON -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
NVIDIA_ROOT="$PROJECT_ROOT/.venv-preprocessing/lib/$PYTHON_VERSION/site-packages/nvidia"
CUDA_LIBRARY_PATH="$NVIDIA_ROOT/cublas/lib:$NVIDIA_ROOT/cudnn/lib:$NVIDIA_ROOT/cuda_nvrtc/lib"

export EVALUATION_PYTHON="$PYTHON"
export EVALUATION_DEVICE=cuda
export EVALUATION_COMPUTE_TYPE="${EVALUATION_COMPUTE_TYPE:-float16}"
export EVALUATION_REPORTS_DIR="${EVALUATION_REPORTS_DIR:-$PROJECT_ROOT/evaluation_reports}"
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
' "$ASR_API_URL" || { echo "Start ./processing_scripts/asr_api_backend.sh before evaluation." >&2; exit 1; }
fi

cd "$PROJECT_ROOT"
echo "Starting evaluation for $PARTICIPANT_ID on GPU $GPU_ID."
exec "$PYTHON" -c '
import json
import sys
import app

result = app.api_start_participant_evaluation(sys.argv[1], admin=True)
print(json.dumps(result, indent=2))
' "$PARTICIPANT_ID"
