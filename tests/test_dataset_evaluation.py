import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from processing_scripts import dataset_evaluation


class FakeWhisperModel:
    def __init__(self, *_args, **_kwargs):
        pass

    def transcribe(self, _audio, **_kwargs):
        return iter([SimpleNamespace(text="Ini kalimat uji")]), None


class DatasetEvaluationTests(unittest.TestCase):
    def test_cer_handles_indonesian_word_boundary_difference(self):
        reference = "Festival wayang golek digelar aula kecamatan."
        hypothesis = "Festival Wayang Golek di Gelar Aula Kejamatan."
        _reference, _hypothesis, wer, cer = dataset_evaluation.transcript_error_rates(
            reference, hypothesis
        )
        self.assertEqual(wer, 0.5)
        self.assertEqual(cer, 0.0256)
        self.assertEqual(dataset_evaluation.transcript_decision(wer, cer), "pass")

    def test_voice_level_uses_mean_volume_threshold(self):
        below_threshold = SimpleNamespace(
            stderr="mean_volume: -45.1 dB\nmax_volume: -15.0 dB\n"
        )
        with patch.object(dataset_evaluation.subprocess, "run", return_value=below_threshold):
            stats, flagged = dataset_evaluation.check_audio_levels(__file__)
        self.assertTrue(flagged)
        self.assertEqual(stats["flags"], ["VOICE_LEVEL_TOO_LOW"])

        at_threshold = SimpleNamespace(
            stderr="mean_volume: -45.0 dB\nmax_volume: -26.0 dB\n"
        )
        with patch.object(dataset_evaluation.subprocess, "run", return_value=at_threshold):
            stats, flagged = dataset_evaluation.check_audio_levels(__file__)
        self.assertFalse(flagged)
        self.assertEqual(stats["flags"], [])

    def test_current_recording_evaluation_writes_versioned_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            participant_dir = root / "recordings" / "P0001" / "files"
            participant_dir.mkdir(parents=True)
            video = participant_dir / "P0001_ID0001_original.mp4"
            audio = participant_dir / "P0001_ID0001_audio.wav"
            text = participant_dir / "P0001_ID0001_text.txt"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=c=gray:s=160x120:r=15", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=16000", "-t", "0.4",
                "-c:v", "libx264", "-threads", "1", "-c:a", "aac",
                "-y", str(video),
            ], check=True)
            subprocess.run([
                "ffmpeg", "-v", "error", "-i", str(video), "-vn",
                "-ac", "1", "-ar", "16000", "-y", str(audio),
            ], check=True)
            text.write_text("Ini kalimat uji", encoding="utf-8")
            database = root / "app.db"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE recordings (
                        participant_code TEXT, sentence_id TEXT,
                        original_mp4_path TEXT, audio_wav_path TEXT,
                        text_path TEXT, quality_status TEXT
                    );
                    CREATE TABLE evaluation_runs (
                        id TEXT PRIMARY KEY, status TEXT, total_recordings INTEGER,
                        completed_recordings INTEGER, summary_path TEXT,
                        error TEXT, finished_at REAL,
                        overall_wer REAL, overall_cer REAL
                    );
                    INSERT INTO evaluation_runs VALUES
                        ('run-test','running',0,0,'','',NULL,NULL,NULL);
                """)
                connection.execute(
                    "INSERT INTO recordings VALUES (?,?,?,?,?,'unchecked')",
                    ("P0001", "ID0001", str(video), str(audio), str(text)),
                )

            output_root = root / "reports"
            arguments = [
                "dataset_evaluation.py", "--userid", "P0001",
                "--base_path", str(root / "recordings"),
                "--db_path", str(database), "--output_root", str(output_root),
                "--run_id", "run-test",
            ]
            with patch.object(sys, "argv", arguments), \
                 patch.object(dataset_evaluation, "WhisperModel", FakeWhisperModel):
                dataset_evaluation.main()

            with sqlite3.connect(database) as connection:
                status, completed, summary_path = connection.execute(
                    "SELECT status, completed_recordings, summary_path FROM evaluation_runs"
                ).fetchone()
            self.assertEqual((status, completed), ("completed", 1))
            report = json.loads(Path(summary_path).read_text(encoding="utf-8"))
            self.assertEqual(report["total_recordings_processed"], 1)
            self.assertEqual(report["passed_count"], 1)
            self.assertEqual(report["records"][0]["asr_evaluation"]["word_error_rate"], 0)
            self.assertEqual(report["records"][0]["asr_evaluation"]["character_error_rate"], 0)
            self.assertEqual(report["records"][0]["asr_evaluation"]["transcript_decision"], "pass")
            self.assertTrue(str(Path(summary_path)).startswith(str(output_root / "P0001" / "run-test")))


if __name__ == "__main__":
    unittest.main()
