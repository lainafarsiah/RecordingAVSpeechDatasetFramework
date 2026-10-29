# Persistent ASR API

The local ASR service loads faster-whisper `large-v3` once and shares it between
dataset evaluation and preprocessing. It binds to localhost, validates input
paths, requires a generated bearer token, and limits concurrent GPU requests.

Start one service on GPU 0:

```bash
./processing_scripts/asr_api_backend.sh 0 8765 2
```

Arguments are GPU ID, TCP port, and maximum concurrent transcriptions. Wait for
Uvicorn to report that startup is complete before launching jobs. Keep this
terminal running, or manage the command with a process supervisor.

Check readiness:

```bash
curl http://127.0.0.1:8765/health
```

Once `data/asr_api.token` exists, `processing_scripts/evaluation_backend.sh`,
`processing_scripts/preprocessing_backend.sh`, and web-launched jobs automatically use the local
service at port 8765. If a different port is used, set `ASR_API_URL` for the web
service and backend command.

The API accepts files only under `recordings/` and `recording_clean/`. Override
this with the colon-separated `ASR_ALLOWED_ROOTS` environment variable.

To return temporarily to one model per job, move `data/asr_api.token` out of the
project and unset `ASR_API_URL` before launching the job.
