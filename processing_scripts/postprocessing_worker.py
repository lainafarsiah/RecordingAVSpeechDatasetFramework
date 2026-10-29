"""Loudness normalization and MediaPipe face/mouth derivative generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np

from .postprocessing_system import connect, save_result, update_job


TARGET_FPS = 25
TARGET_LUFS = -23.0
TRUE_PEAK_DBTP = -1.0
LOUDNESS_RANGE = 7.0
SMOOTHING_FRAMES = 15
FACE_SIZE = 512
MOUTH_SIZE = 112
FACE_PADDING = 0.30
MOUTH_PADDING = 0.30
FFMPEG_TIMEOUT = max(60, int(os.getenv("POSTPROCESS_FFMPEG_TIMEOUT", "600")))
_thread_state = threading.local()
_lip_indices = sorted({index for edge in mp.solutions.face_mesh.FACEMESH_LIPS for index in edge})


def run(command: list[str], timeout: int = FFMPEG_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("FFmpeg exceeded its processing time limit.") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "Media command failed.").strip()
        raise RuntimeError(detail[-2500:]) from error


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(path: Path) -> dict:
    payload = json.loads(run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,start_time,duration,avg_frame_rate,width,height",
        "-of", "json", str(path),
    ], timeout=60).stdout)
    result = {"duration": float(payload.get("format", {}).get("duration") or 0)}
    for stream in payload.get("streams", []):
        kind = stream.get("codec_type")
        if kind in ("audio", "video"):
            result[kind] = stream
    return result


def parse_loudnorm(stderr: str) -> dict:
    candidates = re.findall(r"\{\s*\"input_i\".*?\}", stderr, flags=re.DOTALL)
    if not candidates:
        raise RuntimeError("FFmpeg did not return loudness measurements.")
    return json.loads(candidates[-1])


def normalize_video(source: Path, destination: Path) -> dict:
    base = f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK_DBTP}:LRA={LOUDNESS_RANGE}"
    analysis = run([
        "ffmpeg", "-nostdin", "-hide_banner", "-i", str(source),
        "-map", "0:a:0", "-af", f"{base}:print_format=json", "-f", "null", "-",
    ])
    measured = parse_loudnorm(analysis.stderr)
    second_pass = (
        f"{base}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true:print_format=json"
    )
    result = run([
        "ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0", "-vf", f"fps={TARGET_FPS}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-af", second_pass, "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(destination),
    ])
    output = parse_loudnorm(result.stderr)
    return {
        "target_lufs": TARGET_LUFS, "true_peak_limit_dbtp": TRUE_PEAK_DBTP,
        "loudness_range_target": LOUDNESS_RANGE, "first_pass": measured,
        "second_pass": output, "method": "ffmpeg_loudnorm_two_pass_ebu_r128",
    }


def face_mesh():
    if not hasattr(_thread_state, "face_mesh"):
        _thread_state.face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False, max_num_faces=1, refine_landmarks=True,
            min_detection_confidence=0.5, min_tracking_confidence=0.5,
        )
    return _thread_state.face_mesh


def square_box(points: list[tuple[float, float]], width: int, height: int, padding: float):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    center_x, center_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    size = max((max(xs) - min(xs)) * width, (max(ys) - min(ys)) * height)
    return center_x * width, center_y * height, max(2.0, size * (1 + 2 * padding))


def crop_square(frame: np.ndarray, box: tuple[float, float, float], output_size: int) -> np.ndarray:
    center_x, center_y, size = box
    half = max(1, int(round(size / 2)))
    cx, cy = int(round(center_x)), int(round(center_y))
    x1, y1, x2, y2 = cx - half, cy - half, cx + half, cy + half
    height, width = frame.shape[:2]
    left, top, right, bottom = max(0, -x1), max(0, -y1), max(0, x2 - width), max(0, y2 - height)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
    crop = frame[y1:y2, x1:x2]
    if any((left, top, right, bottom)):
        crop = cv2.copyMakeBorder(crop, top, bottom, left, right,
                                  cv2.BORDER_CONSTANT, value=(0, 0, 0))
    return cv2.resize(crop, (output_size, output_size), interpolation=cv2.INTER_AREA)


def encoder(path: Path, size: int) -> subprocess.Popen:
    return subprocess.Popen([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo",
        "-pix_fmt", "bgr24", "-s", f"{size}x{size}", "-r", str(TARGET_FPS),
        "-i", "-", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
    ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def finish_encoder(process: subprocess.Popen, label: str) -> None:
    assert process.stdin is not None
    process.stdin.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    code = process.wait(timeout=FFMPEG_TIMEOUT)
    if code:
        raise RuntimeError(f"{label} encoding failed: {stderr[-2000:]}")


def create_crops(source: Path, face_silent: Path, mouth_silent: Path) -> dict:
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError("OpenCV could not open the normalized video.")
    face_process, mouth_process = encoder(face_silent, FACE_SIZE), encoder(mouth_silent, MOUTH_SIZE)
    face_history, mouth_history = deque(maxlen=SMOOTHING_FRAMES), deque(maxlen=SMOOTHING_FRAMES)
    frames = detections = reused = fallback = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames += 1
            height, width = frame.shape[:2]
            result = face_mesh().process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if result.multi_face_landmarks:
                landmarks = result.multi_face_landmarks[0].landmark
                all_points = [(item.x, item.y) for item in landmarks]
                lip_points = [(landmarks[index].x, landmarks[index].y) for index in _lip_indices]
                face_history.append(square_box(all_points, width, height, FACE_PADDING))
                mouth_history.append(square_box(lip_points, width, height, MOUTH_PADDING))
                detections += 1
            elif face_history:
                reused += 1
            else:
                fallback += 1
                face_history.append((width / 2, height / 2, min(width, height)))
                mouth_history.append((width / 2, height * 0.62, min(width, height) * 0.35))
            face_box = tuple(float(np.mean([box[i] for box in face_history])) for i in range(3))
            mouth_box = tuple(float(np.mean([box[i] for box in mouth_history])) for i in range(3))
            assert face_process.stdin is not None and mouth_process.stdin is not None
            face_process.stdin.write(crop_square(frame, face_box, FACE_SIZE).tobytes())
            mouth_process.stdin.write(crop_square(frame, mouth_box, MOUTH_SIZE).tobytes())
    finally:
        capture.release()
    finish_encoder(face_process, "Face crop")
    finish_encoder(mouth_process, "Mouth crop")
    if not frames:
        raise RuntimeError("The normalized video contains no decodable frames.")
    return {
        "frames": frames, "detected_frames": detections,
        "reused_previous_crop_frames": reused, "fallback_frames": fallback,
        "detection_rate": round(detections / frames, 4),
        "smoothing_frames": SMOOTHING_FRAMES, "face_size": FACE_SIZE,
        "mouth_size": MOUTH_SIZE, "face_padding": FACE_PADDING,
        "mouth_padding": MOUTH_PADDING, "landmark_model": "MediaPipe Face Mesh",
        "lip_landmark_indices": _lip_indices,
    }


def mux_audio(video: Path, audio_source: Path, destination: Path) -> None:
    run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video),
        "-i", str(audio_source), "-map", "0:v:0", "-map", "1:a:0",
        "-c", "copy", "-movflags", "+faststart", str(destination),
    ])


def process_recording(job: dict, source_item: dict) -> dict:
    source = Path(source_item["clean_video"])
    stat = source.stat()
    if stat.st_size != source_item["source_size"] or stat.st_mtime_ns != source_item["source_mtime_ns"]:
        raise RuntimeError("Clean source changed after this run was started.")
    output_dir = Path(job["result_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    base = source.name.removesuffix("_clean.mp4")
    with tempfile.TemporaryDirectory(prefix="postprocess-", dir=output_dir.parent) as temporary:
        staging = Path(temporary)
        normalized = staging / f"{base}_normalized.mp4"
        face_silent = staging / f"{base}_face_silent.mp4"
        mouth_silent = staging / f"{base}_mouth_silent.mp4"
        face = staging / f"{base}_face_512.mp4"
        mouth = staging / f"{base}_mouth_112.mp4"
        loudness = normalize_video(source, normalized)
        detection = create_crops(normalized, face_silent, mouth_silent)
        mux_audio(face_silent, normalized, face)
        mux_audio(mouth_silent, normalized, mouth)
        source_timing = probe(source)
        outputs = {"normalized_video": normalized, "face_video": face, "mouth_video": mouth}
        timing = {"source": source_timing, "outputs": {key: probe(path) for key, path in outputs.items()}}
        timing["max_duration_change_seconds"] = round(max(
            abs(item["duration"] - source_timing["duration"]) for item in timing["outputs"].values()
        ), 4)
        timing["audio_video_offsets_seconds"] = {
            key: round(
                float(item.get("audio", {}).get("start_time") or 0)
                - float(item.get("video", {}).get("start_time") or 0), 4
            ) for key, item in timing["outputs"].items()
        }
        timing["flags"] = []
        if timing["max_duration_change_seconds"] > 0.10:
            timing["flags"].append("duration_changed")
        if any(abs(value) > 0.05 for value in timing["audio_video_offsets_seconds"].values()):
            timing["flags"].append("audio_video_offset")
        final = {key: output_dir / path.name for key, path in outputs.items()}
        for key, path in outputs.items():
            os.replace(path, final[key])
        report = {
            "job_id": job["id"], "participant_code": job["participant_code"],
            "sentence_id": source_item["sentence_id"], "source_clean_video": str(source),
            "source_sha256": sha256(source), "outputs": {key: str(path) for key, path in final.items()},
            "output_sha256": {key: sha256(path) for key, path in final.items()},
            "loudness": loudness, "detection": detection, "timing": timing,
            "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        report_path = output_dir / f"{base}_postprocessing.json"
        temporary_report = staging / report_path.name
        temporary_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(temporary_report, report_path)
        return {"status": "complete", **{key: str(path) for key, path in final.items()},
                "loudness": loudness, "detection": detection, "timing": timing}


def process_job(db_path: Path, job: dict, workers: int) -> int:
    completed = failed = 0
    def process_one(item):
        try:
            return item, process_recording(job, item)
        except Exception as error:
            return item, {"status": "failed", "error": str(error)[:2500]}
    update_job(db_path, job["id"], stage=f"Processing with {workers} worker(s)")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process_one, item) for item in job["source_snapshot"]]
        for future in as_completed(futures):
            item, result = future.result()
            if result["status"] == "complete": completed += 1
            else: failed += 1
            save_result(db_path, job, item, result)
            update_job(db_path, job["id"], completed_files=completed, failed_files=failed,
                       current_sentence=item["sentence_id"], stage="Recording result saved")
    write_manifest(db_path, job)
    update_job(db_path, job["id"], status="completed" if failed == 0 else "completed_with_errors",
               stage="Finished", current_sentence="", finished_at=time.time())
    return failed


def write_manifest(db_path: Path, job: dict) -> None:
    with connect(db_path) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM postprocessing_results WHERE job_id=? ORDER BY sentence_id", (job["id"],)
        )]
    manifest = {
        "job_id": job["id"], "participant_code": job["participant_code"],
        "preprocessing_job_id": job["preprocessing_job_id"], "target_fps": TARGET_FPS,
        "target_lufs": TARGET_LUFS, "true_peak_limit_dbtp": TRUE_PEAK_DBTP,
        "mediapipe_version": mp.__version__, "opencv_version": cv2.__version__, "results": rows,
    }
    output = Path(job["result_dir"]).parent / "manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f".{job['id']}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(temporary, output)


def preflight() -> None:
    for command in ("ffmpeg", "ffprobe"):
        if shutil.which(command) is None:
            raise RuntimeError(f"{command} is required.")
