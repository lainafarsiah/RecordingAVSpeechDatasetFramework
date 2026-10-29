"""Persistent queue and reporting helpers for offline participant preprocessing.

This module intentionally has no FastAPI or ML dependencies. The live web process
only creates/reads jobs; the optional preprocessing worker imports the heavy tools.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


ACTIVE_STATUSES = ("queued", "running")


def connect(db_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize(db_path: Path | str) -> None:
    with closing(connect(db_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS preprocessing_jobs (
                id TEXT PRIMARY KEY,
                participant_code TEXT NOT NULL,
                status TEXT NOT NULL,
                total_files INTEGER NOT NULL,
                completed_files INTEGER NOT NULL DEFAULT 0,
                failed_files INTEGER NOT NULL DEFAULT 0,
                flagged_files INTEGER NOT NULL DEFAULT 0,
                current_sentence TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT 'Waiting for worker',
                error TEXT NOT NULL DEFAULT '',
                source_snapshot TEXT NOT NULL,
                result_dir TEXT NOT NULL,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                heartbeat_at REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS preprocessing_active_participant
                ON preprocessing_jobs(participant_code)
                WHERE status IN ('queued', 'running');
            CREATE INDEX IF NOT EXISTS preprocessing_job_created
                ON preprocessing_jobs(created_at DESC);

            CREATE TABLE IF NOT EXISTS preprocessing_results (
                job_id TEXT NOT NULL,
                participant_code TEXT NOT NULL,
                sentence_id TEXT NOT NULL,
                status TEXT NOT NULL,
                source_video TEXT NOT NULL,
                clean_video TEXT NOT NULL DEFAULT '',
                clean_audio TEXT NOT NULL DEFAULT '',
                silent_video TEXT NOT NULL DEFAULT '',
                expected_text TEXT NOT NULL,
                transcript TEXT NOT NULL DEFAULT '',
                wer REAL,
                cer REAL,
                quality_json TEXT NOT NULL DEFAULT '{}',
                timing_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                processed_at REAL,
                PRIMARY KEY(job_id, sentence_id)
            );
            """
        )


def public_job(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    if "id" in item and "job_id" not in item:
        item["job_id"] = item.pop("id")
    item.pop("source_snapshot", None)
    item["cancel_requested"] = bool(item.get("cancel_requested"))
    total = item.get("total_files") or 0
    finished = (item.get("completed_files") or 0) + (item.get("failed_files") or 0)
    item["percent"] = round((finished / total) * 100, 1) if total else 0
    for key in ("created_at", "started_at", "finished_at", "heartbeat_at"):
        if item.get(key):
            item[key] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item[key]))
    return item


def enqueue_participant(
    db_path: Path | str,
    clean_root: Path,
    participant_code: str,
) -> tuple[dict, bool]:
    """Snapshot current recording rows and enqueue exactly one participant."""
    now = time.time()
    with closing(connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT * FROM preprocessing_jobs
               WHERE participant_code=? AND status IN ('queued','running')""",
            (participant_code,),
        ).fetchone()
        if existing:
            return public_job(existing), False

        participant = connection.execute(
            "SELECT participant_code FROM participants WHERE participant_code=?",
            (participant_code,),
        ).fetchone()
        if participant is None:
            raise LookupError("Participant not found.")
        pending = connection.execute(
            """SELECT 1 FROM media_jobs WHERE participant_code=?
               AND status IN ('queued','processing') LIMIT 1""",
            (participant_code,),
        ).fetchone()
        if pending:
            raise RuntimeError("This participant still has an upload waiting or processing.")

        recordings = [
            dict(row)
            for row in connection.execute(
                """SELECT id, sentence_id, sentence_text, original_mp4_path,
                          recording_time, duration_seconds
                   FROM recordings
                   WHERE participant_code=? AND quality_status!='retake_requested'
                   ORDER BY sentence_id""",
                (participant_code,),
            )
        ]
        if not recordings:
            raise ValueError("This participant has no current recordings to preprocess.")

        # The worker verifies these fingerprints before each file so a retake cannot
        # silently be confused with the recording that was approved for this run.
        for recording in recordings:
            source = Path(recording["original_mp4_path"])
            if not source.is_file():
                raise FileNotFoundError(
                    f"Source video is missing for {recording['sentence_id']}."
                )
            stat = source.stat()
            recording["source_size"] = stat.st_size
            recording["source_mtime_ns"] = stat.st_mtime_ns

        job_id = uuid.uuid4().hex
        result_dir = clean_root / participant_code / "files"
        connection.execute(
            """INSERT INTO preprocessing_jobs
               (id, participant_code, status, total_files, source_snapshot,
                result_dir, created_at)
               VALUES (?, ?, 'queued', ?, ?, ?, ?)""",
            (
                job_id,
                participant_code,
                len(recordings),
                json.dumps(recordings, ensure_ascii=False),
                str(result_dir),
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM preprocessing_jobs WHERE id=?", (job_id,)
        ).fetchone()
        return public_job(row), True


def start_participant_run(
    db_path: Path | str,
    clean_root: Path,
    participant_code: str,
    worker_id: str,
) -> dict:
    """Snapshot and start one participant immediately, without queueing."""
    now = time.time()
    with closing(connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            """SELECT 1 FROM preprocessing_jobs
               WHERE participant_code=? AND status IN ('queued','running') LIMIT 1""",
            (participant_code,),
        ).fetchone()
        if active:
            raise RuntimeError(f"{participant_code} is already being preprocessed.")
        if connection.execute(
            "SELECT 1 FROM participants WHERE participant_code=?", (participant_code,)
        ).fetchone() is None:
            raise LookupError("Participant not found.")
        if connection.execute(
            """SELECT 1 FROM media_jobs WHERE participant_code=?
               AND status IN ('queued','processing') LIMIT 1""",
            (participant_code,),
        ).fetchone():
            raise RuntimeError("This participant still has an upload waiting or processing.")

        recordings = [
            dict(row)
            for row in connection.execute(
                """SELECT id, sentence_id, sentence_text, original_mp4_path,
                          recording_time, duration_seconds
                   FROM recordings
                   WHERE participant_code=? AND quality_status!='retake_requested'
                   ORDER BY sentence_id""",
                (participant_code,),
            )
        ]
        if not recordings:
            raise ValueError("This participant has no current recordings to preprocess.")
        for recording in recordings:
            source = Path(recording["original_mp4_path"])
            if not source.is_file():
                raise FileNotFoundError(
                    f"Source video is missing for {recording['sentence_id']}."
                )
            stat = source.stat()
            recording["source_size"] = stat.st_size
            recording["source_mtime_ns"] = stat.st_mtime_ns

        run_id = uuid.uuid4().hex
        result_dir = clean_root / participant_code / "files"
        connection.execute(
            """INSERT INTO preprocessing_jobs
               (id, participant_code, status, total_files, source_snapshot,
                result_dir, created_at, started_at, heartbeat_at, worker_id, stage)
               VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, 'Loading model')""",
            (
                run_id, participant_code, len(recordings),
                json.dumps(recordings, ensure_ascii=False), str(result_dir),
                now, now, now, worker_id,
            ),
        )
        return {
            "id": run_id,
            "participant_code": participant_code,
            "status": "running",
            "total_files": len(recordings),
            "completed_files": 0,
            "failed_files": 0,
            "flagged_files": 0,
            "source_snapshot": recordings,
            "result_dir": str(result_dir),
            "worker_id": worker_id,
        }


def list_participants(db_path: Path | str) -> list[dict]:
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT p.participant_code,
                      COUNT(CASE WHEN r.quality_status!='retake_requested' THEN 1 END) AS recordings,
                      j.id AS job_id, j.status, j.total_files, j.completed_files,
                      j.failed_files, j.flagged_files, j.stage, j.error,
                      j.created_at, j.started_at, j.finished_at, j.heartbeat_at,
                      j.cancel_requested, j.result_dir, j.worker_id,
                      (SELECT ROUND(AVG(pr.wer), 4) FROM preprocessing_results pr
                       WHERE pr.job_id=j.id AND pr.wer IS NOT NULL) AS overall_wer,
                      (SELECT ROUND(AVG(pr.cer), 4) FROM preprocessing_results pr
                       WHERE pr.job_id=j.id AND pr.cer IS NOT NULL) AS overall_cer,
                      (SELECT COUNT(*) FROM preprocessing_results pr
                       WHERE pr.job_id=j.id AND pr.wer IS NOT NULL) AS scored_files
               FROM participants p
               LEFT JOIN recordings r ON r.participant_code=p.participant_code
               LEFT JOIN preprocessing_jobs j ON j.id=(
                   SELECT id FROM preprocessing_jobs
                   WHERE participant_code=p.participant_code
                   ORDER BY created_at DESC LIMIT 1
               )
               GROUP BY p.participant_code
               ORDER BY p.id"""
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        job = public_job(item) if item.get("job_id") else None
        if job:
            job["summary"] = {
                "overall_wer": job.pop("overall_wer", None),
                "overall_cer": job.pop("overall_cer", None),
                "scored_files": job.pop("scored_files", 0),
            }
        output.append(
            {
                "participant_code": item["participant_code"],
                "recordings": item["recordings"],
                "latest_job": job,
            }
        )
    return output


def get_job(db_path: Path | str, job_id: str, include_results: bool = True) -> dict | None:
    with closing(connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM preprocessing_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        job = public_job(row)
        if include_results:
            results = []
            for result in connection.execute(
                """SELECT sentence_id, status, source_video, expected_text, transcript, wer, cer,
                          quality_json, timing_json, error, clean_video,
                          clean_audio, silent_video
                   FROM preprocessing_results WHERE job_id=? ORDER BY sentence_id""",
                (job_id,),
            ):
                item = dict(result)
                item["quality"] = json.loads(item.pop("quality_json") or "{}")
                item["timing"] = json.loads(item.pop("timing_json") or "{}")
                results.append(item)
            job["results"] = results
            scored = [result for result in results if result["wer"] is not None]
            job["summary"] = {
                "scored_files": len(scored),
                "overall_wer": round(
                    sum(result["wer"] for result in scored) / len(scored), 4
                ) if scored else None,
                "overall_cer": round(
                    sum(result["cer"] for result in scored) / len(scored), 4
                ) if scored else None,
            }
        return job


def request_cancel(db_path: Path | str, job_id: str) -> bool:
    now = time.time()
    with closing(connect(db_path)) as connection, connection:
        result = connection.execute(
            """UPDATE preprocessing_jobs SET cancel_requested=1,
                      status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                      stage=CASE WHEN status='queued' THEN 'Cancelled' ELSE stage END,
                      finished_at=CASE WHEN status='queued' THEN ? ELSE finished_at END
               WHERE id=? AND status IN ('queued','running')""",
            (now, job_id),
        )
        return result.rowcount > 0


def claim_job(db_path: Path | str, worker_id: str, job_id: str | None = None) -> dict | None:
    now = time.time()
    with closing(connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        # A dead worker can be retried after ten minutes. Finished result rows make
        # processing idempotent at sentence granularity.
        connection.execute(
            """UPDATE preprocessing_jobs
               SET status='queued', stage='Recovered after worker interruption', worker_id=''
               WHERE status='running' AND heartbeat_at < ?""",
            (now - 600,),
        )
        if job_id:
            row = connection.execute(
                """SELECT * FROM preprocessing_jobs
                   WHERE id=? AND status='queued' AND cancel_requested=0""",
                (job_id,),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT * FROM preprocessing_jobs
                   WHERE status='queued' AND cancel_requested=0
                   ORDER BY created_at LIMIT 1"""
            ).fetchone()
        if row is None:
            connection.execute(
                """UPDATE preprocessing_jobs SET status='cancelled',
                          stage='Cancelled', finished_at=?
                   WHERE status='queued' AND cancel_requested=1""",
                (now,),
            )
            return None
        connection.execute(
            """UPDATE preprocessing_jobs SET status='running', started_at=COALESCE(started_at, ?),
                      heartbeat_at=?, worker_id=?, stage='Preparing models'
               WHERE id=?""",
            (now, now, worker_id, row["id"]),
        )
        job = dict(row)
        job["source_snapshot"] = json.loads(job["source_snapshot"])
        job["worker_id"] = worker_id
        return job


def update_job(db_path: Path | str, job_id: str, worker_id: str, **values) -> None:
    allowed = {
        "completed_files", "failed_files", "flagged_files", "current_sentence",
        "stage", "error", "status", "finished_at", "heartbeat_at",
    }
    data = {key: value for key, value in values.items() if key in allowed}
    if not data:
        return
    assignments = ", ".join(f"{key}=?" for key in data)
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            f"UPDATE preprocessing_jobs SET {assignments} WHERE id=? AND worker_id=?",
            (*data.values(), job_id, worker_id),
        )
