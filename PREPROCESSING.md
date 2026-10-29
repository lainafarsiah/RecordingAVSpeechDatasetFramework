# Participant preprocessing

The preprocessing subsystem is isolated from live recording uploads. The web app
displays persistent results. A server command starts the selected participant
immediately, without a preprocessing queue. Multiple recordings can run in
parallel. A participant-specific lock prevents duplicate commands for the same
participant, while different participants may run concurrently. Source files in
`recordings/` are never modified.

## Pipeline

For each current, non-retake recording, the worker:

1. verifies that the source file and database row still match the snapshot taken
   when the job was queued;
2. runs FFmpeg `arnndn` on the audio while copying the original video stream;
3. extracts a 16 kHz mono lossless WAV and a silent MP4 from the clean MP4;
4. compares source and clean timing, including audio/video start-offset changes;
5. measures clipping, level, and silence on source and clean PCM audio;
6. transcribes only the clean WAV with faster-whisper `large-v3`, language `id`;
7. calculates WER and CER against the expected sentence; and
8. writes per-sentence JSON reports plus a participant manifest.

Results are stored in:

```text
recording_clean/P0001/
├── files/
│   ├── <base>_clean.mp4
│   ├── <base>_clean_audio.wav
│   ├── <base>_clean_videoonly.mp4
│   └── <base>_preprocessing.json
└── manifest.json
```

The clean MP4 audio is AAC at 192 kb/s for browser compatibility. The extracted
WAV is lossless relative to that clean MP4. The original source remains the
archival reference.

## Install without interrupting the live service

Create a separate environment. This does not alter the web application's current
environment:

```bash
python3 -m venv .venv-preprocessing
.venv-preprocessing/bin/pip install -r requirements.txt
```

This same environment is used for dataset evaluation. On Linux x86-64, the
requirements include CUDA 12 cuBLAS and cuDNN 9 for CTranslate2 GPU inference.

The baseline FFmpeg-compatible RNNoise model is included at
`models/rnnoise.rnnn`; its source and SHA-256 hash are recorded in
`models/README.md`. Set `RNNOISE_MODEL_PATH` to use a different validated model.

The first worker start downloads the CTranslate2 `large-v3` model if it is not
already cached. Do this before a collection session when possible because the
download and model initialization are substantial.

Process one participant directly on the server:

```bash
./processing_scripts/preprocessing_backend.sh P0001 2 0
```

Arguments are participant ID, recording workers, and physical GPU ID.

Process every participant who currently has the complete sentence inventory:

```bash
./processing_scripts/preprocessing_completed_backend.sh 2 0
```

The two arguments are the number of concurrent recordings per participant and
the physical GPU ID. Participants are processed sequentially so the shared ASR
service and storage are not overloaded. A participant is skipped when a
completed preprocessing run matches all current source files. Useful inspection
and override modes are:

```bash
./processing_scripts/preprocessing_completed_backend.sh --list
./processing_scripts/preprocessing_completed_backend.sh --pending
./processing_scripts/preprocessing_completed_backend.sh --force 2 0
```

`--list` shows every participant with 200 current recordings. `--pending` shows
only those without current completed preprocessing. `--force` reruns all
eligible participants.

When the persistent service described in `ASR_API.md` is running, preprocessing
sends each cleaned WAV to that service instead of loading another large-v3 model.

Then sign in as admin and open `/admin/preprocessing.html` to review the
overall WER/CER, per-sentence transcript checks, quality measurements, and
side-by-side original and cleaned video. The page does not start processing.

The current server has no working NVIDIA driver, so defaults use CPU `int8`.
Large-v3 on CPU can be slow. Keep one worker only. If a compatible CUDA runtime
is later installed, use `WHISPER_DEVICE=cuda` and
`WHISPER_COMPUTE_TYPE=float16` after testing on the deployment host.

## Review policy

Automatic flags are screening signals, not ground truth:

- transcript mismatch: WER greater than 0.25;
- timing change: audio/video offset over 50 ms or duration change over 100 ms;
- clipping: more than 0.1% of PCM samples near full scale;
- very quiet: RMS below -35 dBFS;
- mostly silence: more than 80% of 20 ms windows below -45 dBFS.

Calibrate these thresholds on a representative Indonesian pilot set. Whisper can
misrecognize accents and noise, so flagged recordings require human review.

## Operational behavior

- The command processes exactly one participant per invocation and never waits
  in a preprocessing queue.
- `--workers` controls concurrent recordings within that participant (1–16).
- Separate commands for different participants can run concurrently.
- Only one active job is allowed per participant.
- Jobs survive browser closure and server restarts.
- A worker interrupted for ten minutes releases the job for retry.
- Completed sentence results are retained; retrying an interrupted participant
  safely replaces only finished output files after each new result is verified.
- Cancel stops between recordings and retains completed clean files.
- A retake or replaced source causes that sentence to fail safely rather than
  processing stale media.
- Output file downloads require the admin session.

After installing the code, restart the web service during a planned maintenance
window so it creates the additive preprocessing tables and exposes the new page.
Do not restart while participants are actively uploading.
