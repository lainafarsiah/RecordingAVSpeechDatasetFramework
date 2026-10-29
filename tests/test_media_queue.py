import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from media_jobs import MediaQueue, QueueFull


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'jobs.db'
        self.queue = MediaQueue(self.db, lambda job: {'ok': True}, max_active=2, max_pending=8)
        self.queue.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_duplicate_uploads_share_one_job(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.queue.enqueue('P1', 'S1', {}), range(16)))
        self.assertEqual(len({job_id for job_id, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)

    def test_global_limit_across_queue_instances_and_recovery(self):
        for number in range(8):
            self.queue.enqueue(f'P{number}', 'S1', {})
        queues = [MediaQueue(self.db, lambda job: {}, max_active=2) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [job for job in pool.map(lambda q: q.claim(), queues) if job]
        self.assertEqual(len(jobs), 2)
        with self.queue.connect() as conn:
            conn.execute('UPDATE media_jobs SET lease_until=0 WHERE id=?', (jobs[0]['id'],))
        recovered = queues[0].claim()
        self.assertEqual(recovered['id'], jobs[0]['id'])
        self.assertNotEqual(recovered['token'], jobs[0]['token'])

    def test_capacity_persistence_and_ownership(self):
        for number in range(8):
            self.queue.enqueue(f'P{number}', 'S1', {})
        with self.assertRaises(QueueFull):
            self.queue.enqueue('EXCESS', 'S1', {})
        reopened = MediaQueue(self.db, lambda job: {'ok': True}, max_active=2)
        job_id = reopened.pending('P0')[0]
        self.assertIsNone(reopened.get(job_id, 'OTHER'))
        reopened.run_once()
        self.assertEqual(reopened.get(job_id, 'P0')['status'], 'complete')

    def test_background_worker_processes_persisted_job(self):
        completed = threading.Event()
        def process(job):
            completed.set()
            return {"ok": True}
        self.queue.process = process
        job_id, _ = self.queue.enqueue('P1', 'S1', {})
        self.queue.start()
        try:
            self.assertTrue(completed.wait(timeout=5))
        finally:
            self.queue.shutdown()
        self.assertEqual(self.queue.get(job_id, 'P1')['status'], 'complete')

    def test_processing_failure_is_visible_and_can_be_retried(self):
        def fail(job):
            raise RuntimeError('bad input')
        self.queue.process = fail
        job_id, _ = self.queue.enqueue('P1', 'S1', {})
        with self.assertLogs('media_jobs', level='ERROR'):
            self.queue.run_once()
        self.assertEqual(self.queue.get(job_id, 'P1')['status'], 'failed')
        replacement, created = self.queue.enqueue('P1', 'S1', {})
        self.assertTrue(created)
        self.assertNotEqual(job_id, replacement)


if __name__ == '__main__':
    unittest.main()
