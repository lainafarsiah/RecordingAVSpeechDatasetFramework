import os
import subprocess
import glob
import json
import re
import string
import argparse
import sqlite3
import time
import traceback
import uuid
import cv2
import numpy as np
from faster_whisper import WhisperModel
import jiwer
from .asr_client import RemoteWhisperModel

# Define threshold flags / rules
MAX_WER_THRESHOLD = 0.25         # Flag if Word Error Rate > 25%
CER_REVIEW_THRESHOLD = 0.10
CER_FLAG_THRESHOLD = 0.20
MIN_BRIGHTNESS_THRESHOLD = 80    # Below 80 is considered too dark
MAX_BRIGHTNESS_THRESHOLD = 180   # Above 180 is considered too bright
MIN_VOLUME_DB_THRESHOLD = -45.0  # Corpus-derived conservative whole-clip screen

def parse_arguments():
    parser = argparse.ArgumentParser(description="Evaluate AV Dataset recordings for a specific participant.")
    parser.add_argument(
        "--userid", 
        type=str, 
        default="P0003", 
        help="Participant ID (e.g., P0003, P0004). Defaults to P0003."
    )
    parser.add_argument(
        "--base_path", 
        type=str, 
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings"),
        help="Root recordings directory path."
    )
    parser.add_argument("--db_path", type=str, default="", help="Application SQLite database path.")
    parser.add_argument(
        "--output_root", type=str,
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evaluation_reports"),
        help="Root directory for versioned evaluation reports.",
    )
    parser.add_argument("--run_id", type=str, default="", help="Evaluation run identifier.")
    parser.add_argument("--device", type=str, default="cpu", help="faster-whisper device.")
    parser.add_argument("--compute_type", type=str, default="int8", help="CTranslate2 compute type.")
    return parser.parse_args()


def update_run(db_path, run_id, **values):
    if not db_path or not run_id or not values:
        return
    allowed = {
        "status", "total_recordings", "completed_recordings", "summary_path",
        "error", "finished_at", "overall_wer", "overall_cer",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key}=?" for key in values)
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute(
            f"UPDATE evaluation_runs SET {assignments} WHERE id=?",
            (*values.values(), run_id),
        )


def current_recordings(db_path, userid):
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row) for row in connection.execute(
                """SELECT sentence_id, original_mp4_path, audio_wav_path, text_path
                   FROM recordings
                   WHERE participant_code=? AND quality_status!='retake_requested'
                   ORDER BY sentence_id""",
                (userid,),
            )
        ]


def write_json_atomic(path, payload):
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=4, ensure_ascii=False)
    os.replace(temporary, path)

def normalize_text(text):
    """Normalizes text by converting to lowercase, removing punctuation, and standardizing whitespace."""
    if not text:
        return ""
    # Convert to lowercase
    text = text.lower()
    # Remove punctuation using string.punctuation translation table
    text = text.translate(str.maketrans("", "", string.punctuation))
    # Replace multiple whitespace characters (including newlines/tabs) with a single space
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def transcript_error_rates(reference, hypothesis):
    normalized_reference = normalize_text(reference)
    normalized_hypothesis = normalize_text(hypothesis)
    wer_score = float(jiwer.wer(normalized_reference, normalized_hypothesis))
    reference_characters = normalized_reference.replace(" ", "")
    hypothesis_characters = normalized_hypothesis.replace(" ", "")
    cer_score = float(jiwer.cer(reference_characters, hypothesis_characters))
    return normalized_reference, normalized_hypothesis, round(wer_score, 4), round(cer_score, 4)

def transcript_decision(wer_score, cer_score):
    if cer_score > CER_FLAG_THRESHOLD or (
        wer_score > MAX_WER_THRESHOLD and cer_score > CER_REVIEW_THRESHOLD
    ):
        return "flag"
    if cer_score > CER_REVIEW_THRESHOLD:
        return "review"
    return "pass"

def check_audio_levels(audio_file):
    """Checks Noise & Voice levels using FFmpeg volumedetect."""
    if not os.path.exists(audio_file):
        return {"error": "Audio file not found"}, True

    cmd = ["ffmpeg", "-i", audio_file, "-af", "volumedetect", "-f", "null", "-"]
    result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    output = result.stderr

    mean_vol_match = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", output)
    max_vol_match = re.search(r"max_volume:\s*([-\d.]+)\s*dB", output)

    mean_volume = float(mean_vol_match.group(1)) if mean_vol_match else None
    max_volume = float(max_vol_match.group(1)) if max_vol_match else None

    is_flagged = False
    flags = []

    if mean_volume is not None and mean_volume < MIN_VOLUME_DB_THRESHOLD:
        is_flagged = True
        flags.append("VOICE_LEVEL_TOO_LOW")

    stats = {
        "mean_volume_db": mean_volume,
        "max_volume_db": max_volume,
        "flags": flags
    }
    return stats, is_flagged

def check_video_lightness(video_file):
    """Checks Video Quality (Lightness/Darkness) using OpenCV."""
    cap = cv2.VideoCapture(video_file)
    if not cap.isOpened():
        return {"error": "Could not open video file"}, True

    brightness_values = []
    frame_step = 30  # Sample every 30th frame
    count = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        if count % frame_step == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness_values.append(np.mean(gray))
        count += 1

    cap.release()

    avg_brightness = float(np.mean(brightness_values)) if brightness_values else 0.0
    
    is_flagged = False
    flags = []
    exposure = "Balanced Exposure"

    if avg_brightness < MIN_BRIGHTNESS_THRESHOLD:
        exposure = "Underexposed (Too Dark)"
        is_flagged = True
        flags.append("VIDEO_UNDEREXPOSED")
    elif avg_brightness > MAX_BRIGHTNESS_THRESHOLD:
        exposure = "Overexposed (Too Bright)"
        is_flagged = True
        flags.append("VIDEO_OVEREXPOSED")

    stats = {
        "avg_brightness": round(avg_brightness, 2),
        "exposure_status": exposure,
        "flags": flags
    }
    return stats, is_flagged

def calculate_wer(audio_file, reference_text_file, asr_model):
    """Computes Word Error Rate using faster-whisper (large-v3) and jiwer with text normalization."""
    if not os.path.exists(reference_text_file):
        return {"error": "Reference text file not found"}, True

    with open(reference_text_file, "r", encoding="utf-8") as f:
        raw_reference = f.read().strip()

    # Transcribe with faster-whisper large-v3
    segments, _ = asr_model.transcribe(audio_file, beam_size=5)
    raw_hypothesis = " ".join([segment.text for segment in segments]).strip()

    # Apply text normalization (lowercase, remove punctuation, normalize spaces)
    normalized_reference, normalized_hypothesis, wer_float, cer_float = transcript_error_rates(
        raw_reference, raw_hypothesis
    )
    wer_percent = round(wer_float * 100, 2)
    cer_percent = round(cer_float * 100, 2)
    decision = transcript_decision(wer_float, cer_float)

    is_flagged = False
    flags = []
    if decision == "flag":
        is_flagged = True
        flags.append("TRANSCRIPT_MISMATCH")
    elif decision == "review":
        flags.append("TRANSCRIPT_REVIEW")

    stats = {
        "reference_text": raw_reference,
        "hypothesis_text": raw_hypothesis,
        "normalized_reference": normalized_reference,
        "normalized_hypothesis": normalized_hypothesis,
        "word_error_rate": wer_float,
        "word_error_rate_percent": wer_percent,
        "character_error_rate": cer_float,
        "character_error_rate_percent": cer_percent,
        "transcript_decision": decision,
        "flags": flags
    }
    return stats, is_flagged

def main():
    args = parse_arguments()
    userid = args.userid.strip().upper()
    run_id = args.run_id or uuid.uuid4().hex
    participant_dir = os.path.join(args.base_path, userid)
    run_dir = os.path.join(args.output_root, userid, run_id)
    output_dir = os.path.join(run_dir, "records")
    os.makedirs(output_dir, exist_ok=True)

    if os.getenv("ASR_API_URL"):
        print(f"Using persistent ASR service for participant: {userid}...")
        asr_model = RemoteWhisperModel()
    else:
        print(f"Initializing faster-whisper (large-v3) for participant: {userid}...")
        asr_model = WhisperModel("large-v3", device=args.device, compute_type=args.compute_type)

    if args.db_path:
        recordings = current_recordings(args.db_path, userid)
    else:
        search_pattern = os.path.join(participant_dir, "**", "*_original.mp4")
        recordings = []
        for video_path in glob.glob(search_pattern, recursive=True):
            directory = os.path.dirname(video_path)
            base_name = os.path.basename(video_path).replace("_original.mp4", "")
            recordings.append({
                "sentence_id": base_name,
                "original_mp4_path": video_path,
                "audio_wav_path": os.path.join(directory, f"{base_name}_audio.wav"),
                "text_path": os.path.join(directory, f"{base_name}_text.txt"),
            })
    update_run(args.db_path, run_id, total_recordings=len(recordings))
    print(f"Found {len(recordings)} recordings to process for {userid}.")

    all_reports = []
    passed_count = 0
    review_count = 0
    failed_count = 0

    for idx, recording in enumerate(recordings, 1):
        video_path = recording["original_mp4_path"]
        base_name = os.path.basename(video_path).replace("_original.mp4", "")
        print(f"[{idx}/{len(recordings)}] Processing: {base_name}")
        audio_path = recording["audio_wav_path"]
        text_path = recording["text_path"]

        audio_stats, audio_flagged = check_audio_levels(audio_path)
        video_stats, video_flagged = check_video_lightness(video_path)
        wer_stats, wer_flagged = calculate_wer(audio_path, text_path, asr_model)

        all_flags = audio_stats.get("flags", []) + video_stats.get("flags", []) + wer_stats.get("flags", [])
        needs_review = wer_stats.get("transcript_decision") == "review"
        recording_flagged = audio_flagged or video_flagged or wer_flagged
        if recording_flagged:
            failed_count += 1
            recording_status = "FLAGGED"
        elif needs_review:
            review_count += 1
            recording_status = "REVIEW"
        else:
            passed_count += 1
            recording_status = "PASSED"

        report = {
            "recording_id": base_name,
            "sentence_id": recording.get("sentence_id", base_name),
            "file_paths": {
                "video": video_path,
                "audio": audio_path,
                "transcript": text_path
            },
            "status": recording_status,
            "flags": all_flags,
            "audio_evaluation": audio_stats,
            "video_evaluation": video_stats,
            "asr_evaluation": wer_stats
        }

        output_json_path = os.path.join(output_dir, f"{base_name}_report.json")
        write_json_atomic(output_json_path, report)

        all_reports.append(report)
        update_run(args.db_path, run_id, completed_recordings=idx)

    total_processed = len(recordings)
    avg_dataset_wer = (
        sum([r["asr_evaluation"].get("word_error_rate", 0) for r in all_reports]) / total_processed
        if total_processed > 0 else 0
    )
    avg_dataset_brightness = (
        sum([r["video_evaluation"].get("avg_brightness", 0) for r in all_reports]) / total_processed
        if total_processed > 0 else 0
    )

    master_summary = {
        "userid": userid,
        "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_recordings_processed": total_processed,
        "passed_count": passed_count,
        "review_count": review_count,
        "flagged_count": failed_count,
        "dataset_averages": {
            "average_word_error_rate": round(avg_dataset_wer, 4),
            "average_word_error_rate_percent": round(avg_dataset_wer * 100, 2),
            "average_character_error_rate": round(
                sum(r["asr_evaluation"].get("character_error_rate", 0) for r in all_reports) / total_processed,
                4,
            ) if total_processed else 0,
            "average_character_error_rate_percent": round(
                sum(r["asr_evaluation"].get("character_error_rate", 0) for r in all_reports) / total_processed * 100,
                2,
            ) if total_processed else 0,
            "average_brightness": round(avg_dataset_brightness, 2)
        },
        "thresholds_applied": {
            "max_wer_threshold": MAX_WER_THRESHOLD,
            "cer_review_threshold": CER_REVIEW_THRESHOLD,
            "cer_flag_threshold": CER_FLAG_THRESHOLD,
            "min_brightness_threshold": MIN_BRIGHTNESS_THRESHOLD,
            "max_brightness_threshold": MAX_BRIGHTNESS_THRESHOLD,
            "min_volume_db_threshold": MIN_VOLUME_DB_THRESHOLD
        },
        "records": all_reports
    }

    master_summary_path = os.path.join(run_dir, f"{userid}_master_evaluation_summary.json")
    write_json_atomic(master_summary_path, master_summary)
    update_run(
        args.db_path, run_id, status="completed", completed_recordings=total_processed,
        summary_path=os.path.abspath(master_summary_path), finished_at=time.time(),
        overall_wer=master_summary["dataset_averages"]["average_word_error_rate"],
        overall_cer=master_summary["dataset_averages"]["average_character_error_rate"],
    )

    print(f"\nBatch processing complete for {userid}!")
    print(f"Total: {total_processed} | Passed: {passed_count} | Flagged: {failed_count}")
    print(f"Master summary report saved to: {master_summary_path}")

if __name__ == "__main__":
    parsed_run_id = ""
    parsed_db_path = ""
    try:
        arguments = parse_arguments()
        parsed_run_id = arguments.run_id
        parsed_db_path = arguments.db_path
        main()
    except Exception as error:
        update_run(
            parsed_db_path, parsed_run_id, status="failed", error=str(error)[:2000],
            finished_at=time.time(),
        )
        traceback.print_exc()
        raise
