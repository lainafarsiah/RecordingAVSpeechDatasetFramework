import io
import json
import zipfile
import asyncio
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import httpx
import app


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(app, 'DB_PATH', root / 'app.db'),
                        patch.object(app, 'RECORDINGS_DIR', root / 'recordings'),
                        patch.object(app, 'BACKUPS_DIR', root / 'backups'),
                        patch.dict(os.environ, {'MEDIA_LOCAL_WORKERS': '0'})]
        for p in self.patches:
            p.start()
        # Periodic wakeup also makes these tests work in restricted socket sandboxes.
        async def heartbeat():
            while True:
                await asyncio.sleep(0.01)
        self.heartbeat = asyncio.create_task(heartbeat())
        self.lifespan = app.lifespan(app.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='https://test')
        with app.get_db() as conn:
            conn.execute("INSERT INTO participant_credentials(participant_code,password,created_at) VALUES ('P0001','test','today')")
        response = await self.client.post('/participant/login', data={'participant_code':'P0001','password':'test'})
        self.assertEqual(response.status_code, 303)
        response = await self.client.post('/api/participant/register', data={
            'age_group':'18-25', 'gender':'female', 'indonesian_accent':'baku_netral',
            'public_speaking_ability':'A','communication_style':'B'})
        self.assertEqual(response.status_code, 200)
        self.sid = app.load_sentences()[0]['sentence_id']
        self.clip = root / 'sample.webm'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=320x240:r=15',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','0.5',
                        '-c:v','libvpx','-threads','1','-c:a','libopus',str(self.clip)], check=True)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None,None,None)
        self.heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await self.heartbeat
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def upload(self, content=None, sentence_id=None):
        return await self.client.post('/api/recordings/upload',
            data={'participant_code':'P0001','sentence_id':sentence_id or self.sid},
            files={'file':('clip.webm',self.clip.read_bytes() if content is None else content,'video/webm')})

    async def test_bulk_retake_is_atomic_for_selected_recordings(self):
        second_sid = app.load_sentences()[1]['sentence_id']
        first = await self.upload()
        second = await self.upload(sentence_id=second_sid)
        await asyncio.to_thread(app.media_queue.run_once)
        await asyncio.to_thread(app.media_queue.run_once)
        self.assertEqual((await self.client.get(first.json()['status_url'])).json()['status'], 'complete')
        self.assertEqual((await self.client.get(second.json()['status_url'])).json()['status'], 'complete')

        url = '/api/admin/recordings/bulk-retake'
        payload = {'participant_code': 'P0001', 'sentence_ids': [self.sid, second_sid]}
        self.assertEqual((await self.client.post(url, json=payload)).status_code, 401)
        await self.client.post(f'{app.ADMIN_BASE_PATH}/login', data={'password': app.ADMIN_PASSWORD})
        await self.client.post('/api/admin/participant/P0001/validation', data={'validated': 'true'})
        with app.get_db() as conn:
            paths = [
                Path(row[0])
                for row in conn.execute(
                    'SELECT original_mp4_path FROM recordings WHERE participant_code=? ORDER BY sentence_id',
                    ('P0001',),
                )
            ]

        invalid = await self.client.post(url, json={
            'participant_code': 'P0001',
            'sentence_ids': [self.sid, 'MISSING'],
        })
        self.assertEqual(invalid.status_code, 404)
        self.assertTrue(all(path.is_file() for path in paths))

        response = await self.client.post(url, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['retake_count'], 2)
        self.assertTrue(all(not path.exists() for path in paths))
        with app.get_db() as conn:
            rows = conn.execute(
                'SELECT quality_status, duration_seconds FROM recordings WHERE participant_code=?',
                ('P0001',),
            ).fetchall()
            self.assertTrue(all(row['quality_status'] == 'retake_requested' for row in rows))
            self.assertTrue(all(row['duration_seconds'] == 0 for row in rows))
        summary = (await self.client.get('/api/admin/summary')).json()
        self.assertFalse(summary['participants'][0]['admin_validated'])

    async def test_full_retake_backup_and_reset(self):
        url = '/api/admin/participant/P0001/full-retake'
        self.assertEqual((await self.client.post(url)).status_code, 401)
        await self.client.post(f'{app.ADMIN_BASE_PATH}/login', data={'password':app.ADMIN_PASSWORD})
        self.assertEqual((await self.client.post(url)).status_code, 400)
        uploaded = await self.upload()
        self.assertEqual((await self.client.post(url)).status_code, 409)
        await asyncio.to_thread(app.media_queue.run_once)
        result = (await self.client.get(uploaded.json()['status_url'])).json()['result']
        await self.client.post('/api/admin/participant/P0001/validation', data={'validated':'true'})
        response = await self.client.post(url)
        self.assertEqual(response.status_code, 200, response.text)
        backup = response.json()['backup']
        archive_response = await self.client.get(backup['download_url'])
        self.assertEqual(archive_response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            self.assertIsNone(archive.testzip())
            metadata = json.loads(archive.read('metadata.json'))
            self.assertTrue(metadata['participant']['admin_validated'])
            for path in result['files'].values():
                name = 'recordings/P0001/' + str(Path(path).relative_to(app.RECORDINGS_DIR / 'P0001'))
                self.assertEqual(archive.read(name), Path(path).read_bytes())
        session = (await self.client.get('/api/session/P0001')).json()
        self.assertEqual(session['completed'], 0)
        self.assertEqual(session['remaining'], len(app.load_sentences()))
        self.assertFalse((await self.client.get('/api/admin/summary')).json()['participants'][0]['admin_validated'])
        self.assertEqual((await self.client.post(url)).json()['backup']['id'], backup['id'])
        await self.client.get(f'{app.ADMIN_BASE_PATH}/logout')
        self.assertEqual((await self.client.get(backup['download_url'])).status_code, 401)
        await self.client.post('/participant/login', data={'participant_code':'P0001','password':'test'})
        self.assertTrue((await self.client.get('/api/participant/me')).json()['profile_complete'])

    async def test_full_retake_backup_failure_keeps_progress(self):
        await self.upload()
        await asyncio.to_thread(app.media_queue.run_once)
        await self.client.post(f'{app.ADMIN_BASE_PATH}/login', data={'password':app.ADMIN_PASSWORD})
        with patch.object(app.zipfile.ZipFile, 'testzip', return_value='damaged-file'):
            response = await self.client.post('/api/admin/participant/P0001/full-retake')
        self.assertEqual(response.status_code, 500)
        self.assertEqual((await self.client.get('/api/session/P0001')).json()['completed'], 1)
        with app.get_db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM participant_backups').fetchone()[0], 0)
        self.assertEqual(list(app.BACKUPS_DIR.iterdir()), [])

    async def test_admin_validation_persists_and_requires_admin(self):
        url = '/api/admin/participant/P0001/validation'
        self.assertEqual((await self.client.post(url, data={'validated':'true'})).status_code, 401)
        await self.client.post(f'{app.ADMIN_BASE_PATH}/login', data={'password':app.ADMIN_PASSWORD})
        summary = (await self.client.get('/api/admin/summary')).json()
        self.assertFalse(summary['participants'][0]['admin_validated'])
        response = await self.client.post(url, data={'validated':'true'})
        self.assertEqual(response.status_code, 200)
        app.init_db()
        summary = (await self.client.get('/api/admin/summary')).json()
        self.assertTrue(summary['participants'][0]['admin_validated'])
        response = await self.client.post(url, data={'validated':'false'})
        self.assertFalse(response.json()['admin_validated'])
        self.assertEqual((await self.client.post('/api/admin/participant/MISSING/validation', data={'validated':'true'})).status_code, 404)
        self.assertEqual((await self.client.post(url, data={'validated':'invalid'})).status_code, 422)

    async def test_real_conversion_resume_duplicate_and_retake(self):
        response = await self.upload()
        self.assertEqual(response.status_code,202,response.text)
        job_id = response.json()['job_id']
        duplicate = await self.upload()
        self.assertEqual(duplicate.json()['job_id'],job_id)
        self.assertEqual(len(list((app.RECORDINGS_DIR / '.uploads').iterdir())),1)
        session = (await self.client.get('/api/session/P0001')).json()
        self.assertEqual(session['completed'],0)
        self.assertEqual(session['pending_jobs'],[job_id])
        await asyncio.to_thread(app.media_queue.run_once)
        result = (await self.client.get('/api/recordings/jobs/'+job_id)).json()
        self.assertEqual(result['status'],'complete',result)
        original = result['result']
        self.assertTrue(result['result']['base_filename'].endswith('_XA_YB'))
        for filename in result['result']['files'].values():
            self.assertGreater(Path(filename).stat().st_size,0)
        self.assertEqual((await self.client.get('/api/session/P0001')).json()['completed'],1)
        self.assertEqual(list((app.RECORDINGS_DIR / '.uploads').iterdir()),[])
        video_url = f'/api/admin/recording/video/P0001/{self.sid}'
        # Ownership checks apply to job status, even with a valid different login.
        self.assertIsNone(app.media_queue.get(job_id,'OTHER'))
        await self.client.post(f'{app.ADMIN_BASE_PATH}/login',data={'password':app.ADMIN_PASSWORD})
        old_video = await self.client.get(video_url)
        self.assertEqual(old_video.status_code, 200)
        self.assertIn('no-store', old_video.headers['cache-control'])
        response = await self.client.post('/api/admin/recording/quality',data={
            'participant_code':'P0001','sentence_id':self.sid,'quality_status':'retake_requested'})
        self.assertEqual(response.status_code,200,response.text)
        replacement = self.clip.with_name('replacement.webm')
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=320x240:r=15',
                        '-f','lavfi','-i','sine=frequency=880:sample_rate=48000','-t','0.5',
                        '-c:v','libvpx','-threads','1','-c:a','libopus',str(replacement)], check=True)
        response = await self.upload(replacement.read_bytes())
        await asyncio.to_thread(app.media_queue.run_once)
        retake = (await self.client.get(response.json()['status_url'])).json()
        self.assertEqual(retake['status'],'complete')
        self.assertNotEqual(original['files']['original_mp4'], retake['result']['files']['original_mp4'])
        for filename in retake['result']['files'].values():
            self.assertTrue(Path(filename).exists())
        new_video = await self.client.get(video_url)
        self.assertEqual(new_video.status_code, 200)
        self.assertNotEqual(old_video.content, new_video.content)
        detail = (await self.client.get('/api/admin/participant/P0001')).json()
        self.assertGreater(detail['recordings'][0]['recording_version'], 0)
        self.assertEqual((await self.client.get('/api/admin/summary')).status_code,200)

    async def test_requests_respond_while_worker_is_busy(self):
        entered = threading.Event()
        release = threading.Event()
        original = app.media_queue.process
        def slow(job):
            entered.set()
            release.wait(timeout=10)
            return original(job)
        app.media_queue.process = slow
        await self.upload()
        processing = asyncio.create_task(asyncio.to_thread(app.media_queue.run_once))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,3))
            responses = await asyncio.wait_for(asyncio.gather(*[
                self.client.get('/api/participant/me') for _ in range(30)]),timeout=5)
            self.assertTrue(all(r.status_code==200 for r in responses))
        finally:
            release.set()
            await processing

    async def test_limits_failures_and_failed_upload_preserves_previous_recording(self):
        self.assertEqual((await self.upload(b'')).status_code,400)
        with patch.object(app,'MAX_UPLOAD_BYTES',10):
            self.assertEqual((await self.upload()).status_code,413)
        response = await self.upload()
        await asyncio.to_thread(app.media_queue.run_once)
        original = (await self.client.get(response.json()['status_url'])).json()['result']
        invalid = await self.upload(b'not a valid video')
        with self.assertLogs('media_jobs',level='ERROR'):
            await asyncio.to_thread(app.media_queue.run_once)
        self.assertEqual((await self.client.get(invalid.json()['status_url'])).json()['status'],'failed')
        self.assertEqual((await self.client.get('/api/session/P0001')).json()['completed'],1)
        self.assertTrue(all(Path(path).exists() for path in original['files'].values()))
        with patch.object(app.media_queue,'max_pending',0):
            self.assertEqual((await self.upload()).status_code,503)


if __name__ == '__main__':
    unittest.main()
