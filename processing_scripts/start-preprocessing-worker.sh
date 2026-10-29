#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PREPROCESS_PYTHON="${PREPROCESS_PYTHON:-$PWD/.venv-preprocessing/bin/python}"
if [[ ! -x "$PREPROCESS_PYTHON" ]]; then
    echo "Missing preprocessing Python environment: $PREPROCESS_PYTHON" >&2
    echo "Create it and install requirements.txt first." >&2
    exit 1
fi

export APP_DB_PATH="${APP_DB_PATH:-$PWD/data/app.db}"
export CLEAN_RECORDINGS_DIR="${CLEAN_RECORDINGS_DIR:-$PWD/recording_clean}"
export RNNOISE_MODEL_PATH="${RNNOISE_MODEL_PATH:-$PWD/models/rnnoise.rnnn}"
export WHISPER_MODEL="${WHISPER_MODEL:-large-v3}"
export WHISPER_DEVICE="${WHISPER_DEVICE:-cpu}"
export WHISPER_COMPUTE_TYPE="${WHISPER_COMPUTE_TYPE:-int8}"

PYTHON_VERSION="$($PREPROCESS_PYTHON -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
NVIDIA_ROOT="$PWD/.venv-preprocessing/lib/$PYTHON_VERSION/site-packages/nvidia"
CUDA_LIBRARY_PATH="$NVIDIA_ROOT/cublas/lib:$NVIDIA_ROOT/cudnn/lib:$NVIDIA_ROOT/cuda_nvrtc/lib"
export LD_LIBRARY_PATH="$CUDA_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 PARTICIPANT_ID [WORKERS]" >&2
    echo "Example: $0 P0001 2" >&2
    exit 2
fi

# Lower CPU and disk scheduling priority protects live participant uploads.
exec ionice -c 2 -n 7 nice -n 10 \
    "$PREPROCESS_PYTHON" -m processing_scripts.preprocessing --participant_id "$1" --workers "${2:-2}"
