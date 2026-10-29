"""Build participant-level AVSE-ready multimodal directory trees."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .dataset_readiness_system import connect, save_result, update_job


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_basename(normalized_video: Path) -> str:
    suffix = "_normalized.mp4"
    if not normalized_video.name.endswith(suffix):
        raise ValueError(f"Unexpected normalized filename: {normalized_video.name}")
    base = normalized_video.name[:-len(suffix)]
    if not base:
        raise ValueError("The canonical filename is empty.")
    return base


def atomic_materialize(source: Path, destination: Path, mode: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    method = mode
    if mode == "link":
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
            method = "copy-fallback"
    else:
        shutil.copy2(source, temporary)
    os.replace(temporary, destination)
    return method


def extract_audio(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.wav")
    try:
        result = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(temporary),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError((result.stderr or "Audio extraction failed.")[-2000:])
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_text(text: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(text.strip() + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def verify_source(item: dict, key: str) -> Path:
    path = Path(item[key])
    stat = path.stat()
    expected = item["fingerprints"][key]
    if stat.st_size != expected["size"] or stat.st_mtime_ns != expected["mtime_ns"]:
        raise RuntimeError(f"{key} changed after the export started.")
    return path


def export_recording(job: dict, item: dict) -> dict:
    normalized = verify_source(item, "normalized_video")
    face = verify_source(item, "face_video")
    mouth = verify_source(item, "mouth_video")
    base = canonical_basename(normalized)
    root = Path(job["result_dir"])
    destinations = {
        "video": root / "video" / f"{base}_video.mp4",
        "audio": root / "audio" / f"{base}_audio.wav",
        "text": root / "text" / f"{base}_text.txt",
        "face": root / "face" / f"{base}_face.mp4",
        "mouth": root / "mouth" / f"{base}_mouth.mp4",
    }
    methods = {
        "video": atomic_materialize(normalized, destinations["video"], job["export_mode"]),
        "face": atomic_materialize(face, destinations["face"], job["export_mode"]),
        "mouth": atomic_materialize(mouth, destinations["mouth"], job["export_mode"]),
    }
    extract_audio(normalized, destinations["audio"])
    write_text(item["sentence_text"], destinations["text"])
    metadata = {
        "sentence_id": item["sentence_id"], "reference_text": item["sentence_text"],
        "source_postprocessing_job": job["postprocessing_job_id"],
        "sources": {"video": str(normalized), "face": str(face), "mouth": str(mouth)},
        "materialization": methods, "audio_format": {
            "codec": "pcm_s16le", "sample_rate": 16000, "channels": 1,
        },
        "postprocessing": {
            "detection": json.loads(item["detection_json"] or "{}"),
            "loudness": json.loads(item["loudness_json"] or "{}"),
            "timing": json.loads(item["timing_json"] or "{}"),
        },
        "sha256": {key: sha256(path) for key, path in destinations.items()},
    }
    return {"status": "complete", "canonical_basename": base,
            **{f"{key}_path": str(path) for key, path in destinations.items()},
            "metadata": metadata}


def write_manifest(db_path: Path, job: dict) -> None:
    with connect(db_path) as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT sentence_id,canonical_basename,status,video_path,audio_path,
                      text_path,face_path,mouth_path,metadata_json,error
               FROM dataset_readiness_results WHERE job_id=? ORDER BY sentence_id""",
            (job["id"],),
        )]
    for row in rows:
        row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
    payload = {
        "job_id": job["id"], "participant_code": job["participant_code"],
        "postprocessing_job_id": job["postprocessing_job_id"],
        "export_mode": job["export_mode"], "modalities": ["video", "audio", "text", "face", "mouth"],
        "records": rows, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    output = Path(job["result_dir"]) / "manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f".{job['id']}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, output)


def process_job(db_path: Path, job: dict, workers: int) -> int:
    completed = failed = 0
    def process_one(item):
        try: return item, export_recording(job, item)
        except Exception as error: return item, {"status": "failed", "error": str(error)[:2500]}
    update_job(db_path, job["id"], stage=f"Exporting with {workers} worker(s)")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process_one, item) for item in job["source_snapshot"]]
        for future in as_completed(futures):
            item, result = future.result()
            if result["status"] == "complete": completed += 1
            else: failed += 1
            save_result(db_path, job, item, result)
            update_job(db_path, job["id"], completed_files=completed, failed_files=failed,
                       current_sentence=item["sentence_id"], stage="Export result saved")
    write_manifest(db_path, job)
    update_job(db_path, job["id"], status="completed" if failed == 0 else "completed_with_errors",
               current_sentence="", stage="Finished", finished_at=time.time())
    return failed
