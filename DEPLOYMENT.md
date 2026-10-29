# Running the recording service under concurrent load

## What changed

- Uploads are written in 1 MiB chunks, then accepted with HTTP 202 and a job ID.
- A SQLite-backed queue persists accepted jobs. Conversion runs in background threads, with a database-enforced limit across all web workers on this host.
- The browser polls for completion and advances only after all output files and the recording row are saved. Refreshing resumes pending processing. A full queue returns 503; the browser retains the clip so Save & Continue can be retried.
- An interrupted processing job becomes eligible again after its lease expires (12 minutes with default settings). Queued jobs are available immediately after restart. Each processing attempt uses separate output paths, so a failed conversion cannot overwrite a valid recording.
- SQLite uses WAL and a 30-second busy timeout. Transactions do not stay open during conversion. Sentence CSV parsing is cached by file modification time.
- Admin requests no longer inspect old videos using FFprobe. Existing missing duration values can be backfilled separately during a maintenance window.

This improves request responsiveness under media-processing load. It is **not a verified capacity of 1,000 active participants**. Processing and network throughput still determine waiting time.

## Install and start

Use Python 3.10 or newer and install FFmpeg/FFprobe on the host. From this directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export PYTHON_BIN="$PWD/.venv/bin/python"
export SESSION_SECRET_KEY="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
read -rsp 'Admin password: ' ADMIN_PASSWORD
export ADMIN_PASSWORD
bash scripts/start-production.sh
```

Store the generated session secret and admin password in your service's protected environment configuration for subsequent starts. Changing the session secret signs everyone out. Run the startup command through your service manager so it restarts after a crash. Do not use `--reload` in production.

The default listener is `127.0.0.1:8000`; put your HTTPS reverse proxy in front of it. Camera access for remote participants requires a suitable secure browser context. For an existing Nginx HTTPS server block, an example location is:

```nginx
location / {
    client_max_body_size 101m;
    client_body_timeout 120s;
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
}
```

The proxy request-body limit matters: multipart parsing happens before the application endpoint checks the individual video size. Provide enough disk capacity for both proxy temporary files and application uploads. This example is configuration guidance, not an installed proxy change.

## Tuning

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `WEB_WORKERS` | 4 | Web processes started by the production script |
| `MEDIA_LOCAL_WORKERS` | 2 | Background processing threads per web process |
| `MEDIA_MAX_ACTIVE` | 4 | Maximum active media jobs across this database |
| `MEDIA_MAX_PENDING` | 1000 | Maximum queued plus active jobs |
| `FFMPEG_THREADS` | 2 | FFmpeg input decoding and H.264 encoding thread setting |
| `FFMPEG_TIMEOUT` | 180 | Timeout in seconds for each conversion command |
| `MAX_UPLOAD_MB` | 100 | Maximum video upload size in MiB |

Keep settings consistent across web processes. Setting `MEDIA_LOCAL_WORKERS=0` disables processing in that process; at least one process must have workers enabled. Increasing `WEB_WORKERS` does not increase the global conversion limit. FFmpeg can have additional helper threads, so this is not an exact CPU-thread ceiling.

Use a **local filesystem on one host** for the SQLite database and recordings. This design is not intended for independent servers sharing SQLite over network storage. For multiple hosts, use a central job broker, a server database, and shared object storage.

Output filenames still include `_XA_YB` and other answer codes. New files are nested under `recordings/<participant>/<job>/<attempt>/`; admin playback reads their saved database paths. Existing files and paths remain valid. External file collection scripts must search recursively.

Back up `data/` and `recordings/` together, using a SQLite-aware backup or while the service is stopped. WAL has sidecar files; copying only a live `app.db` is not a complete backup. Failed job inputs and uploads interrupted before enqueue are retained under `recordings/.uploads/`; review them during maintenance rather than deleting active inputs. Replaced successful recordings and job history are retained, so monitor disk usage and establish an archival policy.

## Validation and rollout

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
node tests/test_upload_frontend.js
```

Use Node.js 18 or newer for the browser-script tests. After deployment, refresh any already-open participant pages so they use the new queue-aware script.

Tests use temporary databases and recordings. They cover duplicate submission, queue capacity, global processing limits, lease recovery, invalid video, real FFmpeg conversion, retakes, output filenames, and concurrent API requests during processing. They are not a production benchmark.

Before inviting 1,000 people, load-test a staging deployment with realistic clip sizes and participant save intervals. Ramp through 25, 100, 250, 500, then 1,000 simulated participants. Measure upload latency, processing wait, error rate, CPU, memory, disk free space/throughput, and network bandwidth. Increase conversion concurrency gradually only if CPU and disk measurements support it. A queue that grows continuously means arrival rate exceeds processing capacity, even if login remains responsive.

Perform a real browser test over the deployed HTTPS URL: login, fill the form, record, save, refresh while queued, verify completion, request a retake, and save again. Browser camera/microphone behavior is not covered by the server tests.
