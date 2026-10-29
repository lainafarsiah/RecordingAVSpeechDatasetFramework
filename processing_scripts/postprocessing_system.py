"""SQLite state and source selection for clean-recording post-processing."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


def connect(db_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize(db_path: Path | str) -> None:
    with closing(connect(db_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS postprocessing_jobs (
                id TEXT PRIMARY KEY,
                participant_code TEXT NOT NULL,
                preprocessing_job_id TEXT NOT NULL,
                status TEXT NOT NULL,
                total_files INTEGER NOT NULL,
                completed_files INTEGER NOT NULL DEFAULT 0,
                failed_files INTEGER NOT NULL DEFAULT 0,
                current_sentence TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                source_snapshot TEXT NOT NULL,
                result_dir TEXT NOT NULL,
                created_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS postprocessing_active_participant
                ON postprocessing_jobs(participant_code)
                WHERE status='running';
            CREATE INDEX IF NOT EXISTS postprocessing_participant_created
                ON postprocessing_jobs(participant_code, created_at DESC);
            CREATE TABLE IF NOT EXISTS postprocessing_results (
                job_id TEXT NOT NULL,
                participant_code TEXT NOT NULL,
                sentence_id TEXT NOT NULL,
                status TEXT NOT NULL,
                source_clean_video TEXT NOT NULL,
                normalized_video TEXT NOT NULL DEFAULT '',
                face_video TEXT NOT NULL DEFAULT '',
                mouth_video TEXT NOT NULL DEFAULT '',
                detection_json TEXT NOT NULL DEFAULT '{}',
                loudness_json TEXT NOT NULL DEFAULT '{}',
                timing_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                processed_at REAL,
                PRIMARY KEY(job_id, sentence_id)
            );
            """
        )


def clean_sources(connection: sqlite3.Connection, participant_code: str) -> tuple[str, list[dict]]:
    job = connection.execute(
        """SELECT id FROM preprocessing_jobs
           WHERE participant_code=? AND status='completed' AND failed_files=0
           ORDER BY created_at DESC LIMIT 1""",
        (participant_code,),
    ).fetchone()
    if job is None:
        raise ValueError("No completed preprocessing run is available for this participant.")
    rows = connection.execute(
        """SELECT sentence_id, clean_video FROM preprocessing_results
           WHERE job_id=? AND status IN ('complete','review','flagged')
           ORDER BY sentence_id""",
        (job["id"],),
    ).fetchall()
    sources = []
    for row in rows:
        path = Path(row["clean_video"])
        if not path.is_file():
            raise FileNotFoundError(f"Clean video is missing for {row['sentence_id']}.")
        stat = path.stat()
        sources.append({
            "sentence_id": row["sentence_id"],
            "clean_video": str(path.resolve()),
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
        })
    if not sources:
        raise ValueError("The completed preprocessing run has no usable clean videos.")
    return job["id"], sources


def start_run(db_path: Path | str, output_root: Path, participant_code: str) -> dict:
    now = time.time()
    with closing(connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM postprocessing_jobs WHERE participant_code=? AND status='running'",
            (participant_code,),
        ).fetchone():
            raise RuntimeError(f"{participant_code} is already being post-processed.")
        preprocessing_job_id, sources = clean_sources(connection, participant_code)
        job_id = uuid.uuid4().hex
        result_dir = output_root / participant_code / "files"
        connection.execute(
            """INSERT INTO postprocessing_jobs
               (id,participant_code,preprocessing_job_id,status,total_files,
                source_snapshot,result_dir,created_at,stage)
               VALUES (?,?,?,'running',?,?,?,?,?)""",
            (job_id, participant_code, preprocessing_job_id, len(sources),
             json.dumps(sources), str(result_dir), now, "Starting"),
        )
    return {
        "id": job_id, "participant_code": participant_code,
        "preprocessing_job_id": preprocessing_job_id, "status": "running",
        "total_files": len(sources), "source_snapshot": sources,
        "result_dir": str(result_dir),
    }


def update_job(db_path: Path | str, job_id: str, **values) -> None:
    allowed = {"status", "completed_files", "failed_files", "current_sentence",
               "stage", "error", "finished_at"}
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            f"UPDATE postprocessing_jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )


def save_result(db_path: Path | str, job: dict, source: dict, result: dict) -> None:
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            """INSERT OR REPLACE INTO postprocessing_results
               (job_id,participant_code,sentence_id,status,source_clean_video,
                normalized_video,face_video,mouth_video,detection_json,
                loudness_json,timing_json,error,processed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["id"], job["participant_code"], source["sentence_id"],
             result["status"], source["clean_video"],
             result.get("normalized_video", ""), result.get("face_video", ""),
             result.get("mouth_video", ""), json.dumps(result.get("detection", {})),
             json.dumps(result.get("loudness", {})), json.dumps(result.get("timing", {})),
             result.get("error", ""), time.time()),
        )


def current_completed_run(db_path: Path | str, output_root: Path, participant_code: str) -> bool:
    with closing(connect(db_path)) as connection:
        try:
            preprocessing_job_id, sources = clean_sources(connection, participant_code)
        except (ValueError, FileNotFoundError):
            return False
        rows = connection.execute(
            """SELECT id,preprocessing_job_id,source_snapshot,result_dir,total_files,
                      completed_files,failed_files
               FROM postprocessing_jobs
               WHERE participant_code=? AND status='completed'
               ORDER BY created_at DESC""",
            (participant_code,),
        ).fetchall()
    current = {item["sentence_id"]: (item["clean_video"], item["source_size"], item["source_mtime_ns"])
               for item in sources}
    for row in rows:
        if row["preprocessing_job_id"] != preprocessing_job_id or row["failed_files"]:
            continue
        if row["total_files"] != len(current) or row["completed_files"] != len(current):
            continue
        snapshot = json.loads(row["source_snapshot"])
        recorded = {item["sentence_id"]: (item["clean_video"], item["source_size"], item["source_mtime_ns"])
                    for item in snapshot}
        manifest = Path(row["result_dir"]).parent / "manifest.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if recorded == current and payload.get("job_id") == row["id"]:
            return True
    return False
