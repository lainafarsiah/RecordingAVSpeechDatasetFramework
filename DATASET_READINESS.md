# AVSE dataset readiness export

This export reads only completed post-processing outputs. For each participant it
creates `video`, `audio`, `text`, `face`, and `mouth` directories under
`dataset_ready/<participant>/`. Full canonical filenames are retained.

```text
dataset_ready/P0001/video/P0001_ID0001_2_m_E_XC_YC_video.mp4
dataset_ready/P0001/audio/P0001_ID0001_2_m_E_XC_YC_audio.wav
dataset_ready/P0001/text/P0001_ID0001_2_m_E_XC_YC_text.txt
dataset_ready/P0001/face/P0001_ID0001_2_m_E_XC_YC_face.mp4
dataset_ready/P0001/mouth/P0001_ID0001_2_m_E_XC_YC_mouth.mp4
```

Video is the normalized full-frame MP4. Audio is extracted from that exact
timeline as mono 16-kHz 16-bit PCM WAV. Text is the reference sentence from the
current recording row. Face and mouth are the synchronized MediaPipe outputs.

Run one participant:

```bash
./processing_scripts/dataset_readiness_backend.sh P0001 link 2
```

`link` creates hard links for MP4 files and avoids duplicate storage. WAV and
text files are generated normally. Use `copy` for an independent portable tree:

```bash
./processing_scripts/dataset_readiness_backend.sh P0001 copy 2
```

Batch commands for participants with completed 200/200 post-processing:

```bash
./processing_scripts/dataset_readiness_completed_backend.sh --list
./processing_scripts/dataset_readiness_completed_backend.sh --pending link
./processing_scripts/dataset_readiness_completed_backend.sh link 2
./processing_scripts/dataset_readiness_completed_backend.sh --force copy 2
```

Use one worker while participants are actively recording to reduce contention
with upload conversion:

```bash
./processing_scripts/dataset_readiness_completed_backend.sh link 1
```

The backend runs exports with idle disk priority and reduced CPU priority.

Every participant directory contains `manifest.json` with source job IDs,
reference text, post-processing measurements, materialization method, and
SHA-256 hashes for all five modalities.

If a process or terminal closes during an export, run the same command again.
The participant lock prevents concurrent exports; after acquiring it, the
worker marks the abandoned database run as interrupted and safely rebuilds the
participant. Completed current exports are skipped by the batch command.
