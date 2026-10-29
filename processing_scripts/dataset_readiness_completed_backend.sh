#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv-preprocessing/bin/python"
ACTION="run"
if [[ "${1:-}" == "--list" || "${1:-}" == "--pending" || "${1:-}" == "--force" ]]; then ACTION="$1"; shift; fi
MODE="${1:-link}"; WORKERS="${2:-2}"
if [[ $# -gt 2 || ( "$MODE" != "link" && "$MODE" != "copy" ) || ! "$WORKERS" =~ ^[1-8]$ ]]; then
    echo "Usage: $0 [--list|--pending|--force] [link|copy] [WORKERS]" >&2; exit 2
fi
if [[ ! -x "$PYTHON" ]]; then echo "Missing environment: $PYTHON" >&2; exit 1; fi
export READINESS_ACTION="$ACTION" READINESS_MODE="$MODE" READINESS_WORKERS="$WORKERS" READINESS_ROOT="$PROJECT_ROOT"
cd "$PROJECT_ROOT"
exec "$PYTHON" -u - <<'PY'
import os, sqlite3, subprocess
from pathlib import Path
from processing_scripts.dataset_readiness_system import current_export_exists, initialize
root=Path(os.environ['READINESS_ROOT']); db=Path(os.getenv('APP_DB_PATH',root/'data'/'app.db'))
output=Path(os.getenv('DATASET_READY_DIR',root/'dataset_ready'))
action=os.environ['READINESS_ACTION']; mode=os.environ['READINESS_MODE']; workers=os.environ['READINESS_WORKERS']
with (root/'sentences.csv').open(encoding='utf-8-sig') as source:
 expected=sum(1 for line in source if line.strip())-1
if expected <= 0: raise SystemExit('No sentences were found in sentences.csv.')
initialize(db)
c=sqlite3.connect(db); c.row_factory=sqlite3.Row
codes=[r['participant_code'] for r in c.execute(
 """SELECT participant_code FROM postprocessing_jobs j
    WHERE status='completed' AND failed_files=0 AND total_files=? AND completed_files=?
      AND id=(SELECT id FROM postprocessing_jobs WHERE participant_code=j.participant_code ORDER BY created_at DESC LIMIT 1)
    ORDER BY created_at""",(expected,expected))]
c.close()
print(f'Found {len(codes)} participants with current completed {expected}/{expected} post-processing.',flush=True)
if action=='--list': print(' '.join(codes)); raise SystemExit(0)
pending=[code for code in codes if action=='--force' or not current_export_exists(db,output,code,mode)]
if action=='--pending':
 print(f'{len(pending)} participants need a {mode}-mode dataset export.'); print(' '.join(pending)); raise SystemExit(0)
completed=failed=skipped=0
for index,code in enumerate(codes,1):
 if code not in pending:
  skipped+=1; print(f'[{index}/{len(codes)}] Skipping {code}: current export exists.',flush=True); continue
 print(f'[{index}/{len(codes)}] Exporting {code}...',flush=True)
 result=subprocess.run([str(root/'processing_scripts'/'dataset_readiness_backend.sh'),code,mode,workers],cwd=root)
 if result.returncode==0: completed+=1
 else: failed+=1; print(f'  {code}: failed; continuing.',flush=True)
print(f'Batch finished: {completed} completed, {failed} failed, {skipped} skipped.',flush=True)
raise SystemExit(1 if failed else 0)
PY
