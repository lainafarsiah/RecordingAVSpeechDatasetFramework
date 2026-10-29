#!/usr/bin/env python3
"""Local persistent faster-whisper service for evaluation and preprocessing."""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parents[1]
MODEL_NAME = os.getenv("ASR_MODEL", os.getenv("WHISPER_MODEL", "large-v3"))
DEVICE = os.getenv("ASR_DEVICE", "cuda")
COMPUTE_TYPE = os.getenv("ASR_COMPUTE_TYPE", "float16")
MAX_CONCURRENT = max(1, min(16, int(os.getenv("ASR_MAX_CONCURRENT", "2"))))
API_TOKEN = os.getenv("ASR_API_TOKEN", "")
ALLOWED_ROOTS = tuple(
    Path(item).expanduser().resolve()
    for item in os.getenv(
        "ASR_ALLOWED_ROOTS",
        os.pathsep.join((str(BASE_DIR / "recordings"), str(BASE_DIR / "recording_clean"))),
    ).split(os.pathsep)
    if item
)

model = None
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
metrics_lock = threading.Lock()
metrics = {"accepted": 0, "completed": 0, "failed": 0, "active": 0, "seconds": 0.0}


class TranscriptionRequest(BaseModel):
    audio_path: str = Field(min_length=1, max_length=4096)
    language: str = Field(default="id", min_length=2, max_length=16)
    task: str = Field(default="transcribe", pattern="^(transcribe|translate)$")
    beam_size: int = Field(default=5, ge=1, le=10)
    word_timestamps: bool = False
    vad_filter: bool = False
    condition_on_previous_text: bool = True


def require_token(authorization: str | None = Header(default=None)):
    if API_TOKEN:
        supplied = ""
        if authorization and authorization.startswith("Bearer "):
            supplied = authorization[7:]
        if not hmac.compare_digest(supplied, API_TOKEN):
            raise HTTPException(status_code=401, detail="Invalid ASR API token.")


def approved_audio_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not any(path.is_relative_to(root) for root in ALLOWED_ROOTS):
        raise HTTPException(status_code=403, detail="Audio path is outside approved directories.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Audio file was not found.")
    return path


def load_model():
    return WhisperModel(
        MODEL_NAME,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        num_workers=MAX_CONCURRENT,
    )


def run_transcription(path: Path, request: TranscriptionRequest) -> dict:
    started = time.monotonic()
    segments, info = model.transcribe(
        str(path),
        language=request.language,
        task=request.task,
        beam_size=request.beam_size,
        word_timestamps=request.word_timestamps,
        vad_filter=request.vad_filter,
        condition_on_previous_text=request.condition_on_previous_text,
    )
    items = [
        {"start": segment.start, "end": segment.end, "text": segment.text}
        for segment in segments
    ]
    return {
        "text": " ".join(item["text"].strip() for item in items).strip(),
        "segments": items,
        "info": {
            "language": getattr(info, "language", request.language),
            "language_probability": getattr(info, "language_probability", None),
            "duration": getattr(info, "duration", None),
        },
        "processing_seconds": round(time.monotonic() - started, 4),
        "model": MODEL_NAME,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
    }


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global model
    model = await asyncio.to_thread(load_model)
    yield
    model = None


app = FastAPI(title="Local ASR Service", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "ready": model is not None,
        "model": MODEL_NAME,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "max_concurrent": MAX_CONCURRENT,
    }


@app.get("/metrics", dependencies=[Depends(require_token)])
def service_metrics():
    with metrics_lock:
        return dict(metrics)


@app.post("/transcribe", dependencies=[Depends(require_token)])
async def transcribe(request: TranscriptionRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="ASR model is still loading.")
    path = approved_audio_path(request.audio_path)
    with metrics_lock:
        metrics["accepted"] += 1
    async with semaphore:
        with metrics_lock:
            metrics["active"] += 1
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(run_transcription, path, request)
            with metrics_lock:
                metrics["completed"] += 1
            return result
        except Exception as error:
            with metrics_lock:
                metrics["failed"] += 1
            raise HTTPException(status_code=500, detail=f"Transcription failed: {error}") from error
        finally:
            with metrics_lock:
                metrics["active"] -= 1
                metrics["seconds"] = round(metrics["seconds"] + time.monotonic() - started, 4)
