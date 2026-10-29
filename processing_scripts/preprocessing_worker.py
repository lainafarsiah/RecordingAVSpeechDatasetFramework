"""One-at-a-time offline preprocessing worker.

Run this separately from the web application. It never modifies source recordings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import time
import unicodedata
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path

from .preprocessing_system import claim_job, connect, initialize, update_job


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("APP_DB_PATH", BASE_DIR / "data" / "app.db"))
CLEAN_ROOT = Path(os.getenv("CLEAN_RECORDINGS_DIR", BASE_DIR / "recording_clean"))
RNNOISE_MODEL = Path(os.getenv("RNNOISE_MODEL_PATH", BASE_DIR / "models" / "rnnoise.rnnn"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv(
    "WHISPER_COMPUTE_TYPE", "int8" if WHISPER_DEVICE == "cpu" else "float16"
)
MAX_WER_THRESHOLD = 0.25
CER_REVIEW_THRESHOLD = 0.10
CER_FLAG_THRESHOLD = 0.20
FFMPEG_TIMEOUT = max(30, int(os.getenv("PREPROCESS_FFMPEG_TIMEOUT", "300")))
WORKER_ID = f"preprocess-{uuid.uuid4().hex[:12]}"
STOP = False


def stop_worker(*_args) -> None:
    global STOP
    STOP = True


def run(command: list[str], timeout: int = FFMPEG_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("FFmpeg exceeded its processing time limit.") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "Media processing failed.").strip()
        raise RuntimeError(detail[-2000:]) from error


def ffprobe(path: Path) -> dict:
    result = run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=start_time,duration:stream=index,codec_type,start_time,duration",
            "-of", "json", str(path),
        ],
        timeout=60,
    )
    return json.loads(result.stdout)


def media_timing(path: Path) -> dict:
    payload = ffprobe(path)
    result = {
        "format_start": float(payload.get("format", {}).get("start_time") or 0),
        "format_duration": float(payload.get("format", {}).get("duration") or 0),
    }
    for stream in payload.get("streams", []):
        kind = stream.get("codec_type")
        if kind in ("audio", "video"):
            result[f"{kind}_start"] = float(stream.get("start_time") or 0)
            result[f"{kind}_duration"] = float(stream.get("duration") or 0)
    return result


def escape_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def clean_video(source: Path, destination: Path) -> None:
    model = escape_filter_path(RNNOISE_MODEL)
    run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
            "-af", f"aresample=48000,arnndn=model='{model}':mix=1",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-map_metadata", "0", str(destination),
        ]
    )


def extract_wav(source: Path, destination: Path) -> None:
    run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(destination),
        ]
    )


def extract_silent_video(source: Path, destination: Path) -> None:
    run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-an", "-c:v", "copy", "-map_metadata", "0",
            "-movflags", "+faststart", str(destination),
        ]
    )


def wav_quality(path: Path) -> dict:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.getnframes()
        if channels != 1 or width != 2:
            raise RuntimeError("Quality analysis expects 16-bit mono WAV audio.")
        raw = audio.readframes(frames)
    samples = memoryview(raw).cast("h")
    if not samples:
        raise RuntimeError("The audio stream is empty.")
    peak = max(abs(value) for value in samples)
    square_sum = sum(value * value for value in samples)
    rms = math.sqrt(square_sum / len(samples))
    clipping = sum(abs(value) >= 32700 for value in samples) / len(samples)
    window = max(1, int(rate * 0.02))
    quiet_windows = 0
    windows = 0
    # -45 dBFS is used only as a transparent screening threshold.
    silence_threshold = 32768 * (10 ** (-45 / 20))
    for offset in range(0, len(samples), window):
        part = samples[offset : offset + window]
        if not part:
            continue
        part_rms = math.sqrt(sum(value * value for value in part) / len(part))
        quiet_windows += part_rms < silence_threshold
        windows += 1
    peak_dbfs = 20 * math.log10(max(peak, 1) / 32768)
    rms_dbfs = 20 * math.log10(max(rms, 1) / 32768)
    silence_ratio = quiet_windows / windows if windows else 1
    flags = []
    if clipping > 0.001:
        flags.append("clipping")
    if rms_dbfs < -35:
        flags.append("very_quiet")
    if silence_ratio > 0.8:
        flags.append("mostly_silence")
    return {
        "sample_rate": rate,
        "duration_seconds": round(len(samples) / rate, 3),
        "peak_dbfs": round(peak_dbfs, 2),
        "rms_dbfs": round(rms_dbfs, 2),
        "clipping_ratio": round(clipping, 6),
        "silence_ratio": round(silence_ratio, 4),
        "flags": flags,
        "method": "deterministic_pcm_screening_v1",
    }


def normalize_text(value: str) -> list[str]:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return value.split()


def edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, 1):
        current = [left_index]
        for right_index, right_value in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_value != right_value),
                )
            )
        previous = current
    return previous[-1]


def error_rates(expected: str, transcript: str) -> tuple[float, float]:
    expected_words = normalize_text(expected)
    transcript_words = normalize_text(transcript)
    wer = edit_distance(expected_words, transcript_words) / max(1, len(expected_words))
    expected_chars = list("".join(expected_words))
    transcript_chars = list("".join(transcript_words))
    cer = edit_distance(expected_chars, transcript_chars) / max(1, len(expected_chars))
    return round(wer, 4), round(cer, 4)


def transcript_decision(wer: float, cer: float) -> str:
    if cer > CER_FLAG_THRESHOLD or (
        wer > MAX_WER_THRESHOLD and cer > CER_REVIEW_THRESHOLD
    ):
        return "flag"
    if cer > CER_REVIEW_THRESHOLD:
        return "review"
    return "pass"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_asr_model(workers: int = 1):
    if os.getenv("ASR_API_URL"):
        from .asr_client import RemoteWhisperModel
        return RemoteWhisperModel()
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError(
            "faster-whisper is not installed. Install requirements.txt."
        ) from error
    return WhisperModel(
        WHISPER_MODEL,
        device=WHISPER_DEVICE,
        compute_type=WHISPER_COMPUTE_TYPE,
        num_workers=max(1, workers),
    )


def transcribe(model, audio_path: Path) -> tuple[str, list[dict]]:
    segments, _info = model.transcribe(
        str(audio_path),
        language="id",
        task="transcribe",
        beam_size=5,
        word_timestamps=True,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    items = []
    for segment in segments:
        items.append(
            {"start": segment.start, "end": segment.end, "text": segment.text.strip()}
        )
    return " ".join(item["text"] for item in items).strip(), items


def assert_source_is_current(recording: dict) -> Path:
    source = Path(recording["original_mp4_path"]).resolve()
    stat = source.stat()
    if stat.st_size != recording["source_size"] or stat.st_mtime_ns != recording["source_mtime_ns"]:
        raise RuntimeError("The source recording changed after this job was queued.")
    with closing(connect(DB_PATH)) as connection:
        current = connection.execute(
            """SELECT id, original_mp4_path, quality_status FROM recordings
               WHERE participant_code=? AND sentence_id=?""",
            (recording["participant_code"], recording["sentence_id"]),
        ).fetchone()
    if (
        current is None
        or current["id"] != recording["id"]
        or Path(current["original_mp4_path"]).resolve() != source
        or current["quality_status"] == "retake_requested"
    ):
        raise RuntimeError("This sentence was replaced or requested for retake.")
    return source


def job_cancelled(job_id: str) -> bool:
    with closing(connect(DB_PATH)) as connection:
        row = connection.execute(
            "SELECT cancel_requested FROM preprocessing_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return bool(row and row["cancel_requested"])


def save_result(job: dict, recording: dict, values: dict) -> None:
    with closing(connect(DB_PATH)) as connection, connection:
        connection.execute(
            """INSERT OR REPLACE INTO preprocessing_results
               (job_id, participant_code, sentence_id, status, source_video,
                clean_video, clean_audio, silent_video, expected_text, transcript,
                wer, cer, quality_json, timing_json, error, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job["id"], job["participant_code"], recording["sentence_id"],
                values["status"], recording["original_mp4_path"],
                values.get("clean_video", ""), values.get("clean_audio", ""),
                values.get("silent_video", ""), recording["sentence_text"],
                values.get("transcript", ""), values.get("wer"), values.get("cer"),
                json.dumps(values.get("quality", {}), ensure_ascii=False),
                json.dumps(values.get("timing", {}), ensure_ascii=False),
                values.get("error", ""), time.time(),
            ),
        )


def process_recording(job: dict, recording: dict, model) -> dict:
    recording = dict(recording, participant_code=job["participant_code"])
    source = assert_source_is_current(recording)
    output_dir = Path(job["result_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    base = source.stem.removesuffix("_original")
    staging = output_dir / f".{base}.{job['id']}.tmp"
    # A process killed during FFmpeg may leave only this job-specific staging
    # directory. It contains no source media and is safe to rebuild on recovery.
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=False, exist_ok=False)
    clean_mp4 = staging / f"{base}_clean.mp4"
    clean_wav = staging / f"{base}_clean_audio.wav"
    silent_mp4 = staging / f"{base}_clean_videoonly.mp4"
    original_wav = staging / f"{base}_source_analysis.wav"
    try:
        update_job(DB_PATH, job["id"], WORKER_ID, stage="Denoising audio in video", heartbeat_at=time.time())
        clean_video(source, clean_mp4)
        update_job(DB_PATH, job["id"], WORKER_ID, stage="Creating clean audio and silent video", heartbeat_at=time.time())
        extract_wav(clean_mp4, clean_wav)
        extract_silent_video(clean_mp4, silent_mp4)
        extract_wav(source, original_wav)
        original_quality = wav_quality(original_wav)
        clean_quality = wav_quality(clean_wav)
        source_timing = media_timing(source)
        clean_timing = media_timing(clean_mp4)
        audio_video_offset_change = (
            (clean_timing.get("audio_start", 0) - clean_timing.get("video_start", 0))
            - (source_timing.get("audio_start", 0) - source_timing.get("video_start", 0))
        )
        duration_change = clean_timing["format_duration"] - source_timing["format_duration"]
        timing = {
            "source": source_timing,
            "clean": clean_timing,
            "audio_video_offset_change_seconds": round(audio_video_offset_change, 4),
            "duration_change_seconds": round(duration_change, 4),
        }
        timing_flags = []
        if abs(audio_video_offset_change) > 0.05:
            timing_flags.append("audio_video_offset_changed")
        if abs(duration_change) > 0.10:
            timing_flags.append("duration_changed")
        timing["flags"] = timing_flags

        update_job(DB_PATH, job["id"], WORKER_ID, stage="Transcribing clean audio", heartbeat_at=time.time())
        transcript, segments = transcribe(model, clean_wav)
        wer, cer = error_rates(recording["sentence_text"], transcript)
        decision = transcript_decision(wer, cer)
        flags = list(dict.fromkeys(clean_quality["flags"] + timing_flags))
        technical_flags = bool(flags)
        if decision == "flag":
            flags.append("transcript_mismatch")
        elif decision == "review":
            flags.append("transcript_review")
        quality = {
            "source_audio": original_quality,
            "clean_audio": clean_quality,
            "flags": flags,
            "asr_model": WHISPER_MODEL,
            "asr_backend": "api" if os.getenv("ASR_API_URL") else "local",
            "asr_language": "id",
            "asr_segments": segments,
            "transcript_decision": decision,
            "transcript_thresholds": {
                "max_wer": MAX_WER_THRESHOLD,
                "cer_review": CER_REVIEW_THRESHOLD,
                "cer_flag": CER_FLAG_THRESHOLD,
            },
        }
        final_paths = {
            "clean_video": output_dir / clean_mp4.name,
            "clean_audio": output_dir / clean_wav.name,
            "silent_video": output_dir / silent_mp4.name,
        }
        for key, final_path in final_paths.items():
            os.replace(
                {"clean_video": clean_mp4, "clean_audio": clean_wav, "silent_video": silent_mp4}[key],
                final_path,
            )
        report = {
            "participant_code": job["participant_code"],
            "sentence_id": recording["sentence_id"],
            "expected_text": recording["sentence_text"],
            "transcript": transcript,
            "wer": wer,
            "cer": cer,
            "quality": quality,
            "timing": timing,
            "source_video": str(source),
            "source_sha256": sha256(source),
            "outputs": {key: str(path) for key, path in final_paths.items()},
            "output_sha256": {key: sha256(path) for key, path in final_paths.items()},
            "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        report_path = output_dir / f"{base}_preprocessing.json"
        temporary_report = staging / report_path.name
        temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary_report, report_path)
        return {
            "status": (
                "flagged" if technical_flags or decision == "flag"
                else "review" if decision == "review"
                else "complete"
            ),
            **{key: str(path) for key, path in final_paths.items()},
            "transcript": transcript,
            "wer": wer,
            "cer": cer,
            "quality": quality,
            "timing": timing,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def write_manifest(job: dict) -> None:
    with closing(connect(DB_PATH)) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM preprocessing_results WHERE job_id=? ORDER BY sentence_id",
                (job["id"],),
            )
        ]
    manifest = {
        "job_id": job["id"],
        "participant_code": job["participant_code"],
        "rnnoise_model": str(RNNOISE_MODEL.resolve()),
        "rnnoise_model_sha256": sha256(RNNOISE_MODEL),
        "whisper_model": WHISPER_MODEL,
        "whisper_device": WHISPER_DEVICE,
        "whisper_compute_type": WHISPER_COMPUTE_TYPE,
        "results": rows,
    }
    output = Path(job["result_dir"]).parent / "manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f".{job['id']}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, output)


def process_job(job: dict, model, workers: int = 1) -> None:
    with closing(connect(DB_PATH)) as connection:
        existing = {
            row["sentence_id"]: row["status"]
            for row in connection.execute(
                "SELECT sentence_id, status FROM preprocessing_results WHERE job_id=?",
                (job["id"],),
            )
        }
    completed = sum(status in ("complete", "review", "flagged") for status in existing.values())
    failed = sum(status == "failed" for status in existing.values())
    flagged = sum(status == "flagged" for status in existing.values())
    pending = []
    for recording in job["source_snapshot"]:
        if existing.get(recording["sentence_id"]) in ("complete", "review", "flagged"):
            continue
        # A recovered worker retries a failed row in place. Remove its previous
        # contribution before recounting the replacement result.
        if existing.get(recording["sentence_id"]) == "failed":
            failed -= 1
        pending.append(recording)

    if STOP or job_cancelled(job["id"]):
        update_job(
            DB_PATH, job["id"], WORKER_ID, status="cancelled", stage="Cancelled",
            finished_at=time.time(), heartbeat_at=time.time(),
        )
        return

    def process_one(recording: dict) -> tuple[dict, dict]:
        try:
            return recording, process_recording(job, recording, model)
        except Exception as error:
            return recording, {"status": "failed", "error": str(error)[:2000]}

    worker_count = max(1, min(int(workers), len(pending) or 1))
    update_job(
        DB_PATH, job["id"], WORKER_ID,
        stage=f"Processing with {worker_count} worker(s)", heartbeat_at=time.time(),
    )
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(process_one, recording) for recording in pending]
        for future in as_completed(futures):
            recording, values = future.result()
            if values["status"] == "failed":
                failed += 1
            else:
                completed += 1
                flagged += values["status"] == "flagged"
            save_result(job, recording, values)
            update_job(
                DB_PATH, job["id"], WORKER_ID, completed_files=completed,
                failed_files=failed, flagged_files=flagged,
                current_sentence=recording["sentence_id"],
                stage="Recording result saved", heartbeat_at=time.time(),
            )
    write_manifest(job)
    status = "completed" if failed == 0 else "completed_with_errors"
    update_job(
        DB_PATH, job["id"], WORKER_ID, status=status,
        stage="Finished", current_sentence="", finished_at=time.time(),
        heartbeat_at=time.time(),
    )


def preflight() -> None:
    if not RNNOISE_MODEL.is_file():
        raise RuntimeError(
            f"RNNoise model not found: {RNNOISE_MODEL}. Set RNNOISE_MODEL_PATH."
        )
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("FFmpeg and FFprobe must be installed.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Participant preprocessing worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued participant")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    initialize(DB_PATH)
    preflight()
    model = load_asr_model()
    while not STOP:
        job = claim_job(DB_PATH, WORKER_ID)
        if job:
            try:
                process_job(job, model)
            except Exception as error:
                update_job(
                    DB_PATH, job["id"], WORKER_ID, status="failed",
                    stage="Worker failed", error=str(error)[:2000],
                    finished_at=time.time(), heartbeat_at=time.time(),
                )
            if args.once:
                return 0
        elif args.once:
            return 0
        else:
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
