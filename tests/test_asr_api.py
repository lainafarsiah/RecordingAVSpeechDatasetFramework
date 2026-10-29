import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from processing_scripts import asr_api
from processing_scripts.asr_client import RemoteWhisperModel


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeModel:
    def transcribe(self, _path, **_options):
        segments = [SimpleNamespace(start=0.0, end=0.5, text=" Ini kalimat uji ")]
        info = SimpleNamespace(language="id", language_probability=0.99, duration=0.5)
        return iter(segments), info


class ASRServiceTests(unittest.TestCase):
    def test_remote_client_matches_faster_whisper_interface(self):
        payload = {
            "segments": [{"start": 0.0, "end": 0.5, "text": "Halo"}],
            "info": {"language": "id", "duration": 0.5},
        }
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload)) as request:
            segments, info = RemoteWhisperModel("http://127.0.0.1:8765", "secret").transcribe(
                "/allowed/audio.wav", language="id", beam_size=5
            )
        self.assertEqual([segment.text for segment in segments], ["Halo"])
        self.assertEqual(info.language, "id")
        self.assertEqual(request.call_args.args[0].headers["Authorization"], "Bearer secret")

    def test_path_validation_and_transcription(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            request = asr_api.TranscriptionRequest(audio_path=str(audio))
            with patch.object(asr_api, "ALLOWED_ROOTS", (root.resolve(),)), \
                 patch.object(asr_api, "model", FakeModel()):
                approved = asr_api.approved_audio_path(str(audio))
                result = asr_api.run_transcription(approved, request)
            self.assertEqual(result["text"], "Ini kalimat uji")
            self.assertEqual(result["info"]["language"], "id")
            with patch.object(asr_api, "ALLOWED_ROOTS", (root.resolve(),)):
                with self.assertRaises(HTTPException):
                    asr_api.approved_audio_path("/etc/passwd")


if __name__ == "__main__":
    unittest.main()
