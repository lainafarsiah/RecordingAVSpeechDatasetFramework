import csv
import io
import json
import string
import os
import sqlite3
import subprocess
import base64
import zipfile
import shutil
import tempfile
import urllib.error
import urllib.request
from contextlib import closing
from functools import lru_cache
from media_jobs import MediaQueue, QueueFull
from processing_scripts.preprocessing_system import (
    get_job as get_preprocessing_job,
    initialize as initialize_preprocessing,
    list_participants as list_preprocessing_participants,
)
from processing_scripts.dataset_readiness_system import initialize as initialize_readiness
from io import BytesIO
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

import secrets
import hmac
from fastapi import Request, Form, Depends
from starlette.middleware.sessions import SessionMiddleware
from config import ADMIN_BASE_PATH, ADMIN_PASSWORD, SESSION_SECRET_KEY

# =========================
# Basic configuration
# =========================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
RECORDINGS_DIR = BASE_DIR / "recordings"
CSV_PATH = BASE_DIR / "sentences.csv"
DB_PATH = DATA_DIR / "app.db"
BACKUPS_DIR = BASE_DIR / "backups"
CLEAN_RECORDINGS_DIR = Path(os.getenv("CLEAN_RECORDINGS_DIR", str(BASE_DIR / "recording_clean")))
LEGACY_CLEAN_RECORDINGS_DIR = BASE_DIR / "recording clean"
EVALUATION_REPORTS_DIR = Path(os.getenv("EVALUATION_REPORTS_DIR", str(BASE_DIR / "evaluation_reports")))
DATASET_READY_DIR = Path(os.getenv("DATASET_READY_DIR", str(BASE_DIR / "dataset_ready")))
LEGACY_EVALUATION_REPORTS_DIR = BASE_DIR / "evaluation reports"
SHARED_ML_PYTHON = BASE_DIR / ".venv-preprocessing" / "bin" / "python"
EVALUATION_PYTHON = Path(os.getenv("EVALUATION_PYTHON", str(SHARED_ML_PYTHON)))
PREPROCESSING_PYTHON = Path(os.getenv("PREPROCESSING_PYTHON", str(SHARED_ML_PYTHON)))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024
FFMPEG_THREADS = max(1, int(os.getenv("FFMPEG_THREADS", "2")))
FFMPEG_TIMEOUT = max(1, int(os.getenv("FFMPEG_TIMEOUT", "180")))
media_queue = None

DATA_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

CONSENT_VERSION = os.getenv("CONSENT_VERSION", "replace-with-approved-version")
CONSENT_TEXT_PATH = os.getenv("CONSENT_TEXT_PATH", "consent_forms/consent.pdf")
CONSENT_TEMPLATE_PATH = BASE_DIR / CONSENT_TEXT_PATH
EXPLAINER_SIGNATURE_PATH = BASE_DIR / os.getenv(
    "EXPLAINER_SIGNATURE_PATH", "consent_forms/explainer_signature.png"
)
SIGNED_CONSENT_DIR = BASE_DIR / "output" / "icf"
SIGNED_CONSENT_DIR.mkdir(parents=True, exist_ok=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global media_queue
    init_db()
    media_queue = MediaQueue(
        DB_PATH, process_media_job,
        max_active=max(1, int(os.getenv("MEDIA_MAX_ACTIVE", "4"))),
        max_pending=max(1, int(os.getenv("MEDIA_MAX_PENDING", "1000"))),
        lease_seconds=3 * FFMPEG_TIMEOUT + 180,
        local_workers=max(0, int(os.getenv("MEDIA_LOCAL_WORKERS", "2")))
    )
    media_queue.initialize()
    media_queue.start()
    try:
        yield
    finally:
        media_queue.shutdown()

app = FastAPI(title="Audio-Visual Sentence Recording App", lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)

# =========================
# Database
# =========================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ml_subprocess_environment():
    """Expose CUDA libraries installed inside the shared ML virtual environment."""
    environment = os.environ.copy()
    site_packages = SHARED_ML_PYTHON.parent.parent / "lib" / "python3.10" / "site-packages"
    cuda_libraries = [
        site_packages / "nvidia" / package / "lib"
        for package in ("cublas", "cudnn", "cuda_nvrtc")
    ]
    available = [str(path) for path in cuda_libraries if path.is_dir()]
    existing = environment.get("LD_LIBRARY_PATH", "")
    if existing:
        available.append(existing)
    if available:
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(available)
    token_file = DATA_DIR / "asr_api.token"
    if token_file.is_file() and "ASR_API_URL" not in environment:
        environment["ASR_API_URL"] = "http://127.0.0.1:8765"
        environment["ASR_API_TOKEN"] = token_file.read_text(encoding="utf-8").strip()
    return environment


def require_asr_service_if_configured():
    token_file = DATA_DIR / "asr_api.token"
    if not token_file.is_file():
        return
    url = os.getenv("ASR_API_URL", "http://127.0.0.1:8765").rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=3) as response:
            health = json.loads(response.read().decode("utf-8"))
        if not health.get("ready"):
            raise RuntimeError("model is still loading")
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"Persistent ASR service is not ready at {url}. Start processing_scripts/asr_api_backend.sh first.",
        ) from error


def migrate_legacy_evaluation_reports():
    """Move finished legacy report folders without touching active evaluations."""
    # Never migrate production files while a test or alternate database is active.
    if DB_PATH.resolve() != (BASE_DIR / "data" / "app.db").resolve():
        return
    if not LEGACY_EVALUATION_REPORTS_DIR.is_dir():
        return
    EVALUATION_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with closing(get_db()) as conn:
        active_codes = {
            row[0] for row in conn.execute(
                "SELECT participant_code FROM evaluation_runs WHERE status='running'"
            )
        }
    for source in LEGACY_EVALUATION_REPORTS_DIR.iterdir():
        if not source.is_dir() or source.name in active_codes:
            continue
        destination = EVALUATION_REPORTS_DIR / source.name
        if destination.exists():
            continue
        shutil.move(str(source), str(destination))
        old_prefix = str(source)
        new_prefix = str(destination)
        with closing(get_db()) as conn, conn:
            conn.execute(
                """UPDATE evaluation_runs
                   SET summary_path=replace(summary_path, ?, ?),
                       log_path=replace(log_path, ?, ?)
                   WHERE participant_code=?""",
                (old_prefix, new_prefix, old_prefix, new_prefix, source.name),
            )


def backfill_evaluation_averages():
    """Copy list-page aggregates from existing reports into lightweight columns."""
    with closing(get_db()) as conn:
        rows = conn.execute(
            """SELECT id, summary_path FROM evaluation_runs
               WHERE status='completed' AND summary_path!=''
                 AND (overall_wer IS NULL OR overall_cer IS NULL)"""
        ).fetchall()
    updates = []
    for row in rows:
        try:
            report = json.loads(Path(row["summary_path"]).read_text(encoding="utf-8"))
            averages = report.get("dataset_averages", {})
            wer = averages.get("average_word_error_rate")
            cer = averages.get("average_character_error_rate")
            if wer is not None and cer is not None:
                updates.append((wer, cer, row["id"]))
        except (OSError, json.JSONDecodeError):
            continue
    if updates:
        with closing(get_db()) as conn, conn:
            conn.executemany(
                "UPDATE evaluation_runs SET overall_wer=?, overall_cer=? WHERE id=?",
                updates,
            )


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("BEGIN IMMEDIATE")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_code TEXT UNIQUE NOT NULL,
            participant_name TEXT NOT NULL DEFAULT '',
            age_group TEXT NOT NULL,
            gender TEXT NOT NULL,
            native_language TEXT NOT NULL,
            indonesian_accent TEXT NOT NULL DEFAULT 'unknown_unsure',
            consent_agreed INTEGER NOT NULL,
            consent_version TEXT NOT NULL,
            consent_timestamp TEXT NOT NULL,
            consent_text_path TEXT NOT NULL,
            consent_pdf_path TEXT NOT NULL DEFAULT '',
            record_date TEXT NOT NULL,
            recording_time TEXT NOT NULL
        )
    """)

    cur.execute("PRAGMA table_info(participants)")
    participant_columns = {row["name"] for row in cur.fetchall()}
    if "admin_validated" not in participant_columns:
        cur.execute("ALTER TABLE participants ADD COLUMN admin_validated INTEGER NOT NULL DEFAULT 0")

    for column in ("public_speaking_ability", "communication_style"):
        if column not in participant_columns:
            cur.execute(f"ALTER TABLE participants ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    if "indonesian_accent" not in participant_columns:
        cur.execute("""
            ALTER TABLE participants
            ADD COLUMN indonesian_accent TEXT NOT NULL DEFAULT 'unknown_unsure'
        """)
    if "participant_name" not in participant_columns:
        cur.execute("""
            ALTER TABLE participants
            ADD COLUMN participant_name TEXT NOT NULL DEFAULT ''
        """)
    if "consent_pdf_path" not in participant_columns:
        cur.execute("""
            ALTER TABLE participants
            ADD COLUMN consent_pdf_path TEXT NOT NULL DEFAULT ''
        """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS participant_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_code TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signed_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_code TEXT UNIQUE NOT NULL,
            participant_name TEXT NOT NULL,
            consent_version TEXT NOT NULL,
            consent_timestamp TEXT NOT NULL,
            consent_pdf_path TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_code TEXT NOT NULL,
            sentence_id TEXT NOT NULL,
            sentence_text TEXT NOT NULL,
            original_mp4_path TEXT NOT NULL,
            audio_wav_path TEXT NOT NULL,
            video_only_mp4_path TEXT NOT NULL,
            text_path TEXT NOT NULL,
            recording_time TEXT NOT NULL,
            duration_seconds REAL DEFAULT 0,
            quality_status TEXT DEFAULT 'unchecked',
            quality_note TEXT DEFAULT '',
            UNIQUE(participant_code, sentence_id)
        )
    """)

    cur.execute("PRAGMA table_info(recordings)")
    recording_columns = {row["name"] for row in cur.fetchall()}
    if "duration_seconds" not in recording_columns:
        cur.execute("ALTER TABLE recordings ADD COLUMN duration_seconds REAL DEFAULT 0")

    cur.execute("""CREATE TABLE IF NOT EXISTS participant_backups (
        id TEXT PRIMARY KEY, participant_code TEXT NOT NULL,
        archive_path TEXT NOT NULL, created_at TEXT NOT NULL,
        recording_count INTEGER NOT NULL
    )""")

    cur.execute("CREATE INDEX IF NOT EXISTS recordings_participant_quality ON recordings(participant_code, quality_status)")
    cur.execute("""CREATE TABLE IF NOT EXISTS evaluation_runs (
        id TEXT PRIMARY KEY,
        participant_code TEXT NOT NULL,
        status TEXT NOT NULL,
        total_recordings INTEGER NOT NULL DEFAULT 0,
        completed_recordings INTEGER NOT NULL DEFAULT 0,
        summary_path TEXT NOT NULL DEFAULT '',
        log_path TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        pid INTEGER,
        created_at REAL NOT NULL,
        finished_at REAL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS evaluation_participant_created ON evaluation_runs(participant_code, created_at DESC)")
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS evaluation_active_participant
        ON evaluation_runs(participant_code) WHERE status='running'""")
    cur.execute("PRAGMA table_info(evaluation_runs)")
    evaluation_columns = {row["name"] for row in cur.fetchall()}
    if "overall_wer" not in evaluation_columns:
        cur.execute("ALTER TABLE evaluation_runs ADD COLUMN overall_wer REAL")
    if "overall_cer" not in evaluation_columns:
        cur.execute("ALTER TABLE evaluation_runs ADD COLUMN overall_cer REAL")
    conn.commit()
    conn.close()

    migrate_legacy_evaluation_reports()
    backfill_evaluation_averages()

    # Safe additive migration for the offline preprocessing subsystem. The live
    # recording path does not read or write these tables.
    initialize_preprocessing(DB_PATH)
    initialize_readiness(DB_PATH)


# =========================
# Sentence loader
# =========================

def require_admin(request: Request):
    if not request.session.get("is_admin"):
        raise HTTPException(status_code=401, detail="Admin login required.")
    return True

def require_participant(request: Request):
    participant_code = request.session.get("participant_code")
    if not participant_code:
        raise HTTPException(status_code=401, detail="Participant login required.")
    return participant_code

def load_sentences():
    return _load_sentences(str(CSV_PATH), CSV_PATH.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _load_sentences(csv_path, modified_at):
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Missing CSV file: {CSV_PATH}")

    sentences = []

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")

        required_cols = {"final_id", "sentence"}
        if not required_cols.issubset(reader.fieldnames):
            raise ValueError(
                f"CSV must contain columns: {required_cols}. "
                f"Found: {reader.fieldnames}"
            )

        for row in reader:
            sentence_id = row["final_id"].strip()
            sentence_text = row["sentence"].strip()

            if sentence_id and sentence_text:
                sentences.append({
                    "sentence_id": sentence_id,
                    "sentence_text": sentence_text
                })

    return sentences

def generate_next_participant_code():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT participant_code
        FROM (
            SELECT participant_code FROM participants
            UNION
            SELECT participant_code FROM participant_credentials
        )
        ORDER BY participant_code DESC
        LIMIT 1
    """)

    row = cur.fetchone()
    conn.close()

    if row is None:
        return "P0001"

    last_code = row["participant_code"]  # example: P0007

    try:
        last_number = int(last_code.replace("P", ""))
    except ValueError:
        last_number = 0

    next_number = last_number + 1

    return f"P{next_number:04d}"

def generate_participant_password(length=6):
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def get_sentence_by_id(sentence_id: str):
    sentences = load_sentences()
    for item in sentences:
        if item["sentence_id"] == sentence_id:
            return item
    return None

ALLOWED_INDONESIAN_ACCENTS = {
    "baku_netral",
    "jakarta_betawi",
    "sunda",
    "jawa_tengah_diy",
    "jawa_timur",
    "madura",
    "aceh",
    "sumatra_utara",
    "minangkabau",
    "riau_kepri",
    "jambi_sumsel_bengkulu",
    "lampung",
    "bangka_belitung",
    "kalimantan_barat",
    "kalimantan_tengah_selatan",
    "kalimantan_timur_utara",
    "sulawesi_utara_gorontalo",
    "sulawesi_tengah",
    "sulawesi_selatan_barat",
    "sulawesi_tenggara",
    "bali",
    "nusa_tenggara_barat",
    "nusa_tenggara_timur",
    "maluku",
    "papua",
    "campuran",
    "lainnya",
    "tidak_yakin",
}

AGE_GROUP_CODES = {
    "18-25": "1",
    "26-35": "2",
    "36-45": "3",
    "46-55": "4",
    "56+": "5"
}

GENDER_CODES = {
    "female": "f",
    "male": "m",
}

INDONESIAN_ACCENT_CODES = {
    "baku_netral": "A",
    "jakarta_betawi": "B",
    "sunda": "C",
    "jawa_tengah_diy": "D",
    "jawa_timur": "E",
    "madura": "F",
    "aceh": "G",
    "sumatra_utara": "H",
    "minangkabau": "I",
    "riau_kepri": "J",
    "jambi_sumsel_bengkulu": "K",
    "lampung": "L",
    "bangka_belitung": "M",
    "kalimantan_barat": "N",
    "kalimantan_tengah_selatan": "O",
    "kalimantan_timur_utara": "P",
    "sulawesi_utara_gorontalo": "Q",
    "sulawesi_tengah": "R",
    "sulawesi_selatan_barat": "S",
    "sulawesi_tenggara": "T",
    "bali": "U",
    "nusa_tenggara_barat": "V",
    "nusa_tenggara_timur": "W",
    "maluku": "X",
    "papua": "Y",
    "campuran": "Z",
    "lainnya": "AA",
    "tidak_yakin": "AB",
}

def decode_signature_data_url(signature_data_url):
    prefix = "data:image/png;base64,"
    if not signature_data_url.startswith(prefix):
        raise ValueError("Signature must be a PNG data URL.")
    return base64.b64decode(signature_data_url[len(prefix):], validate=True)


def stamp_signature(overlay, signature_bytes, x, y, width, height):
    from reportlab.lib.utils import ImageReader

    overlay.drawImage(
        ImageReader(BytesIO(signature_bytes)),
        x,
        y,
        width=width,
        height=height,
        preserveAspectRatio=True,
        mask="auto"
    )


def stamp_signature_file(overlay, signature_path, x, y, width, height):
    from reportlab.lib.utils import ImageReader

    overlay.drawImage(
        ImageReader(str(signature_path)),
        x,
        y,
        width=width,
        height=height,
        preserveAspectRatio=True,
        mask="auto"
    )


def stamp_checkmark(overlay, x, y, size=10):
    overlay.setLineWidth(1.2)
    overlay.line(x, y + size * 0.45, x + size * 0.35, y)
    overlay.line(x + size * 0.35, y, x + size, y + size)


def generate_signed_consent_pdf(
    participant_code,
    consent_timestamp,
    signature_data_url
):
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
    except ImportError as e:
        raise RuntimeError(
            "PDF signing dependencies are missing. Install pypdf and reportlab."
        ) from e

    if not CONSENT_TEMPLATE_PATH.exists():
        raise RuntimeError(f"Missing ICF template PDF: {CONSENT_TEMPLATE_PATH}")

    if not EXPLAINER_SIGNATURE_PATH.exists():
        raise RuntimeError(f"Missing explainer signature image: {EXPLAINER_SIGNATURE_PATH}")

    signature_bytes = decode_signature_data_url(signature_data_url)
    pdf_path = SIGNED_CONSENT_DIR / f"{participant_code}_signed_icf.pdf"

    reader = PdfReader(str(CONSENT_TEMPLATE_PATH))
    writer = PdfWriter()

    overlay_by_page = {}
    signature_date = consent_timestamp.split(" ")[0]

    def build_overlay(page_index):
        page = reader.pages[page_index]
        page_width = float(page.mediabox.width)
        page_height = float(page.mediabox.height)
        overlay_buffer = BytesIO()
        overlay = canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))
        overlay.setStrokeColorRGB(0, 0, 0)
        overlay.setFillColorRGB(0, 0, 0)

        if page_index == 4:
            stamp_checkmark(overlay, 75, 662)
            stamp_checkmark(overlay, 99, 455)
            stamp_checkmark(overlay, 99, 266)

        if page_index == 5:
            stamp_signature(overlay, signature_bytes, 200, 704, 150, 34)
            overlay.setFont("Helvetica", 11)
            overlay.drawString(390, 720, signature_date)

        if page_index == 7:
            stamp_signature(overlay, signature_bytes, 160, 382, 125, 34)
            overlay.setFont("Helvetica", 11)
            overlay.drawString(395, 394, signature_date)

            stamp_signature_file(overlay, EXPLAINER_SIGNATURE_PATH, 55, 346, 90, 30)
            overlay.setFont("Helvetica", 11)
            overlay.drawString(165, 329, signature_date)

        overlay.save()
        overlay_buffer.seek(0)
        return PdfReader(overlay_buffer).pages[0]

    for page_index in (4, 5, 7):
        overlay_by_page[page_index] = build_overlay(page_index)

    for index, page in enumerate(reader.pages):
        if index in overlay_by_page:
            page.merge_page(overlay_by_page[index])
        writer.add_page(page)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with open(pdf_path, "wb") as output_file:
        writer.write(output_file)

    return pdf_path


# =========================
# Participant code
# =========================

@app.get("/api/session/{participant_code}")
def api_get_session(
    participant_code: str,
    logged_participant_code: str = Depends(require_participant)
):
    if participant_code != logged_participant_code:
        raise HTTPException(status_code=403, detail="Cannot access another participant session.")

    sentences = load_sentences()

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT participant_code
        FROM participants
        WHERE participant_code = ?
    """, (participant_code,))

    participant = cur.fetchone()

    if participant is None:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail=f"Participant not found: {participant_code}"
        )

    cur.execute("""
        SELECT sentence_id
        FROM recordings
        WHERE participant_code = ?
          AND quality_status != 'retake_requested'
    """, (participant_code,))

    recorded_rows = cur.fetchall()
    conn.close()

    recorded_sentence_ids = {row["sentence_id"] for row in recorded_rows}

    remaining_sentences = [
        s for s in sentences
        if s["sentence_id"] not in recorded_sentence_ids
    ]

    next_sentence = remaining_sentences[0] if remaining_sentences else None

    return {
        "participant_code": participant_code,
        "total": len(sentences),
        "completed": len(recorded_sentence_ids),
        "remaining": len(remaining_sentences),
        "pending_jobs": media_queue.pending(participant_code),
        "completed_sentence_ids": list(recorded_sentence_ids),
        "next_sentence": next_sentence
    }

# =========================
# FFmpeg processing
# =========================

def run_command(command):
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=FFMPEG_TIMEOUT
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Video processing exceeded its time limit.") from e
    except subprocess.CalledProcessError as e:
        error_message = e.stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(error_message)


def get_media_duration_seconds(media_path):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(media_path)
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30
        )
        return round(float(result.stdout.decode("utf-8").strip()), 2)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return 0


def delete_recording_files(recording_row):
    deleted_files = []
    for key in (
        "original_mp4_path",
        "audio_wav_path",
        "video_only_mp4_path",
        "text_path"
    ):
        file_path = Path(recording_row[key])
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            deleted_files.append(str(file_path))
    return deleted_files


def backfill_missing_durations(cur, participant_code=None):
    if participant_code:
        cur.execute("""
            SELECT id, original_mp4_path
            FROM recordings
            WHERE participant_code = ?
              AND quality_status != 'retake_requested'
              AND (duration_seconds IS NULL OR duration_seconds <= 0)
        """, (participant_code,))
    else:
        cur.execute("""
            SELECT id, original_mp4_path
            FROM recordings
            WHERE quality_status != 'retake_requested'
              AND (duration_seconds IS NULL OR duration_seconds <= 0)
        """)

    rows = cur.fetchall()
    for row in rows:
        duration_seconds = get_media_duration_seconds(row["original_mp4_path"])
        if duration_seconds > 0:
            cur.execute("""
                UPDATE recordings
                SET duration_seconds = ?
                WHERE id = ?
            """, (duration_seconds, row["id"]))


def process_recording(input_path, original_mp4_path, audio_wav_path, video_only_mp4_path):
    """
    Convert browser-uploaded recording to:
    1. original mp4 with audio + video
    2. wav audio only
    3. mp4 video only without audio
    """

    # Convert uploaded file to MP4
    run_command([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-threads", str(FFMPEG_THREADS),
        "-filter_threads", "1",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-threads", str(FFMPEG_THREADS),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(original_mp4_path)
    ])

    # Extract WAV audio
    run_command([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-threads", str(FFMPEG_THREADS),
        "-filter_threads", "1",
        "-i", str(original_mp4_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(audio_wav_path)
    ])

    # Create video-only MP4, remove audio
    run_command([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-threads", str(FFMPEG_THREADS),
        "-filter_threads", "1",
        "-i", str(original_mp4_path),
        "-an",
        "-c:v", "copy",
        str(video_only_mp4_path)
    ])


# =========================
# API endpoints
# =========================

@app.get("/")
def home(request: Request):
    if request.session.get("participant_code"):
        return RedirectResponse(url="/record", status_code=303)

    login_path = STATIC_DIR / "login.html"
    if login_path.exists():
        return FileResponse(login_path)
    return RedirectResponse(url="/record", status_code=303)


@app.get("/record")
def record_page(request: Request):
    if not request.session.get("participant_code"):
        return RedirectResponse(url="/", status_code=303)

    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {
        "message": "Backend is running. Frontend will be added in the next step."
    }


@app.get("/icf")
def icf_page(request: Request):
    if not request.session.get("participant_code") and not request.session.get("is_admin"):
        return RedirectResponse(url="/", status_code=303)

    icf_path = STATIC_DIR / "icf_sign.html"
    if icf_path.exists():
        return FileResponse(icf_path)
    raise HTTPException(status_code=404, detail="ICF form not found.")


@app.post("/participant/login")
def participant_login(
    request: Request,
    participant_code: str = Form(...),
    password: str = Form(...)
):
    normalized_code = participant_code.strip().upper()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT participant_code, password
        FROM participant_credentials
        WHERE participant_code = ?
    """, (normalized_code,))
    credential = cur.fetchone()
    conn.close()

    if credential is None or not hmac.compare_digest(password, credential["password"]):
        return HTMLResponse(
            """
            <h2>Wrong participant ID or password</h2>
            <p><a href="/">Try again</a></p>
            """,
            status_code=401
        )

    request.session["participant_code"] = credential["participant_code"]
    return RedirectResponse(url="/record", status_code=303)


@app.get("/participant/logout")
def participant_logout(request: Request):
    request.session.pop("participant_code", None)
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/participant/me")
def api_participant_me(participant_code: str = Depends(require_participant)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM participants
        WHERE participant_code = ?
    """, (participant_code,))
    participant = cur.fetchone()
    cur.execute("""
        SELECT participant_name, consent_pdf_path
        FROM signed_consents
        WHERE participant_code = ?
    """, (participant_code,))
    signed_consent = cur.fetchone()
    if signed_consent is None and participant is not None:
        cur.execute("""
            SELECT participant_name, consent_pdf_path
            FROM participants
            WHERE participant_code = ?
              AND consent_pdf_path != ''
        """, (participant_code,))
        signed_consent = cur.fetchone()
    conn.close()

    return {
        "participant_code": participant_code,
        "profile_complete": bool(participant and participant["public_speaking_ability"] and participant["communication_style"]),
        "profile": {key: participant[key] for key in (
            "age_group", "gender", "indonesian_accent", "public_speaking_ability", "communication_style"
        )} if participant else None,
        "icf_complete": signed_consent is not None,
        "participant_name": signed_consent["participant_name"] if signed_consent else "",
        "signed_consent_available": signed_consent is not None
    }


@app.get("/api/consent/icf")
def api_open_current_icf(participant_code: str = Depends(require_participant)):
    return RedirectResponse(url="/icf", status_code=303)


@app.get("/api/consent/template")
def api_participant_consent_template(request: Request):
    if not request.session.get("participant_code") and not request.session.get("is_admin"):
        raise HTTPException(status_code=401, detail="Login required.")

    if not CONSENT_TEMPLATE_PATH.exists():
        raise HTTPException(status_code=404, detail="ICF template PDF not found.")

    return FileResponse(
        CONSENT_TEMPLATE_PATH,
        media_type="application/pdf"
    )


@app.post("/api/participant/icf/sign")
def api_sign_icf(
    age_group: str = Form(...),
    gender: str = Form(...),
    indonesian_accent: str = Form(...),
    signature_data: str = Form(...),
    consent_check_retention: bool = Form(...),
    consent_check_deidentification: bool = Form(...),
    consent_check_future_use: bool = Form(...),
    participant_code: str = Depends(require_participant)
):
    allowed_age_groups = {"18-25", "26-35", "36-45", "46-55", "56+"}
    allowed_genders = {"female", "male", "other", "prefer_not_to_say"}

    if age_group not in allowed_age_groups:
        raise HTTPException(status_code=400, detail="Invalid age group.")

    if gender not in allowed_genders:
        raise HTTPException(status_code=400, detail="Invalid gender.")

    if indonesian_accent not in ALLOWED_INDONESIAN_ACCENTS:
        raise HTTPException(status_code=400, detail="Invalid Indonesian accent or variety.")

    if not all([
        consent_check_retention,
        consent_check_deidentification,
        consent_check_future_use
    ]):
        raise HTTPException(status_code=400, detail="Participant must check all ICF consent boxes.")

    consent_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        consent_pdf_path = generate_signed_consent_pdf(
            participant_code,
            consent_timestamp,
            signature_data
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=500, detail=str(e))

    now = datetime.now()
    record_date = now.strftime("%Y-%m-%d")
    recording_time = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO signed_consents (
            participant_code,
            participant_name,
            consent_version,
            consent_timestamp,
            consent_pdf_path
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        participant_code,
        "",
        CONSENT_VERSION,
        consent_timestamp,
        str(consent_pdf_path)
    ))

    cur.execute("""
        SELECT participant_code
        FROM participants
        WHERE participant_code = ?
    """, (participant_code,))
    existing_participant = cur.fetchone()

    if existing_participant is None:
        cur.execute("""
            INSERT INTO participants (
                participant_code,
                participant_name,
                age_group,
                gender,
                native_language,
                indonesian_accent,
                consent_agreed,
                consent_version,
                consent_timestamp,
                consent_text_path,
                consent_pdf_path,
                record_date,
                recording_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            participant_code,
            "",
            age_group,
            gender,
            "N/A",
            indonesian_accent,
            1,
            CONSENT_VERSION,
            consent_timestamp,
            CONSENT_TEXT_PATH,
            str(consent_pdf_path),
            record_date,
            recording_time
        ))

    conn.commit()
    conn.close()

    return {
        "message": "ICF signed and participant session started.",
        "participant_code": participant_code,
        "consent_timestamp": consent_timestamp,
        "download_url": "/api/participant/consent-pdf",
        "redirect_url": "/record"
    }


@app.get("/api/participant/consent-pdf")
def api_participant_signed_consent_pdf(participant_code: str = Depends(require_participant)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT consent_pdf_path
        FROM signed_consents
        WHERE participant_code = ?
    """, (participant_code,))
    participant = cur.fetchone()
    if participant is None:
        cur.execute("""
            SELECT consent_pdf_path
            FROM participants
            WHERE participant_code = ?
              AND consent_pdf_path != ''
        """, (participant_code,))
        participant = cur.fetchone()
    conn.close()

    if participant is None or not participant["consent_pdf_path"]:
        raise HTTPException(status_code=404, detail="Signed consent PDF not found.")

    consent_pdf_path = Path(participant["consent_pdf_path"])
    if not consent_pdf_path.exists():
        raise HTTPException(status_code=404, detail="Signed consent PDF file does not exist.")

    return FileResponse(
        consent_pdf_path,
        media_type="application/pdf",
        filename=consent_pdf_path.name
    )


@app.get("/api/sentences")
def api_get_sentences():
    sentences = load_sentences()
    return {
        "total": len(sentences),
        "sentences": sentences
    }


@app.get("/api/participant/next-code")
def api_next_participant_code():
    return {
        "participant_code": generate_next_participant_code()
    }


@app.post("/api/participant/register")
def api_register_participant(
    age_group: str = Form(...),
    gender: str = Form(...),
    indonesian_accent: str = Form(...),
    public_speaking_ability: str = Form(...),
    communication_style: str = Form(...),
    participant_code: str = Depends(require_participant)
):
    allowed_age_groups = {"18-25", "26-35", "36-45", "46-55", "56+"}
    
    if age_group not in allowed_age_groups:
       raise HTTPException(
           status_code=400,
           detail="Invalid age group."
       )

    if gender not in {"female", "male"}:
        raise HTTPException(status_code=400, detail="Invalid gender.")

    if indonesian_accent not in ALLOWED_INDONESIAN_ACCENTS:
        raise HTTPException(
            status_code=400,
            detail="Invalid Indonesian accent or variety."
        )

    if public_speaking_ability not in {"A", "B", "C", "D"}:
        raise HTTPException(status_code=400, detail="Select a valid public speaking ability.")
    if communication_style not in {"A", "B", "C", "D"}:
        raise HTTPException(status_code=400, detail="Select a valid communication style.")

    now = datetime.now()
    record_date = now.strftime("%Y-%m-%d")
    recording_time = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT participant_name, consent_version, consent_timestamp, consent_pdf_path
        FROM signed_consents
        WHERE participant_code = ?
    """, (participant_code,))
    signed_consent = cur.fetchone()
    # Registration can proceed without consent; preserve existing signed records.
    participant_name = signed_consent["participant_name"] if signed_consent else ""
    consent_version = signed_consent["consent_version"] if signed_consent else ""
    consent_timestamp = signed_consent["consent_timestamp"] if signed_consent else ""
    consent_pdf_path = signed_consent["consent_pdf_path"] if signed_consent else ""

    cur.execute("""
        SELECT participant_code, public_speaking_ability, communication_style
        FROM participants
        WHERE participant_code = ?
    """, (participant_code,))
    existing_participant = cur.fetchone()

    if existing_participant is not None:
        if not existing_participant["public_speaking_ability"] or not existing_participant["communication_style"]:
            cur.execute("""
                UPDATE participants SET public_speaking_ability = ?, communication_style = ?
                WHERE participant_code = ?
            """, (public_speaking_ability, communication_style, participant_code))
            conn.commit()
        conn.close()
        return {
            "message": "Participant information already completed.",
            "participant_code": participant_code,
            "already_registered": True
        }

    cur.execute("""
        INSERT INTO participants (
            participant_code,
            participant_name,
            age_group,
            gender,
            native_language,
            indonesian_accent,
            public_speaking_ability,
            communication_style,
            consent_agreed,
            consent_version,
            consent_timestamp,
            consent_text_path,
            consent_pdf_path,
            record_date,
            recording_time
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        participant_code,
        participant_name,
        age_group,
        gender,
        "N/A",
        indonesian_accent,
        public_speaking_ability,
        communication_style,
        int(signed_consent is not None),
        consent_version,
        consent_timestamp,
        CONSENT_TEXT_PATH if signed_consent else "",
        str(consent_pdf_path),
        record_date,
        recording_time
    ))

    conn.commit()
    conn.close()

    participant_dir = RECORDINGS_DIR / participant_code
    participant_dir.mkdir(parents=True, exist_ok=True)

    return {
        "message": "Participant registered successfully.",
        "participant_code": participant_code,
        "consent_version": consent_version,
        "consent_timestamp": consent_timestamp,
        "consent_pdf_path": str(consent_pdf_path),
        "record_date": record_date,
        "recording_time": recording_time
    }


@app.post("/api/recordings/upload", status_code=202)
def api_upload_recording(
    participant_code: str = Form(...),
    sentence_id: str = Form(...),
    file: UploadFile = File(...),
    logged_participant_code: str = Depends(require_participant)
):
    # Synchronous endpoint: disk I/O runs in FastAPI's thread pool, not its event loop.
    if participant_code != logged_participant_code:
        raise HTTPException(status_code=403, detail="Cannot upload for another participant.")
    sentence = get_sentence_by_id(sentence_id)
    if sentence is None:
        raise HTTPException(status_code=404, detail="Sentence ID not found.")
    with closing(get_db()) as conn:
        participant = conn.execute("SELECT * FROM participants WHERE participant_code = ?", (participant_code,)).fetchone()
    if participant is None:
        raise HTTPException(status_code=404, detail="Participant not found.")
    if not participant["public_speaking_ability"] or not participant["communication_style"]:
        raise HTTPException(status_code=400, detail="Please complete the two new participant questions before recording.")
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Recording is too large. Please record a shorter clip.")

    base_name = (
        f"{participant_code}_{sentence_id}_{AGE_GROUP_CODES.get(participant['age_group'], '0')}"
        f"_{GENDER_CODES.get(participant['gender'].lower(), 'p')}"
        f"_{INDONESIAN_ACCENT_CODES.get(participant['indonesian_accent'], 'Q')}"
        f"_X{participant['public_speaking_ability']}_Y{participant['communication_style']}"
    )
    staging_root = RECORDINGS_DIR / ".uploads"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="upload-", dir=staging_root))
    accepted = False
    try:
        input_path = staging / "input.webm"
        total = 0
        with input_path.open("wb") as output:
            while chunk := file.file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Recording is too large. Please record a shorter clip.")
                output.write(chunk)
            if total == 0:
                raise HTTPException(status_code=400, detail="Recording is empty.")
            output.flush()
            os.fsync(output.fileno())
        payload = {"input_path": str(input_path), "base_name": base_name,
                   "sentence_text": sentence["sentence_text"]}
        try:
            job_id, accepted = media_queue.enqueue(participant_code, sentence_id, payload)
        except QueueFull:
            raise HTTPException(status_code=503, detail="The processing queue is full. Your clip is still in this browser; please retry Save & Continue shortly.", headers={"Retry-After": "30"})
        return {"job_id": job_id, "status_url": f"/api/recordings/jobs/{job_id}",
                "message": "Upload saved. Waiting for processing."}
    finally:
        if not accepted:
            shutil.rmtree(staging, ignore_errors=True)


@app.get("/api/recordings/jobs/{job_id}")
def api_media_job(job_id: str, participant_code: str = Depends(require_participant)):
    job = media_queue.get(job_id, participant_code)
    if job is None:
        raise HTTPException(status_code=404, detail="Recording job not found.")
    return job


def process_media_job(job):
    payload = job["payload"]
    input_path = Path(payload["input_path"])
    base_name = payload["base_name"]
    # Each attempt has isolated paths: retries never overwrite a valid recording.
    destination = RECORDINGS_DIR / job["participant_code"] / job["id"] / job["token"]
    destination.mkdir(parents=True, exist_ok=True)
    paths = {
        "original_mp4": destination / f"{base_name}_original.mp4",
        "audio_wav": destination / f"{base_name}_audio.wav",
        "video_only_mp4": destination / f"{base_name}_videoonly.mp4",
        "text": destination / f"{base_name}_text.txt"
    }
    committed = False
    try:
        process_recording(input_path, paths["original_mp4"], paths["audio_wav"], paths["video_only_mp4"])
        paths["text"].write_text(payload["sentence_text"] + "\n", encoding="utf-8")
        duration = get_media_duration_seconds(paths["original_mp4"])
        if duration <= 0:
            raise RuntimeError("Processed video has no valid duration.")
        result = {"participant_code": job["participant_code"], "sentence_id": job["sentence_id"],
                  "base_filename": base_name, "files": {key: str(path) for key, path in paths.items()},
                  "duration_seconds": duration}
        with closing(get_db()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute("SELECT token, status FROM media_jobs WHERE id=?", (job["id"],)).fetchone()
            if owner is None or owner["token"] != job["token"] or owner["status"] != "processing":
                raise RuntimeError("Job ownership changed.")
            conn.execute("""
                INSERT OR REPLACE INTO recordings (
                    participant_code, sentence_id, sentence_text, original_mp4_path,
                    audio_wav_path, video_only_mp4_path, text_path, recording_time, duration_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (job["participant_code"], job["sentence_id"], payload["sentence_text"],
                  str(paths["original_mp4"]), str(paths["audio_wav"]), str(paths["video_only_mp4"]),
                  str(paths["text"]), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), duration))
            # Recording publication and job completion are one database transaction.
            conn.execute("UPDATE media_jobs SET status='complete', result=? WHERE id=? AND token=?",
                         (json.dumps(result), job["id"], job["token"]))
        committed = True
        shutil.rmtree(input_path.parent, ignore_errors=True)
        return result
    finally:
        if not committed:
            shutil.rmtree(destination, ignore_errors=True)


@app.get("/api/progress/{participant_code}")
def api_progress(participant_code: str):
    sentences = load_sentences()
    total_sentences = len(sentences)

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) AS completed
        FROM recordings
        WHERE participant_code = ?
          AND quality_status != 'retake_requested'
    """, (participant_code,))

    row = cur.fetchone()
    conn.close()

    completed = row["completed"] if row else 0

    return {
        "participant_code": participant_code,
        "completed": completed,
        "total": total_sentences,
        "percentage": round((completed / total_sentences) * 100, 2)
    }

@app.get("/api/admin/summary")
def api_admin_summary(admin: bool = Depends(require_admin)):
    sentences = load_sentences()
    total_sentences = len(sentences)

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) AS total_participants
        FROM participants
    """)
    total_participants = cur.fetchone()["total_participants"]

    cur.execute("""
        SELECT COUNT(*) AS total_recordings
        FROM recordings
    """)
    total_recordings = cur.fetchone()["total_recordings"]

    cur.execute("""
        SELECT
            p.participant_code,
            p.participant_name,
            p.admin_validated,
            p.age_group,
            p.gender,
            p.indonesian_accent,
            p.recording_time AS registered_time,
            SUM(CASE
                WHEN r.id IS NOT NULL AND r.quality_status != 'retake_requested'
                THEN 1
                ELSE 0
            END) AS completed,
            SUM(CASE
                WHEN r.id IS NOT NULL AND r.quality_status != 'retake_requested'
                THEN COALESCE(r.duration_seconds, 0)
                ELSE 0
            END) AS total_duration_seconds,
            MAX(r.recording_time) AS last_recording_time
        FROM participants p
        LEFT JOIN recordings r
            ON p.participant_code = r.participant_code
        GROUP BY p.participant_code
        ORDER BY p.id ASC
    """)

    participants = []

    for row in cur.fetchall():
        completed = row["completed"]
        participants.append({
            "participant_code": row["participant_code"],
            "participant_name": row["participant_name"],
            "admin_validated": bool(row["admin_validated"]),
            "age_group": row["age_group"],
            "gender": row["gender"],
            "indonesian_accent": row["indonesian_accent"],
            "registered_time": row["registered_time"],
            "completed": completed,
            "total": total_sentences,
            "percentage": round((completed / total_sentences) * 100, 2),
            "total_duration_seconds": round(row["total_duration_seconds"] or 0, 2),
            "last_recording_time": row["last_recording_time"]
        })

    conn.close()

    return {
        "total_participants": total_participants,
        "total_recordings": total_recordings,
        "total_sentences": total_sentences,
        "completed_participants": sum(
            1 for participant in participants
            if total_sentences > 0 and participant["completed"] == total_sentences
        ),
        "participants": participants
    }


@app.post("/api/admin/participant/{participant_code}/validation")
def api_set_participant_validation(
    participant_code: str,
    validated: bool = Form(...),
    admin: bool = Depends(require_admin)
):
    with closing(get_db()) as conn, conn:
        result = conn.execute(
            "UPDATE participants SET admin_validated = ? WHERE participant_code = ?",
            (int(validated), participant_code)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Participant not found.")
    return {"participant_code": participant_code, "admin_validated": validated}


@app.get("/api/admin/credentials")
def api_admin_credentials(admin: bool = Depends(require_admin)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            c.participant_code,
            c.password,
            c.created_at,
            CASE WHEN p.participant_code IS NULL OR p.public_speaking_ability = '' OR p.communication_style = '' THEN 0 ELSE 1 END AS profile_complete,
            CASE WHEN s.participant_code IS NULL THEN 0 ELSE 1 END AS icf_signed
        FROM participant_credentials c
        LEFT JOIN participants p
            ON c.participant_code = p.participant_code
        LEFT JOIN signed_consents s
            ON c.participant_code = s.participant_code
        ORDER BY c.participant_code ASC
    """)
    credentials = [dict(row) for row in cur.fetchall()]
    conn.close()

    return {"credentials": credentials}


@app.get("/api/admin/consent/icf")
def api_admin_download_current_icf(admin: bool = Depends(require_admin)):
    if not CONSENT_TEMPLATE_PATH.exists():
        raise HTTPException(status_code=404, detail="ICF template PDF not found.")

    return FileResponse(
        CONSENT_TEMPLATE_PATH,
        media_type="application/pdf",
        filename=CONSENT_TEMPLATE_PATH.name
    )


@app.post("/api/admin/credentials/generate")
def api_admin_generate_credential(admin: bool = Depends(require_admin)):
    participant_code = generate_next_participant_code()
    password = generate_participant_password()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO participant_credentials (
            participant_code,
            password,
            created_at
        )
        VALUES (?, ?, ?)
    """, (participant_code, password, created_at))
    conn.commit()
    conn.close()

    return {
        "participant_code": participant_code,
        "password": password,
        "created_at": created_at
    }

@app.get("/api/admin/participant/{participant_code}")
def api_admin_participant_detail(participant_code: str, admin: bool = Depends(require_admin)):
    sentences = load_sentences()
    all_sentence_ids = [s["sentence_id"] for s in sentences]

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM participants
        WHERE participant_code = ?
    """, (participant_code,))
    participant = cur.fetchone()

    if participant is None:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail=f"Participant not found: {participant_code}"
        )

    cur.execute("""
        SELECT
            id AS recording_version,
            participant_code,
            sentence_id,
            sentence_text,
            original_mp4_path,
            audio_wav_path,
            video_only_mp4_path,
            text_path,
            recording_time,
            duration_seconds,
            quality_status,
            quality_note
        FROM recordings
        WHERE participant_code = ?
        ORDER BY sentence_id ASC
    """, (participant_code,))

    recordings = [dict(row) for row in cur.fetchall()]
    recorded_ids = {
        r["sentence_id"] for r in recordings
        if r["quality_status"] != "retake_requested"
    }
    total_duration_seconds = sum(
        r["duration_seconds"] or 0
        for r in recordings
        if r["quality_status"] != "retake_requested"
    )

    missing_sentence_ids = [
        sid for sid in all_sentence_ids
        if sid not in recorded_ids
    ]

    latest_backup = latest_participant_backup(conn, participant_code)
    conn.close()

    return {
        "participant": dict(participant),
        "latest_backup": latest_backup,
        "completed": len(recorded_ids),
        "total": len(sentences),
        "total_duration_seconds": round(total_duration_seconds, 2),
        "missing_sentence_ids": missing_sentence_ids,
        "recordings": recordings
    }


@app.get("/api/admin/participant/{participant_code}/consent-pdf")
def api_admin_participant_consent_pdf(participant_code: str, admin: bool = Depends(require_admin)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT consent_pdf_path
        FROM signed_consents
        WHERE participant_code = ?
    """, (participant_code,))
    participant = cur.fetchone()
    if participant is None:
        cur.execute("""
            SELECT consent_pdf_path
            FROM participants
            WHERE participant_code = ?
              AND consent_pdf_path != ''
        """, (participant_code,))
        participant = cur.fetchone()
    conn.close()

    if participant is None or not participant["consent_pdf_path"]:
        raise HTTPException(status_code=404, detail="Consent PDF not found.")

    consent_pdf_path = Path(participant["consent_pdf_path"])
    if not consent_pdf_path.exists():
        raise HTTPException(status_code=404, detail="Consent PDF file does not exist.")

    return FileResponse(
        consent_pdf_path,
        media_type="application/pdf",
        filename=consent_pdf_path.name
    )

def latest_participant_backup(conn, participant_code):
    row = conn.execute("""SELECT id, created_at, recording_count FROM participant_backups
        WHERE participant_code=? ORDER BY rowid DESC LIMIT 1""", (participant_code,)).fetchone()
    if row is None:
        return None
    return dict(row, download_url=f"/api/admin/participant/{participant_code}/backups/{row['id']}")


@app.post("/api/admin/participant/{participant_code}/full-retake")
def api_full_participant_retake(participant_code: str, admin: bool = Depends(require_admin)):
    def snapshot(conn):
        participant = conn.execute("SELECT * FROM participants WHERE participant_code=?", (participant_code,)).fetchone()
        if participant is None:
            raise HTTPException(status_code=404, detail="Participant not found.")
        pending = conn.execute("SELECT 1 FROM media_jobs WHERE participant_code=? AND status IN ('queued','processing') LIMIT 1", (participant_code,)).fetchone()
        if pending:
            raise HTTPException(status_code=409, detail="This participant has uploads waiting or processing. Wait until they finish, then retry.")
        rows = conn.execute("SELECT * FROM recordings WHERE participant_code=? ORDER BY sentence_id", (participant_code,)).fetchall()
        return dict(participant), [dict(row) for row in rows]

    with closing(get_db()) as conn:
        participant, recordings = snapshot(conn)
        if not recordings:
            raise HTTPException(status_code=400, detail="This participant has no recordings to back up.")
        existing = latest_participant_backup(conn, participant_code)
        if existing and all(row["quality_status"] == "retake_requested" for row in recordings):
            return {"message": "Full retake has already been requested.", "backup": existing, "recording_count": len(recordings)}

    root = (RECORDINGS_DIR / participant_code).resolve()
    if not root.is_relative_to(RECORDINGS_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid participant recording directory.")
    files = {}
    missing = []
    # Include older job/attempt folders as well as the currently selected recordings.
    for path in root.rglob("*"):
        if path.is_file():
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise HTTPException(status_code=409, detail="Unexpected file link in participant recordings. Backup stopped.")
            files[str(path.relative_to(root))] = path
    for row in recordings:
        for key in ("original_mp4_path", "audio_wav_path", "video_only_mp4_path", "text_path"):
            path = Path(row[key]).resolve()
            if not path.is_file():
                if row["quality_status"] != "retake_requested":
                    raise HTTPException(status_code=409, detail=f"A recording file for {row['sentence_id']} is missing. Nothing was reset.")
                missing.append({"sentence_id": row["sentence_id"], "path": row[key]})
                continue
            if not path.is_relative_to(root):
                raise HTTPException(status_code=409, detail="A recording points outside this participant's folder. Backup stopped.")
            files[str(path.relative_to(root))] = path

    backup_id = secrets.token_hex(16)
    created_at = datetime.now().isoformat(timespec="seconds")
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    temporary = BACKUPS_DIR / f".{backup_id}.tmp"
    archive = BACKUPS_DIR / f"{backup_id}.zip"
    try:
        file_stats = {name: (path.stat().st_size, path.stat().st_mtime_ns) for name, path in files.items()}
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as output:
            for name, path in files.items():
                output.write(path, f"recordings/{participant_code}/{name}")
            output.writestr("metadata.json", json.dumps({
                "created_at": created_at, "participant": participant, "recordings": recordings,
                "missing_previously_deleted_retake_files": missing,
                "files": list(file_stats)
            }, ensure_ascii=False, indent=2))
        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise RuntimeError("Backup verification failed.")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        with closing(get_db()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            # Do not hold a database write lock during the potentially long archive copy.
            if snapshot(conn) != (participant, recordings):
                raise HTTPException(status_code=409, detail="Participant recordings changed during backup. Nothing was reset; please retry.")
            if any((path.stat().st_size, path.stat().st_mtime_ns) != file_stats[name] for name, path in files.items()):
                raise HTTPException(status_code=409, detail="Recording files changed during backup. Nothing was reset; please retry.")
            os.replace(temporary, archive)
            conn.execute("INSERT INTO participant_backups VALUES (?, ?, ?, ?, ?)",
                         (backup_id, participant_code, str(archive), created_at, len(recordings)))
            conn.execute("""UPDATE recordings SET quality_status='retake_requested',
                quality_note='Full retake requested by admin; original files backed up.', duration_seconds=0
                WHERE participant_code=?""", (participant_code,))
            conn.execute("UPDATE participants SET admin_validated=0 WHERE participant_code=?", (participant_code,))
            backup = latest_participant_backup(conn, participant_code)
        return {"message": "Backup verified. All sentences are ready for retake. Ask the participant to reload or log in again.",
                "backup": backup, "recording_count": len(recordings)}
    except HTTPException:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise HTTPException(status_code=500, detail="Backup could not be completed. No recordings were reset. Check server storage and retry.") from error
    finally:
        temporary.unlink(missing_ok=True)


@app.get("/api/admin/participant/{participant_code}/backups/{backup_id}")
def api_download_participant_backup(participant_code: str, backup_id: str, admin: bool = Depends(require_admin)):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT archive_path FROM participant_backups WHERE id=? AND participant_code=?", (backup_id, participant_code)).fetchone()
    if row is None or not Path(row["archive_path"]).is_file():
        raise HTTPException(status_code=404, detail="Backup not found.")
    return FileResponse(row["archive_path"], media_type="application/zip", filename=f"{participant_code}_before_full_retake_{backup_id}.zip")


@app.post("/api/admin/recording/quality")
def api_update_recording_quality(
    participant_code: str = Form(...),
    sentence_id: str = Form(...),
    quality_status: str = Form(...),
    quality_note: str = Form(""),
    admin: bool = Depends(require_admin)
):
    allowed_status = {
        "unchecked",
        "good",
        "bad_audio",
        "bad_video",
        "too_short",
        "too_long",
        "wrong_sentence",
        "background_noise",
        "face_not_visible",
        "retake_requested",
        "other"
    }

    if quality_status not in allowed_status:
        raise HTTPException(
            status_code=400,
            detail="Invalid quality status."
        )

    conn = get_db()
    cur = conn.cursor()
    deleted_files = []

    if quality_status == "retake_requested":
        cur.execute("""
            SELECT original_mp4_path, audio_wav_path, video_only_mp4_path, text_path
            FROM recordings
            WHERE participant_code = ? AND sentence_id = ?
        """, (participant_code, sentence_id))
        recording = cur.fetchone()
        if recording is None:
            conn.close()
            raise HTTPException(
                status_code=404,
                detail="Recording not found."
            )
        deleted_files = delete_recording_files(recording)

    cur.execute("""
        UPDATE recordings
        SET
            quality_status = ?,
            quality_note = ?,
            duration_seconds = CASE
                WHEN ? = 'retake_requested' THEN 0
                ELSE duration_seconds
            END
        WHERE participant_code = ? AND sentence_id = ?
    """, (
        quality_status,
        quality_note,
        quality_status,
        participant_code,
        sentence_id
    ))

    conn.commit()
    conn.close()

    return {
        "message": "Quality status updated.",
        "participant_code": participant_code,
        "sentence_id": sentence_id,
        "quality_status": quality_status,
        "quality_note": quality_note,
        "deleted_files": deleted_files
    }


@app.post("/api/admin/recordings/bulk-retake")
def api_bulk_recording_retake(
    payload: dict = Body(...),
    admin: bool = Depends(require_admin),
):
    participant_code = str(payload.get("participant_code", "")).strip().upper()
    raw_sentence_ids = payload.get("sentence_ids")
    if not participant_code or not isinstance(raw_sentence_ids, list):
        raise HTTPException(status_code=400, detail="Participant and sentence IDs are required.")
    sentence_ids = list(dict.fromkeys(
        str(sentence_id).strip() for sentence_id in raw_sentence_ids
        if str(sentence_id).strip()
    ))
    if not sentence_ids:
        raise HTTPException(status_code=400, detail="Select at least one recording.")
    if len(sentence_ids) > 500:
        raise HTTPException(status_code=400, detail="Too many recordings selected.")

    placeholders = ",".join("?" for _ in sentence_ids)
    conn = get_db()
    staged = []
    staging_dir = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"""SELECT sentence_id, quality_status, original_mp4_path,
                       audio_wav_path, video_only_mp4_path, text_path
                FROM recordings
                WHERE participant_code=? AND sentence_id IN ({placeholders})""",
            (participant_code, *sentence_ids),
        ).fetchall()
        found = {row["sentence_id"] for row in rows}
        missing = [sentence_id for sentence_id in sentence_ids if sentence_id not in found]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"Recording(s) not found: {', '.join(missing)}",
            )
        already_requested = [
            row["sentence_id"] for row in rows
            if row["quality_status"] == "retake_requested"
        ]
        if already_requested:
            raise HTTPException(
                status_code=409,
                detail=f"Retake already requested: {', '.join(already_requested)}",
            )

        participant_root = (RECORDINGS_DIR / participant_code).resolve()
        staging_dir = Path(tempfile.mkdtemp(prefix="bulk-retake-", dir=RECORDINGS_DIR))
        file_keys = ("original_mp4_path", "audio_wav_path", "video_only_mp4_path", "text_path")
        for row_index, row in enumerate(rows):
            for key in file_keys:
                source = Path(row[key]).resolve()
                if not source.exists():
                    continue
                if not source.is_relative_to(participant_root) or not source.is_file():
                    raise HTTPException(
                        status_code=409,
                        detail=f"Invalid recording file for {row['sentence_id']}.",
                    )
                destination = staging_dir / f"{row_index}-{key}-{source.name}"
                os.replace(source, destination)
                staged.append((source, destination))

        conn.execute(
            f"""UPDATE recordings
                SET quality_status='retake_requested',
                    quality_note='Retake requested by admin (bulk selection).',
                    duration_seconds=0
                WHERE participant_code=? AND sentence_id IN ({placeholders})""",
            (participant_code, *sentence_ids),
        )
        conn.execute(
            "UPDATE participants SET admin_validated=0 WHERE participant_code=?",
            (participant_code,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        for source, destination in reversed(staged):
            if destination.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
        raise
    finally:
        conn.close()
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return {
        "message": f"Retake requested for {len(sentence_ids)} recordings.",
        "participant_code": participant_code,
        "sentence_ids": sentence_ids,
        "retake_count": len(sentence_ids),
        "deleted_files": [str(source) for source, _destination in staged],
    }

@app.get("/api/admin/recording/video/{participant_code}/{sentence_id}")
def api_get_recording_video(participant_code: str, sentence_id: str, admin: bool = Depends(require_admin)):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT original_mp4_path
        FROM recordings
        WHERE participant_code = ? AND sentence_id = ?
    """, (participant_code, sentence_id))

    row = cur.fetchone()
    conn.close()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Recording not found."
        )

    video_path = Path(row["original_mp4_path"])

    if not video_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Video file does not exist."
        )

    return FileResponse(
        video_path,
        media_type="video/mp4",
        filename=video_path.name,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

def admin_html_page(filename: str):
    """Render an admin page with its configured base path."""
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin page not found.")
    content = path.read_text(encoding="utf-8").replace(
        "{{ADMIN_BASE_PATH}}", ADMIN_BASE_PATH
    )
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


@app.get(f"{ADMIN_BASE_PATH}/login", response_class=HTMLResponse)
def admin_login_page():
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Login</title>
        <link rel="stylesheet" href="/static/style.css">
    </head>
    <body>
        <div class="container">
            <section class="card">
                <h1>Admin Login</h1>
                <form method="post" action="{ADMIN_BASE_PATH}/login">
                    <label>Password</label>
                    <input type="password" name="password" required>
                    <button type="submit">Login</button>
                </form>
            </section>
        </div>
    </body>
    </html>
    """
@app.post(f"{ADMIN_BASE_PATH}/login")
def admin_login(request: Request, password: str = Form(...)):
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        return HTMLResponse(
            f"""
            <h2>Wrong password</h2>
            <p><a href="{ADMIN_BASE_PATH}/login">Try again</a></p>
            """,
            status_code=401
        )

    request.session["is_admin"] = True
    return RedirectResponse(url=ADMIN_BASE_PATH, status_code=303)

@app.get(f"{ADMIN_BASE_PATH}/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=f"{ADMIN_BASE_PATH}/login", status_code=303)

@app.get(ADMIN_BASE_PATH)
def admin_page(request: Request):
    if not request.session.get("is_admin"):
        return RedirectResponse(url=f"{ADMIN_BASE_PATH}/login", status_code=303)

    return admin_html_page("admin.html")


@app.get(f"{ADMIN_BASE_PATH}/preprocessing.html")
def preprocessing_page(request: Request):
    if not request.session.get("is_admin"):
        return RedirectResponse(url=f"{ADMIN_BASE_PATH}/login", status_code=303)
    return admin_html_page("preprocessing.html")


@app.get(f"{ADMIN_BASE_PATH}/readiness.html")
def dataset_readiness_page(request: Request):
    if not request.session.get("is_admin"):
        return RedirectResponse(url=f"{ADMIN_BASE_PATH}/login", status_code=303)
    return admin_html_page("readiness.html")


def public_readiness_job(row):
    if row is None or row["id"] is None:
        return None
    item = dict(row)
    total = item.get("total_files") or 0
    completed = item.get("completed_files") or 0
    item["percent"] = round(completed * 100 / total, 1) if total else 0
    for key in ("created_at", "finished_at"):
        if item.get(key):
            item[key] = datetime.fromtimestamp(item[key]).strftime("%Y-%m-%d %H:%M:%S")
    item.pop("result_dir", None)
    return item


@app.get("/api/admin/readiness/participants")
def api_readiness_participants(admin: bool = Depends(require_admin)):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """SELECT p.participant_code,
                      COUNT(DISTINCT CASE WHEN r.quality_status!='retake_requested'
                                         THEN r.sentence_id END) AS recordings,
                      j.id, j.status, j.export_mode, j.total_files,
                      j.completed_files, j.failed_files, j.current_sentence,
                      j.stage, j.error, j.result_dir, j.created_at, j.finished_at
               FROM participants p
               LEFT JOIN recordings r ON r.participant_code=p.participant_code
               LEFT JOIN dataset_readiness_jobs j ON j.id=(
                   SELECT id FROM dataset_readiness_jobs
                   WHERE participant_code=p.participant_code
                   ORDER BY created_at DESC LIMIT 1
               )
               GROUP BY p.id, p.participant_code
               ORDER BY p.id"""
        ).fetchall()
    return {
        "participants": [
            {
                "participant_code": row["participant_code"],
                "recordings": row["recordings"],
                "latest_job": public_readiness_job(row),
            }
            for row in rows
        ]
    }


@app.get("/api/admin/readiness/jobs/{job_id}")
def api_readiness_job(job_id: str, admin: bool = Depends(require_admin)):
    with closing(get_db()) as conn:
        job = conn.execute(
            """SELECT id,participant_code,status,export_mode,total_files,
                      completed_files,failed_files,current_sentence,stage,error,
                      result_dir,created_at,finished_at
               FROM dataset_readiness_jobs WHERE id=?""",
            (job_id,),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Dataset readiness job not found.")
        rows = conn.execute(
            """SELECT sentence_id,canonical_basename,status,metadata_json,error
               FROM dataset_readiness_results
               WHERE job_id=? ORDER BY sentence_id""",
            (job_id,),
        ).fetchall()
    results = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        results.append({
            "sentence_id": row["sentence_id"],
            "canonical_basename": row["canonical_basename"],
            "status": row["status"],
            "reference_text": metadata.get("reference_text", ""),
            "error": row["error"],
        })
    payload = public_readiness_job(job)
    payload["results"] = results
    return payload


@app.get("/api/admin/readiness/jobs/{job_id}/files/{sentence_id}/{kind}")
def api_readiness_file(
    job_id: str,
    sentence_id: str,
    kind: str,
    admin: bool = Depends(require_admin),
):
    columns = {
        "video": ("video_path", "video/mp4"),
        "audio": ("audio_path", "audio/wav"),
        "text": ("text_path", "text/plain; charset=utf-8"),
        "face": ("face_path", "video/mp4"),
        "mouth": ("mouth_path", "video/mp4"),
    }
    if kind not in columns:
        raise HTTPException(status_code=404, detail="Unknown dataset modality.")
    column, media_type = columns[kind]
    with closing(get_db()) as conn:
        row = conn.execute(
            f"SELECT {column} AS path FROM dataset_readiness_results WHERE job_id=? AND sentence_id=?",
            (job_id, sentence_id),
        ).fetchone()
    if row is None or not row["path"]:
        raise HTTPException(status_code=404, detail="Dataset readiness file not found.")
    path = Path(row["path"]).resolve()
    root = DATASET_READY_DIR.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Dataset readiness file not found.")
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/admin/preprocessing/participants")
def api_preprocessing_participants(admin: bool = Depends(require_admin)):
    return {
        "participants": list_preprocessing_participants(DB_PATH),
        "output_root": str(CLEAN_RECORDINGS_DIR),
    }


@app.post("/api/admin/preprocessing/participant/{participant_code}/start", status_code=202)
def api_start_participant_preprocessing(
    participant_code: str,
    admin: bool = Depends(require_admin),
):
    participant_code = participant_code.strip().upper()
    require_asr_service_if_configured()
    if not PREPROCESSING_PYTHON.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"Preprocessing Python not found: {PREPROCESSING_PYTHON}",
        )
    with closing(get_db()) as conn:
        participant = conn.execute(
            "SELECT 1 FROM participants WHERE participant_code=?", (participant_code,),
        ).fetchone()
        if participant is None:
            raise HTTPException(status_code=404, detail="Participant not found.")
        if conn.execute(
            """SELECT 1 FROM preprocessing_jobs WHERE participant_code=?
               AND status IN ('queued','running') LIMIT 1""",
            (participant_code,),
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="Preprocessing is already running for this participant.",
            )
        count = conn.execute(
            """SELECT COUNT(*) FROM recordings WHERE participant_code=?
               AND quality_status!='retake_requested'""",
            (participant_code,),
        ).fetchone()[0]
        if count == 0:
            raise HTTPException(
                status_code=400,
                detail="This participant has no current recordings to preprocess.",
            )
        if conn.execute(
            """SELECT 1 FROM media_jobs WHERE participant_code=?
               AND status IN ('queued','processing') LIMIT 1""",
            (participant_code,),
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="This participant still has an upload in progress.",
            )

    log_dir = CLEAN_RECORDINGS_DIR / participant_code
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"preprocessing-{secrets.token_hex(8)}.log"
    command = []
    if shutil.which("ionice"):
        command.extend(["ionice", "-c", "2", "-n", "7"])
    if shutil.which("nice"):
        command.extend(["nice", "-n", "10"])
    command.extend([
        str(PREPROCESSING_PYTHON), "-m", "processing_scripts.preprocessing",
        "--participant_id", participant_code,
        "--workers", str(max(1, min(16, int(os.getenv("PREPROCESSING_WORKERS", "2"))))),
    ])
    try:
        with log_path.open("ab") as log_handle:
            process = subprocess.Popen(
                command, cwd=BASE_DIR, stdout=log_handle, stderr=subprocess.STDOUT,
                start_new_session=True, env=ml_subprocess_environment(),
            )
    except Exception as error:
        raise HTTPException(
            status_code=500, detail="Preprocessing process could not be started."
        ) from error
    return {
        "participant_code": participant_code,
        "status": "starting",
        "recordings": count,
        "pid": process.pid,
    }


@app.get("/api/admin/preprocessing/jobs/{job_id}")
def api_preprocessing_job(job_id: str, admin: bool = Depends(require_admin)):
    job = get_preprocessing_job(DB_PATH, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Preprocessing job not found.")
    return job


@app.get("/api/admin/preprocessing/jobs/{job_id}/files/{sentence_id}/{kind}")
def api_preprocessing_file(
    job_id: str,
    sentence_id: str,
    kind: str,
    admin: bool = Depends(require_admin),
):
    columns = {
        "original": ("source_video", "video/mp4"),
        "video": ("clean_video", "video/mp4"),
        "audio": ("clean_audio", "audio/wav"),
        "silent-video": ("silent_video", "video/mp4"),
    }
    if kind not in columns:
        raise HTTPException(status_code=404, detail="Unknown preprocessing file type.")
    column, media_type = columns[kind]
    with closing(get_db()) as conn:
        row = conn.execute(
            f"SELECT {column} AS path FROM preprocessing_results WHERE job_id=? AND sentence_id=?",
            (job_id, sentence_id),
        ).fetchone()
    if row is None or not row["path"]:
        raise HTTPException(status_code=404, detail="Preprocessed file not found.")
    path = Path(row["path"]).resolve()
    allowed_roots = {RECORDINGS_DIR.resolve()} if kind == "original" else {
        CLEAN_RECORDINGS_DIR.resolve(),
        (BASE_DIR / "recording_clean").resolve(),
        LEGACY_CLEAN_RECORDINGS_DIR.resolve(),
    }
    if not any(path.is_relative_to(root) for root in allowed_roots) or not path.is_file():
        raise HTTPException(status_code=404, detail="Preprocessed file not found.")
    return FileResponse(path, media_type=media_type, filename=path.name)


def public_evaluation_run(row):
    if row is None:
        return None
    item = dict(row)
    total = item.get("total_recordings") or 0
    completed = item.get("completed_recordings") or 0
    item["percent"] = round(completed * 100 / total, 1) if total else 0
    for key in ("created_at", "finished_at"):
        if item.get(key):
            item[key] = datetime.fromtimestamp(item[key]).strftime("%Y-%m-%d %H:%M:%S")
    item.pop("log_path", None)
    item.pop("pid", None)
    return item


@app.get(f"{ADMIN_BASE_PATH}/report.html")
def evaluation_report_page(request: Request):
    if not request.session.get("is_admin"):
        return RedirectResponse(url=f"{ADMIN_BASE_PATH}/login", status_code=303)
    return admin_html_page("report.html")


@app.get("/api/admin/evaluations/participants")
def api_evaluation_participants(admin: bool = Depends(require_admin)):
    with closing(get_db()) as conn:
        rows = conn.execute(
            """SELECT p.participant_code,
                      COUNT(CASE WHEN r.quality_status!='retake_requested' THEN 1 END) AS recordings,
                      e.id, e.status, e.total_recordings, e.completed_recordings,
                      e.summary_path, e.error, e.created_at, e.finished_at,
                      e.overall_wer, e.overall_cer
               FROM participants p
               LEFT JOIN recordings r ON r.participant_code=p.participant_code
               LEFT JOIN evaluation_runs e ON e.id=(
                   SELECT id FROM evaluation_runs
                   WHERE participant_code=p.participant_code
                   ORDER BY created_at DESC LIMIT 1
               )
               GROUP BY p.participant_code ORDER BY p.id"""
        ).fetchall()
    return {
        "participants": [
            {
                "participant_code": row["participant_code"],
                "recordings": row["recordings"],
                "latest_run": public_evaluation_run(row) if row["id"] else None,
            }
            for row in rows
        ]
    }


@app.post("/api/admin/evaluations/participant/{participant_code}", status_code=202)
def api_start_participant_evaluation(participant_code: str, admin: bool = Depends(require_admin)):
    participant_code = participant_code.strip().upper()
    require_asr_service_if_configured()
    if not EVALUATION_PYTHON.is_file():
        raise HTTPException(status_code=503, detail=f"Evaluation Python not found: {EVALUATION_PYTHON}")
    now = datetime.now().timestamp()
    run_id = secrets.token_hex(16)
    EVALUATION_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    log_dir = EVALUATION_REPORTS_DIR / participant_code
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"
    with closing(get_db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM evaluation_runs WHERE participant_code=? AND status='running'",
            (participant_code,),
        ).fetchone():
            raise HTTPException(status_code=409, detail="Evaluation is already running for this participant.")
        participant = conn.execute(
            "SELECT 1 FROM participants WHERE participant_code=?", (participant_code,),
        ).fetchone()
        if participant is None:
            raise HTTPException(status_code=404, detail="Participant not found.")
        count = conn.execute(
            """SELECT COUNT(*) FROM recordings WHERE participant_code=?
               AND quality_status!='retake_requested'""",
            (participant_code,),
        ).fetchone()[0]
        if count == 0:
            raise HTTPException(status_code=400, detail="This participant has no current recordings to evaluate.")
        if conn.execute(
            """SELECT 1 FROM media_jobs WHERE participant_code=?
               AND status IN ('queued','processing') LIMIT 1""",
            (participant_code,),
        ).fetchone():
            raise HTTPException(status_code=409, detail="This participant still has an upload in progress.")
        conn.execute(
            """INSERT INTO evaluation_runs
               (id, participant_code, status, total_recordings, log_path, created_at)
               VALUES (?, ?, 'running', ?, ?, ?)""",
            (run_id, participant_code, count, str(log_path), now),
        )

    command = []
    if shutil.which("ionice"):
        command.extend(["ionice", "-c", "2", "-n", "7"])
    if shutil.which("nice"):
        command.extend(["nice", "-n", "10"])
    command.extend([
        str(EVALUATION_PYTHON), "-m", "processing_scripts.dataset_evaluation",
        "--userid", participant_code,
        "--base_path", str(RECORDINGS_DIR),
        "--db_path", str(DB_PATH),
        "--output_root", str(EVALUATION_REPORTS_DIR),
        "--run_id", run_id,
        "--device", os.getenv("EVALUATION_DEVICE", "cpu"),
        "--compute_type", os.getenv("EVALUATION_COMPUTE_TYPE", "int8"),
    ])
    try:
        with log_path.open("ab") as log_handle:
            process = subprocess.Popen(
                command, cwd=BASE_DIR, stdout=log_handle, stderr=subprocess.STDOUT,
                start_new_session=True, env=ml_subprocess_environment(),
            )
        with closing(get_db()) as conn, conn:
            conn.execute("UPDATE evaluation_runs SET pid=? WHERE id=?", (process.pid, run_id))
    except Exception as error:
        with closing(get_db()) as conn, conn:
            conn.execute(
                "UPDATE evaluation_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                (str(error)[:2000], datetime.now().timestamp(), run_id),
            )
        raise HTTPException(status_code=500, detail="Evaluation process could not be started.") from error
    return {"run": public_evaluation_run({
        "id": run_id, "participant_code": participant_code, "status": "running",
        "total_recordings": count, "completed_recordings": 0,
        "summary_path": "", "error": "", "created_at": now, "finished_at": None,
    })}


@app.get("/api/admin/evaluations/runs/{run_id}")
def api_evaluation_run(run_id: str, admin: bool = Depends(require_admin)):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM evaluation_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found.")
    run = public_evaluation_run(row)
    run["report"] = None
    if row["status"] == "completed" and row["summary_path"]:
        summary_path = Path(row["summary_path"]).resolve()
        allowed_roots = {
            EVALUATION_REPORTS_DIR.resolve(),
            (BASE_DIR / "evaluation_reports").resolve(),
            LEGACY_EVALUATION_REPORTS_DIR.resolve(),
        }
        if not any(summary_path.is_relative_to(root) for root in allowed_roots) or not summary_path.is_file():
            raise HTTPException(status_code=404, detail="Evaluation report file is missing.")
        try:
            run["report"] = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=500, detail="Evaluation report could not be read.") from error
    return run




# Serve static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# =========================
# Run directly with python3 app.py
# =========================

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=False
    )
