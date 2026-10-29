"""Persistent, bounded media processing queue for a single-host deployment."""
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import closing

logger = logging.getLogger(__name__)


class QueueFull(Exception):
    pass


class MediaQueue:
    def __init__(self, db_path, process, max_active=4, max_pending=1000,
                 lease_seconds=900, local_workers=2):
        self.db_path = db_path
        self.process = process
        self.max_active = max_active
        self.max_pending = max_pending
        self.lease_seconds = lease_seconds
        self.local_workers = local_workers
        self.stop = threading.Event()
        self.threads = []

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        with closing(self.connect()) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS media_jobs (
                id TEXT PRIMARY KEY,
                participant_code TEXT NOT NULL,
                sentence_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT,
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                lease_until REAL NOT NULL DEFAULT 0,
                token TEXT NOT NULL DEFAULT ''
            )''')
            conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS media_jobs_active
                ON media_jobs(participant_code, sentence_id)
                WHERE status IN ('queued', 'processing')''')
            conn.execute('''CREATE INDEX IF NOT EXISTS media_jobs_waiting
                ON media_jobs(status, created_at)''')

    def enqueue(self, participant_code, sentence_id, payload):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('''SELECT id FROM media_jobs
                WHERE participant_code=? AND sentence_id=?
                AND status IN ('queued', 'processing')''',
                (participant_code, sentence_id)).fetchone()
            if existing:
                return existing['id'], False
            pending = conn.execute("SELECT COUNT(*) FROM media_jobs WHERE status IN ('queued', 'processing')").fetchone()[0]
            if pending >= self.max_pending:
                raise QueueFull()
            job_id = uuid.uuid4().hex
            conn.execute('''INSERT INTO media_jobs
                (id, participant_code, sentence_id, status, payload, created_at)
                VALUES (?, ?, ?, 'queued', ?, ?)''',
                (job_id, participant_code, sentence_id, json.dumps(payload), time.time()))
            return job_id, True

    def get(self, job_id, participant_code):
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT * FROM media_jobs WHERE id=? AND participant_code=?',
                               (job_id, participant_code)).fetchone()
        if row is None:
            return None
        return {'job_id': row['id'], 'status': row['status'], 'error': row['error'],
                'result': json.loads(row['result']) if row['result'] else None}

    def pending(self, participant_code):
        with closing(self.connect()) as conn:
            return [row['id'] for row in conn.execute('''SELECT id FROM media_jobs
                WHERE participant_code=? AND status IN ('queued', 'processing')
                ORDER BY created_at''', (participant_code,))]

    def claim(self):
        now = time.time()
        with closing(self.connect()) as conn, conn:
            # Avoid acquiring a write lock when there is no work to do.
            if not conn.execute('''SELECT 1 FROM media_jobs WHERE status='queued'
                OR (status='processing' AND lease_until < ?) LIMIT 1''', (now,)).fetchone():
                return None
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE media_jobs SET status='queued', token='' WHERE status='processing' AND lease_until < ?", (now,))
            if conn.execute("SELECT COUNT(*) FROM media_jobs WHERE status='processing'").fetchone()[0] >= self.max_active:
                return None
            row = conn.execute("SELECT * FROM media_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            conn.execute("UPDATE media_jobs SET status='processing', token=?, lease_until=? WHERE id=?",
                         (token, now + self.lease_seconds, row['id']))
            return dict(row, token=token, payload=json.loads(row['payload']))

    def run_once(self):
        job = self.claim()
        if not job:
            return False
        try:
            result = self.process(job)
            with closing(self.connect()) as conn, conn:
                conn.execute("UPDATE media_jobs SET status='complete', result=?, error='' WHERE id=? AND token=? AND status='processing'",
                             (json.dumps(result), job['id'], job['token']))
        except Exception:
            logger.exception('Media job %s failed', job['id'])
            with closing(self.connect()) as conn, conn:
                conn.execute("UPDATE media_jobs SET status='failed', error=? WHERE id=? AND token=? AND status='processing'",
                             ('Processing failed. Please retry Save & Continue or record again.', job['id'], job['token']))
        return True

    def loop(self):
        while not self.stop.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                logger.exception('Media queue worker failed; retrying')
            self.stop.wait(2)

    def start(self):
        for number in range(self.local_workers):
            thread = threading.Thread(target=self.loop, name=f'media-worker-{number}', daemon=True)
            thread.start()
            self.threads.append(thread)

    def shutdown(self):
        self.stop.set()
        # Jobs interrupted by process termination are recovered after their lease.
        for thread in self.threads:
            thread.join(timeout=1)
