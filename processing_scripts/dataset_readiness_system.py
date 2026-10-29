"""State and source selection for multimodal AVSE dataset exports."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


def connect(db_path):
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize(db_path):
    with closing(connect(db_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS dataset_readiness_jobs (
                id TEXT PRIMARY KEY, participant_code TEXT NOT NULL,
                postprocessing_job_id TEXT NOT NULL, export_mode TEXT NOT NULL,
                status TEXT NOT NULL, total_files INTEGER NOT NULL,
                completed_files INTEGER NOT NULL DEFAULT 0,
                failed_files INTEGER NOT NULL DEFAULT 0,
                current_sentence TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', source_snapshot TEXT NOT NULL,
                result_dir TEXT NOT NULL, created_at REAL NOT NULL, finished_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS dataset_readiness_active_participant
                ON dataset_readiness_jobs(participant_code) WHERE status='running';
            CREATE INDEX IF NOT EXISTS dataset_readiness_participant_created
                ON dataset_readiness_jobs(participant_code, created_at DESC);
            CREATE TABLE IF NOT EXISTS dataset_readiness_results (
                job_id TEXT NOT NULL, participant_code TEXT NOT NULL,
                sentence_id TEXT NOT NULL, canonical_basename TEXT NOT NULL,
                status TEXT NOT NULL, video_path TEXT NOT NULL DEFAULT '',
                audio_path TEXT NOT NULL DEFAULT '', text_path TEXT NOT NULL DEFAULT '',
                face_path TEXT NOT NULL DEFAULT '', mouth_path TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                exported_at REAL, PRIMARY KEY(job_id, sentence_id)
            );
            """
        )


def ready_sources(connection, participant_code):
    job = connection.execute(
        """SELECT id FROM postprocessing_jobs
           WHERE participant_code=? AND status='completed' AND failed_files=0
           ORDER BY created_at DESC LIMIT 1""", (participant_code,),
    ).fetchone()
    if job is None:
        raise ValueError("No completed post-processing run is available for this participant.")
    rows = connection.execute(
        """SELECT pr.sentence_id,pr.normalized_video,pr.face_video,pr.mouth_video,
                  pr.detection_json,pr.loudness_json,pr.timing_json,r.sentence_text
           FROM postprocessing_results pr
           JOIN recordings r ON r.participant_code=pr.participant_code
                            AND r.sentence_id=pr.sentence_id
                            AND r.quality_status!='retake_requested'
           WHERE pr.job_id=? AND pr.status='complete'
           ORDER BY pr.sentence_id""", (job["id"],),
    ).fetchall()
    sources = []
    for row in rows:
        item = dict(row)
        fingerprints = {}
        for key in ("normalized_video", "face_video", "mouth_video"):
            path = Path(item[key])
            if not path.is_file():
                raise FileNotFoundError(f"{key} is missing for {item['sentence_id']}.")
            stat = path.stat()
            item[key] = str(path.resolve())
            fingerprints[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        item["fingerprints"] = fingerprints
        sources.append(item)
    if not sources:
        raise ValueError("The completed post-processing run has no usable outputs.")
    return job["id"], sources


def start_run(db_path, output_root: Path, participant_code: str, export_mode: str):
    now = time.time()
    with closing(connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM dataset_readiness_jobs WHERE participant_code=? AND status='running'",
            (participant_code,),
        ).fetchone():
            raise RuntimeError(f"{participant_code} is already being exported.")
        postprocessing_job_id, sources = ready_sources(connection, participant_code)
        job_id = uuid.uuid4().hex
        result_dir = output_root / participant_code
        connection.execute(
            """INSERT INTO dataset_readiness_jobs
               (id,participant_code,postprocessing_job_id,export_mode,status,total_files,
                source_snapshot,result_dir,created_at,stage)
               VALUES (?,?,?,?,'running',?,?,?,?,?)""",
            (job_id, participant_code, postprocessing_job_id, export_mode, len(sources),
             json.dumps(sources), str(result_dir), now, "Starting"),
        )
    return {"id": job_id, "participant_code": participant_code,
            "postprocessing_job_id": postprocessing_job_id, "export_mode": export_mode,
            "total_files": len(sources), "source_snapshot": sources,
            "result_dir": str(result_dir)}


def recover_interrupted_run(db_path, participant_code: str) -> int:
    """Fail stale runs after the caller has acquired the participant file lock."""
    now = time.time()
    with closing(connect(db_path)) as connection, connection:
        cursor = connection.execute(
            """UPDATE dataset_readiness_jobs
               SET status='failed', stage='Interrupted',
                   error='Previous export process ended before completion.',
                   finished_at=?
               WHERE participant_code=? AND status='running'""",
            (now, participant_code),
        )
        return cursor.rowcount


def update_job(db_path, job_id, **values):
    allowed = {"status", "completed_files", "failed_files", "current_sentence",
               "stage", "error", "finished_at"}
    values = {key: value for key, value in values.items() if key in allowed}
    if not values: return
    assignments = ",".join(f"{key}=?" for key in values)
    with closing(connect(db_path)) as connection, connection:
        connection.execute(f"UPDATE dataset_readiness_jobs SET {assignments} WHERE id=?",
                           (*values.values(), job_id))


def save_result(db_path, job, item, result):
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            """INSERT OR REPLACE INTO dataset_readiness_results
               (job_id,participant_code,sentence_id,canonical_basename,status,
                video_path,audio_path,text_path,face_path,mouth_path,metadata_json,error,exported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["id"], job["participant_code"], item["sentence_id"],
             result.get("canonical_basename", ""), result["status"],
             result.get("video_path", ""), result.get("audio_path", ""),
             result.get("text_path", ""), result.get("face_path", ""),
             result.get("mouth_path", ""), json.dumps(result.get("metadata", {})),
             result.get("error", ""), time.time()),
        )


def current_export_exists(db_path, output_root: Path, participant_code: str, export_mode: str):
    with closing(connect(db_path)) as connection:
        try: post_job_id, sources = ready_sources(connection, participant_code)
        except (ValueError, FileNotFoundError): return False
        rows = connection.execute(
            """SELECT id,postprocessing_job_id,export_mode,total_files,completed_files,
                      failed_files,source_snapshot,result_dir
               FROM dataset_readiness_jobs
               WHERE participant_code=? AND status='completed' ORDER BY created_at DESC""",
            (participant_code,),
        ).fetchall()
    for row in rows:
        if row["postprocessing_job_id"] != post_job_id or row["export_mode"] != export_mode:
            continue
        if row["failed_files"] or row["total_files"] != len(sources) or row["completed_files"] != len(sources):
            continue
        manifest = Path(row["result_dir"]) / "manifest.json"
        try: payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): continue
        if payload.get("job_id") != row["id"] or payload.get("postprocessing_job_id") != post_job_id:
            continue
        records = payload.get("records", [])
        if len(records) != len(sources):
            continue
        paths_are_complete = all(
            record.get("status") == "complete" and all(
                Path(record.get(f"{modality}_path", "")).is_file()
                for modality in ("video", "audio", "text", "face", "mouth")
            ) for record in records
        )
        if paths_are_complete:
            return True
    return False
