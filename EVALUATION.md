# Dataset evaluation reports

Administrators can start an evaluation from the **Generate Report** button for a
participant or from `/admin/report.html`. The web application launches
`processing_scripts/dataset_evaluation.py` as a separate low-priority process, so the report does
not block participant requests.

Evaluation and preprocessing share `.venv-preprocessing/bin/python`, which
provides OpenCV, NumPy, faster-whisper, CTranslate2, jiwer, and the CUDA 12
cuBLAS/cuDNN runtime packages. Override it with `EVALUATION_PYTHON` or
`PREPROCESSING_PYTHON` when deploying elsewhere.

The web launcher adds the environment's CUDA library directories to
`LD_LIBRARY_PATH`. After the NVIDIA driver is healthy, enable GPU evaluation
with `EVALUATION_DEVICE=cuda` and `EVALUATION_COMPUTE_TYPE=float16`.

Start a database-backed GPU evaluation directly on the server with:

```bash
./processing_scripts/evaluation_backend.sh P0001 0
```

Arguments are participant ID and physical GPU ID. The command starts a
background evaluation whose progress and result appear on the Evaluation
Reports page.

When the persistent service described in `ASR_API.md` is running, evaluation
sends audio to that service instead of loading another large-v3 model.

## Evaluate completed participants from the backend

Use `processing_scripts/evaluation_completed_backend.sh` to evaluate participants who have exactly
200 distinct current recordings. The launcher processes one participant at a
time and waits for each evaluation to finish before starting the next one. All
evaluations share the persistent ASR API model.

Start the ASR API first if it is not already running:

```bash
./processing_scripts/asr_api_backend.sh 0 8765 2
```

The batch launcher provides these commands:

```bash
# List every participant with 200/200 current recordings.
./processing_scripts/evaluation_completed_backend.sh --list

# List only participants who need a new evaluation.
./processing_scripts/evaluation_completed_backend.sh --pending

# Evaluate only participants who need a new evaluation.
./processing_scripts/evaluation_completed_backend.sh

# Generate a new report for every eligible participant.
./processing_scripts/evaluation_completed_backend.sh --force
```

For a batch that must survive an SSH disconnection:

```bash
nohup ./processing_scripts/evaluation_completed_backend.sh > evaluation_completed.log 2>&1 &
tail -f evaluation_completed.log
```

The normal batch command skips a participant only when the latest completed
report:

- contains the complete sentence inventory configured in `sentences.csv`;
- references the same recording file paths currently stored in the database;
- uses the current WER, CER, brightness, and volume thresholds; and
- exists as valid JSON on disk.

A retake changes the recording path, so the participant automatically becomes
pending again. Changing an evaluation threshold also makes an older report
pending. Immediately before starting each participant, the launcher rechecks
the recording count and skips anyone with a pending upload or fewer than 200
current recordings.

Do not put `processing_scripts/evaluation_backend.sh` in a simple shell loop. That command launches
a detached evaluation and returns immediately, so a loop would start many
large evaluations simultaneously.

Only current recording rows in `data/app.db` are evaluated; recordings marked
`retake_requested` and older upload attempts are excluded. A participant with an
upload still in progress cannot be evaluated.

Versioned results are written to:

```text
evaluation_reports/<participant>/<run-id>/
├── records/
│   └── <recording>_report.json
└── <participant>_master_evaluation_summary.json
```

The database table `evaluation_runs` stores progress and the master-summary path.
Media and JSON content remain filesystem files rather than database blobs.

## Current evaluation thresholds

```text
Maximum WER:        0.25 (25%)
CER review:         above 0.10 through 0.20
CER flag:           above 0.20
Minimum brightness: 80
Maximum brightness: 180
Minimum mean volume: -45 dB
```

The volume rule uses FFmpeg whole-clip mean level, including pauses. The
`-45 dB` value is a corpus-derived conservative screening threshold near the
tenth percentile of the observed recordings; it is not a universal active
speech or perceptual-quality standard.

Transcript decisions use CER as the primary measure. CER at or below 10% passes;
CER above 10% through 20% requires review; and CER above 20% is flagged. A
recording is also flagged when WER is above 25% and CER is above 10%.

Defaults use faster-whisper `large-v3` on CPU with `int8`. These may be changed
with `EVALUATION_DEVICE` and `EVALUATION_COMPUTE_TYPE`. Run one report at a time
on a busy recording server unless resource measurements show sufficient spare
CPU and memory.
