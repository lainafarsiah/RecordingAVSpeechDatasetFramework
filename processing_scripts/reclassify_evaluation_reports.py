#!/usr/bin/env python3
"""Reclassify the latest completed evaluation report without rerunning ASR."""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processing_scripts.dataset_evaluation import (
    CER_FLAG_THRESHOLD,
    CER_REVIEW_THRESHOLD,
    MAX_BRIGHTNESS_THRESHOLD,
    MAX_WER_THRESHOLD,
    MIN_BRIGHTNESS_THRESHOLD,
    MIN_VOLUME_DB_THRESHOLD,
    transcript_decision,
    transcript_error_rates,
)


DB_PATH = ROOT / "data" / "app.db"


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def reclassify_record(item: dict) -> None:
    audio = item.setdefault("audio_evaluation", {})
    video = item.setdefault("video_evaluation", {})
    asr = item.setdefault("asr_evaluation", {})

    audio_flags = [flag for flag in audio.get("flags", []) if flag != "VOICE_LEVEL_TOO_LOW"]
    mean_volume = audio.get("mean_volume_db")
    if isinstance(mean_volume, (int, float)) and mean_volume < MIN_VOLUME_DB_THRESHOLD:
        audio_flags.append("VOICE_LEVEL_TOO_LOW")
    audio["flags"] = audio_flags

    video_flags = [
        flag for flag in video.get("flags", [])
        if flag not in {"VIDEO_UNDEREXPOSED", "VIDEO_OVEREXPOSED"}
    ]
    brightness = video.get("avg_brightness")
    if isinstance(brightness, (int, float)) and brightness < MIN_BRIGHTNESS_THRESHOLD:
        video_flags.append("VIDEO_UNDEREXPOSED")
        video["exposure_status"] = "Underexposed (Too Dark)"
    elif isinstance(brightness, (int, float)) and brightness > MAX_BRIGHTNESS_THRESHOLD:
        video_flags.append("VIDEO_OVEREXPOSED")
        video["exposure_status"] = "Overexposed (Too Bright)"
    elif isinstance(brightness, (int, float)):
        video["exposure_status"] = "Balanced Exposure"
    video["flags"] = video_flags

    _reference, _hypothesis, wer, cer = transcript_error_rates(
        asr.get("reference_text", ""), asr.get("hypothesis_text", "")
    )
    decision = transcript_decision(wer, cer)
    asr["word_error_rate"] = wer
    asr["word_error_rate_percent"] = round(wer * 100, 2)
    asr["character_error_rate"] = cer
    asr["character_error_rate_percent"] = round(cer * 100, 2)
    asr["transcript_decision"] = decision
    asr["flags"] = (
        ["TRANSCRIPT_MISMATCH"] if decision == "flag"
        else ["TRANSCRIPT_REVIEW"] if decision == "review"
        else []
    )

    item["flags"] = audio_flags + video_flags + asr["flags"]
    if audio_flags or video_flags or decision == "flag":
        item["status"] = "FLAGGED"
    elif decision == "review":
        item["status"] = "REVIEW"
    else:
        item["status"] = "PASSED"


def main() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT e.participant_code, e.summary_path
               FROM evaluation_runs e
               JOIN (SELECT participant_code, MAX(created_at) AS created_at
                     FROM evaluation_runs WHERE status='completed'
                     GROUP BY participant_code) latest
                 ON latest.participant_code=e.participant_code
                AND latest.created_at=e.created_at
               ORDER BY e.participant_code"""
        ).fetchall()

    for row in rows:
        summary_path = Path(row["summary_path"])
        if not summary_path.is_file():
            print(f"{row['participant_code']}: missing summary; skipped")
            continue
        report = json.loads(summary_path.read_text(encoding="utf-8"))
        current_thresholds = report.get("thresholds_applied", {})
        already_current = (
            current_thresholds.get("max_wer_threshold") == MAX_WER_THRESHOLD
            and current_thresholds.get("cer_review_threshold") == CER_REVIEW_THRESHOLD
            and current_thresholds.get("cer_flag_threshold") == CER_FLAG_THRESHOLD
            and current_thresholds.get("min_brightness_threshold") == MIN_BRIGHTNESS_THRESHOLD
            and current_thresholds.get("max_brightness_threshold") == MAX_BRIGHTNESS_THRESHOLD
            and current_thresholds.get("min_volume_db_threshold") == MIN_VOLUME_DB_THRESHOLD
            and "review_count" in report
            and all(
                "character_error_rate" in item.get("asr_evaluation", {})
                for item in report.get("records", [])
            )
        )
        if already_current:
            print(f"{row['participant_code']}: already current")
            continue
        records = report.get("records", [])
        for item in records:
            reclassify_record(item)
            record_path = summary_path.parent / "records" / f"{item['recording_id']}_report.json"
            if record_path.is_file():
                write_json_atomic(record_path, item)

        report["passed_count"] = sum(item.get("status") == "PASSED" for item in records)
        report["review_count"] = sum(item.get("status") == "REVIEW" for item in records)
        report["flagged_count"] = sum(item.get("status") == "FLAGGED" for item in records)
        averages = report.setdefault("dataset_averages", {})
        if records:
            average_cer = sum(item["asr_evaluation"]["character_error_rate"] for item in records) / len(records)
            averages["average_character_error_rate"] = round(average_cer, 4)
            averages["average_character_error_rate_percent"] = round(average_cer * 100, 2)
        report["thresholds_applied"] = {
            "max_wer_threshold": MAX_WER_THRESHOLD,
            "cer_review_threshold": CER_REVIEW_THRESHOLD,
            "cer_flag_threshold": CER_FLAG_THRESHOLD,
            "min_brightness_threshold": MIN_BRIGHTNESS_THRESHOLD,
            "max_brightness_threshold": MAX_BRIGHTNESS_THRESHOLD,
            "min_volume_db_threshold": MIN_VOLUME_DB_THRESHOLD,
        }
        write_json_atomic(summary_path, report)
        print(
            f"{row['participant_code']}: pass={report['passed_count']} "
            f"review={report['review_count']} flagged={report['flagged_count']}"
        )


if __name__ == "__main__":
    main()
