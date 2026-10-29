# Post-processing

This stage reads completed videos from `recording_clean` and writes new files to
`recording_processed`. It never changes original or clean recordings.

For each clean video it performs two-pass FFmpeg EBU R128 normalization at
`-23 LUFS`, limits true peak to `-1 dBTP`, converts video to 25 fps, detects one
face with MediaPipe Face Mesh, smooths crop coordinates over 15 frames, and
creates synchronized 512x512 face and 112x112 mouth videos. The same normalized
audio is stream-copied into both cropped videos.

Outputs are stored in `recording_processed/P0001/files/`:

```text
*_normalized.mp4
*_face_512.mp4
*_mouth_112.mp4
*_postprocessing.json
```

Run one participant after their preprocessing is complete:

```bash
./processing_scripts/postprocessing_backend.sh P0001 2
```

List and run participants whose latest preprocessing completed the full configured sentence inventory:

```bash
./processing_scripts/postprocessing_completed_backend.sh --list
./processing_scripts/postprocessing_completed_backend.sh --pending
./processing_scripts/postprocessing_completed_backend.sh 2
```

Completed current outputs are skipped. To intentionally regenerate every
eligible participant:

```bash
./processing_scripts/postprocessing_completed_backend.sh --force 2
```

MediaPipe uses CPU. Start with two workers; increasing the count raises CPU,
memory, encoder load, and disk I/O. No ASR API or GPU is required for this stage.
