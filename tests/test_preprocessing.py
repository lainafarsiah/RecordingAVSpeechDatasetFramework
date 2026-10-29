import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import app
from processing_scripts import preprocessing_system
from processing_scripts import preprocessing_worker
from processing_scripts import preprocessing
from media_jobs import MediaQueue


class FakeWhisper:
    def transcribe(self, _path, **_options):
        return iter([SimpleNamespace(start=0.0, end=0.5, text="Ini kalimat uji")]), None


class PreprocessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcript_decision_uses_cer_with_wer(self):
        reference = "Festival wayang golek digelar aula kecamatan."
        hypothesis = "Festival Wayang Golek di Gelar Aula Kejamatan."
        wer, cer = preprocessing_worker.error_rates(reference, hypothesis)
        self.assertEqual((wer, cer), (0.5, 0.0256))
        self.assertEqual(preprocessing_worker.transcript_decision(wer, cer), "pass")
        self.assertEqual(preprocessing_worker.transcript_decision(0.2, 0.15), "review")
        self.assertEqual(preprocessing_worker.transcript_decision(0.3, 0.15), "flag")

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db = root / "app.db"
        self.recordings = root / "recordings"
        self.clean = root / "recording_clean"
        self.evaluation_reports = root / "evaluation_reports"
        self.model = Path(__file__).resolve().parents[1] / "models" / "rnnoise.rnnn"
        self.patches = [
            patch.object(app, "DB_PATH", self.db),
            patch.object(app, "DATA_DIR", root / "data"),
            patch.object(app, "RECORDINGS_DIR", self.recordings),
            patch.object(app, "CLEAN_RECORDINGS_DIR", self.clean),
            patch.object(app, "EVALUATION_REPORTS_DIR", self.evaluation_reports),
            patch.object(app, "LEGACY_EVALUATION_REPORTS_DIR", root / "legacy_evaluation_reports"),
            patch.object(app, "EVALUATION_PYTHON", Path("/bin/true")),
            patch.object(app, "PREPROCESSING_PYTHON", Path("/bin/true")),
            patch.object(preprocessing_worker, "DB_PATH", self.db),
            patch.object(preprocessing_worker, "CLEAN_ROOT", self.clean),
            patch.object(preprocessing_worker, "RNNOISE_MODEL", self.model),
        ]
        for item in self.patches:
            item.start()
        app.init_db()
        MediaQueue(self.db, lambda _job: {}).initialize()
        participant_dir = self.recordings / "P0001"
        participant_dir.mkdir(parents=True)
        self.source = participant_dir / "P0001_S001_original.mp4"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=s=320x180:r=15", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000", "-t", "0.6",
                "-c:v", "libx264", "-threads", "1", "-c:a", "aac",
                "-y", str(self.source),
            ],
            check=True,
        )
        self.second_source = participant_dir / "P0001_S002_original.mp4"
        self.second_source.write_bytes(self.source.read_bytes())
        with app.get_db() as connection:
            connection.execute(
                """INSERT INTO participants
                   (participant_code, participant_name, age_group, gender,
                    native_language, indonesian_accent, consent_agreed,
                    consent_version, consent_timestamp, consent_text_path,
                    consent_pdf_path, record_date, recording_time,
                    public_speaking_ability, communication_style)
                   VALUES ('P0001','','18-25','female','N/A','baku_netral',0,
                           '','','','','today','today','A','B')"""
            )
            connection.execute(
                """INSERT INTO participant_credentials
                   (participant_code,password,created_at) VALUES ('P0001','test','today')"""
            )
            connection.execute(
                """INSERT INTO recordings
                   (participant_code,sentence_id,sentence_text,original_mp4_path,
                    audio_wav_path,video_only_mp4_path,text_path,recording_time,duration_seconds)
                   VALUES ('P0001','S001','Ini kalimat uji',?,'','','','today',0.6)""",
                (str(self.source),),
            )
            connection.execute(
                """INSERT INTO recordings
                   (participant_code,sentence_id,sentence_text,original_mp4_path,
                    audio_wav_path,video_only_mp4_path,text_path,recording_time,duration_seconds)
                   VALUES ('P0001','S002','Ini kalimat uji',?,'','','','today',0.6)""",
                (str(self.second_source),),
            )
        async def heartbeat():
            while True:
                await asyncio.sleep(0.01)
        self.heartbeat = asyncio.create_task(heartbeat())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.app), base_url="https://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.heartbeat.cancel()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    async def login_admin(self):
        response = await self.client.post(
            f"{app.ADMIN_BASE_PATH}/login", data={"password": app.ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 303)

    async def test_protected_page_queue_progress_and_real_media_pipeline(self):
        self.assertEqual((await self.client.get(f"{app.ADMIN_BASE_PATH}/preprocessing.html")).status_code, 303)
        self.assertEqual((await self.client.get("/api/admin/preprocessing/participants")).status_code, 401)
        await self.login_admin()
        self.assertEqual((await self.client.get(f"{app.ADMIN_BASE_PATH}/preprocessing.html")).status_code, 200)

        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with patch.object(sys, "argv", ["preprocessing.py", "--participant_id", "P0001"]), \
             patch.object(preprocessing_worker, "load_asr_model", return_value=FakeWhisper()):
            self.assertEqual(preprocessing.main(), 0)
        latest = preprocessing_system.list_participants(self.db)[0]["latest_job"]
        job_id = latest["job_id"]

        result = (await self.client.get(f"/api/admin/preprocessing/jobs/{job_id}")).json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["percent"], 100)
        self.assertEqual(result["total_files"], 2)
        self.assertEqual(result["completed_files"], 2)
        self.assertEqual(result["results"][0]["transcript"], "Ini kalimat uji")
        self.assertEqual(result["results"][0]["wer"], 0)
        self.assertEqual(result["summary"]["overall_wer"], 0)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)
        for kind in ("original", "video", "audio", "silent-video"):
            download = await self.client.get(
                f"/api/admin/preprocessing/jobs/{job_id}/files/S001/{kind}"
            )
            self.assertEqual(download.status_code, 200, kind)
            self.assertGreater(len(download.content), 100)
        manifest = self.clean / "P0001" / "manifest.json"
        self.assertTrue(manifest.is_file())

    async def test_changed_source_fails_without_overwriting_source(self):
        await self.login_admin()
        queued, _created = preprocessing_system.enqueue_participant(
            self.db, self.clean, "P0001"
        )
        job_id = queued["job_id"]
        with self.source.open("ab") as handle:
            handle.write(b"changed")
        changed_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        job = preprocessing_system.claim_job(self.db, preprocessing_worker.WORKER_ID)
        preprocessing_worker.process_job(job, FakeWhisper())
        result = (await self.client.get(f"/api/admin/preprocessing/jobs/{job_id}")).json()
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["failed_files"], 1)
        self.assertIn("changed", result["results"][0]["error"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), changed_hash)

    async def test_queued_job_can_be_cancelled_immediately(self):
        await self.login_admin()
        queued, _created = preprocessing_system.enqueue_participant(
            self.db, self.clean, "P0001"
        )
        job_id = queued["job_id"]
        self.assertTrue(preprocessing_system.request_cancel(self.db, job_id))
        job = (await self.client.get(f"/api/admin/preprocessing/jobs/{job_id}")).json()
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["stage"], "Cancelled")
        self.assertIsNone(preprocessing_system.claim_job(self.db, preprocessing_worker.WORKER_ID))

    async def test_preprocessing_can_be_started_from_admin(self):
        self.assertEqual(
            (await self.client.post("/api/admin/preprocessing/participant/P0001/start")).status_code,
            401,
        )
        await self.login_admin()
        with patch.object(app.subprocess, "Popen", return_value=SimpleNamespace(pid=54321)) as popen:
            response = await self.client.post(
                "/api/admin/preprocessing/participant/P0001/start"
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "starting")
        command = popen.call_args.args[0]
        self.assertIn("processing_scripts.preprocessing", command)
        self.assertIn("P0001", command)

    async def test_evaluation_admin_launch_and_report_access(self):
        self.assertEqual((await self.client.get(f"{app.ADMIN_BASE_PATH}/report.html")).status_code, 303)
        self.assertEqual((await self.client.post("/api/admin/evaluations/participant/P0001")).status_code, 401)
        await self.login_admin()
        with patch.object(app.subprocess, "Popen", return_value=SimpleNamespace(pid=12345)):
            response = await self.client.post("/api/admin/evaluations/participant/P0001")
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run"]["id"]
        report_dir = self.evaluation_reports / "P0001" / run_id
        report_dir.mkdir(parents=True)
        report_path = report_dir / "P0001_master_evaluation_summary.json"
        report_path.write_text(json.dumps({
            "userid": "P0001", "total_recordings_processed": 2,
            "passed_count": 1, "flagged_count": 1, "records": [],
        }), encoding="utf-8")
        with app.get_db() as connection:
            connection.execute(
                """UPDATE evaluation_runs SET status='completed', completed_recordings=2,
                   summary_path=?, finished_at=1 WHERE id=?""",
                (str(report_path), run_id),
            )
        detail = await self.client.get(f"/api/admin/evaluations/runs/{run_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["report"]["flagged_count"], 1)
        participants = await self.client.get("/api/admin/evaluations/participants")
        self.assertEqual(participants.json()["participants"][0]["latest_run"]["id"], run_id)


if __name__ == "__main__":
    unittest.main()
