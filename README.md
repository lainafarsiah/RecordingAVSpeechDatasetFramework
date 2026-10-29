# Audio-Visual Sentence Recording Framework

A self-hosted browser application for collecting prompted audio-visual speech,
reviewing participant progress, requesting retakes, evaluating recordings with
ASR and technical checks, generating noise-reduced derivatives, producing
MediaPipe face and mouth crops, and exporting synchronized AVSE-ready datasets.

This repository contains framework code only. It contains no participant
database, recordings, generated reports, signed consent forms, investigator
signatures, or study sentence inventory.

## Features

- Browser camera and microphone recording with preview and retake
- Participant credentials and resumable progress
- Persistent SQLite upload queue and FFmpeg media conversion
- Administrator review, validation, notes, single/bulk retakes, and backups
- Versioned evaluation reports with brightness, level, WER, and CER checks
- Shared GPU faster-whisper API for evaluation and preprocessing
- RNNoise clean video/audio generation with timing verification
- Two-pass EBU R128 loudness normalization
- MediaPipe 512x512 face and 112x112 mouth derivatives
- AVSE export containing video, audio, text, face, mouth, and manifests

## Architecture

```text
Browser capture
  -> recordings/             original MP4, WAV, silent video, reference text
  -> evaluation_reports/     technical checks and ASR reports
  -> recording_clean/        RNNoise clean video/audio derivatives
  -> recording_processed/    normalized video and face/mouth crops
  -> dataset_ready/          video/audio/text/face/mouth export
```

SQLite stores metadata and job state. Media stays on the local filesystem. The
current design targets one host and does not support multiple independent web
servers sharing SQLite over network storage.

All evaluation, preprocessing, post-processing, ASR, and dataset-readiness
Python and shell entry points are grouped in `processing_scripts/`. Run the
shell commands from the repository root as shown below.

## Requirements

- Linux
- Python 3.10
- FFmpeg and FFprobe, with `arnndn`, `loudnorm`, and H.264 support
- Node.js 18 or newer for JavaScript tests
- HTTPS for remote browser camera/microphone access
- Optional NVIDIA GPU for faster-whisper

On Ubuntu:

```bash
sudo apt update
sudo apt install python3.10-venv ffmpeg nodejs
```

Verify required FFmpeg filters:

```bash
ffmpeg -filters | grep -E 'arnndn|loudnorm'
```

## Configure the study

1. Replace `sentences.csv` with the approved sentence inventory:

   ```csv
   final_id;sentence
   ID0001;First approved sentence.
   ID0002;Second approved sentence.
   ```

2. Review and customize the participant fields and instructions in
   `static/index.html`.
3. Obtain ethics approval and configure the approved consent materials as
   described in `consent_forms/README.md`.
4. Review evaluation thresholds in `processing_scripts/dataset_evaluation.py`. Defaults are
   screening rules and are not universal perceptual-quality standards.

Do not begin participant collection using the included example sentences or
placeholder consent page.

## Web application setup

```bash
python3.10 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Set secure production credentials without committing them:

```bash
export PYTHON_BIN="$PWD/.venv/bin/python"
export SESSION_SECRET_KEY="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
read -rsp 'Admin password: ' ADMIN_PASSWORD
export ADMIN_PASSWORD
```

The web settings are centralized in `config.py`. Change `ADMIN_BASE_PATH`
there for local use, or set it together with the other values in `.env` for a
production deployment.

Start the service:

```bash
bash scripts/start-production.sh
```

Default URLs:

- Participant login: `http://127.0.0.1:8000/`
- Administrator: `http://127.0.0.1:8000/admin`

The application creates `data/app.db` and required output directories on first
start. Put an HTTPS reverse proxy in front of the default loopback listener for
remote use. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Participant accounts

Sign in as administrator and open **Participant Login**. Create participant
codes and passwords there. Give each participant only their own credentials.

## ML environment

Evaluation, preprocessing, and post-processing use one isolated environment:

```bash
python3.10 -m venv .venv-preprocessing
.venv-preprocessing/bin/pip install --upgrade pip
.venv-preprocessing/bin/pip install -r requirements.txt
```

The first faster-whisper run downloads the selected model weights. Review
upstream licenses and provision the model cache before production if the server
has restricted network access.

### Shared ASR API

```bash
./processing_scripts/asr_api_backend.sh 0 8765 2
```

Arguments are GPU ID, local port, and concurrent inference slots. The service
binds to loopback and creates `data/asr_api.token` with restrictive permissions.
See [ASR_API.md](ASR_API.md).

## Backend processing

Run one participant:

```bash
./processing_scripts/evaluation_backend.sh P0001 0
./processing_scripts/preprocessing_backend.sh P0001 2 0
./processing_scripts/postprocessing_backend.sh P0001 2
./processing_scripts/dataset_readiness_backend.sh P0001 link 2
```

Run all participants who completed the configured inventory:

```bash
./processing_scripts/evaluation_completed_backend.sh
./processing_scripts/preprocessing_completed_backend.sh 2 0
./processing_scripts/postprocessing_completed_backend.sh 2
./processing_scripts/dataset_readiness_completed_backend.sh link 2
```

Each batch script supports `--list` and `--pending`; processing and export
scripts also document `--force` where supported. See:

- [EVALUATION.md](EVALUATION.md)
- [PREPROCESSING.md](PREPROCESSING.md)
- [POSTPROCESSING.md](POSTPROCESSING.md)
- [DATASET_READINESS.md](DATASET_READINESS.md)

`link` mode uses hard links for MP4 files to avoid duplicate storage. A normal
copy, archive, or `rsync` transfer reads them as complete files. Use `copy` mode
when an independent tree is required on the same filesystem.

## Tests

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
node tests/test_upload_frontend.js
```

Some integration tests require FFmpeg.

## Security and privacy

- Production startup rejects missing session and administrator credentials.
- Use HTTPS and restrict access to generated files and backups.
- Back up `data/` and `recordings/` together using a SQLite-aware procedure.
- Do not publish participant media, metadata, consent records, ASR tokens, or
  evaluation reports.
- Treat face and voice recordings as sensitive identifiable data.
- Review [SECURITY.md](SECURITY.md) before deployment.

The application has not received an independent security audit. Operators are
responsible for institutional policy, consent, retention, withdrawal, access
control, and applicable law.

## License

Framework code is available under the [MIT License](LICENSE). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for bundled and downloaded
components.
