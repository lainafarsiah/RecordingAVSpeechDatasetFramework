#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi
export APP_ENV=production
exec "${PYTHON_BIN:-python3}" -m uvicorn app:app \
    --host "${BIND_HOST:-127.0.0.1}" --port "${PORT:-8000}" \
    --workers "${WEB_WORKERS:-4}" --proxy-headers \
    --forwarded-allow-ips "${TRUSTED_PROXY_IPS:-127.0.0.1}"
