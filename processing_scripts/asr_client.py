"""Small standard-library client compatible with faster-whisper's transcribe API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace


class RemoteWhisperModel:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: int | None = None):
        self.url = (url or os.getenv("ASR_API_URL", "")).rstrip("/")
        self.token = token if token is not None else os.getenv("ASR_API_TOKEN", "")
        self.timeout = timeout or int(os.getenv("ASR_API_TIMEOUT", "600"))
        if not self.url:
            raise RuntimeError("ASR_API_URL is not configured.")

    def transcribe(self, audio_path, **options):
        payload = {
            "audio_path": str(audio_path),
            "language": options.get("language", "id"),
            "task": options.get("task", "transcribe"),
            "beam_size": options.get("beam_size", 5),
            "word_timestamps": bool(options.get("word_timestamps", False)),
            "vad_filter": bool(options.get("vad_filter", False)),
            "condition_on_previous_text": bool(options.get("condition_on_previous_text", True)),
        }
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.url}/transcribe",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8")).get("detail", str(error))
            except Exception:
                detail = str(error)
            raise RuntimeError(f"ASR API rejected the request: {detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(f"ASR API is unavailable at {self.url}: {error}") from error
        segments = [SimpleNamespace(**segment) for segment in result.get("segments", [])]
        info = SimpleNamespace(**result.get("info", {}))
        return iter(segments), info
