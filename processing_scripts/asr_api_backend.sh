#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"
GPU_ID="${1:-0}"
PORT="${2:-8765}"
MAX_CONCURRENT="${3:-2}"
TOKEN_FILE="$PROJECT_ROOT/data/asr_api.token"

if [[ ! -x "$PYTHON" ]]; then
    echo "Missing shared environment: $PYTHON" >&2
    exit 1
fi
if [[ ! "$GPU_ID" =~ ^[0-9]+$ ]] || [[ ! "$PORT" =~ ^[0-9]+$ ]] ||
   [[ ! "$MAX_CONCURRENT" =~ ^([1-9]|1[0-6])$ ]]; then
    echo "Usage: $0 [GPU_ID] [PORT] [MAX_CONCURRENT]" >&2
    echo "Example: $0 0 8765 2" >&2
    exit 2
fi

mkdir -p "$PROJECT_ROOT/data"
if [[ ! -s "$TOKEN_FILE" ]]; then
    "$PYTHON" -c 'import secrets,sys; open(sys.argv[1], "w").write(secrets.token_urlsafe(32))' "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
fi

PYTHON_VERSION="$($PYTHON -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
NVIDIA_ROOT="$PROJECT_ROOT/.venv-preprocessing/lib/$PYTHON_VERSION/site-packages/nvidia"
CUDA_LIBRARY_PATH="$NVIDIA_ROOT/cublas/lib:$NVIDIA_ROOT/cudnn/lib:$NVIDIA_ROOT/cuda_nvrtc/lib"

export ASR_API_TOKEN="$(cat "$TOKEN_FILE")"
export ASR_MODEL="${ASR_MODEL:-large-v3}"
export ASR_DEVICE="${ASR_DEVICE:-cuda}"
export ASR_COMPUTE_TYPE="${ASR_COMPUTE_TYPE:-float16}"
export ASR_MAX_CONCURRENT="$MAX_CONCURRENT"
export ASR_ALLOWED_ROOTS="${ASR_ALLOWED_ROOTS:-$PROJECT_ROOT/recordings:$PROJECT_ROOT/recording_clean}"
export CUDA_VISIBLE_DEVICES="$GPU_ID"
export LD_LIBRARY_PATH="$CUDA_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

cd "$PROJECT_ROOT"
echo "Loading $ASR_MODEL on GPU $GPU_ID. ASR API will listen on 127.0.0.1:$PORT."
exec "$PYTHON" -m uvicorn processing_scripts.asr_api:app \
    --host 127.0.0.1 --port "$PORT" --workers 1
