#!/usr/bin/env python3
"""Local Whisper ASR shim for the Qwen Code Telegram channel.

The Qwen voice bridge transcribes inbound voice messages by calling a model
named ``qwen3-asr-flash`` on an OpenAI-compatible ``/chat/completions``
endpoint, sending the audio as an ``input_audio`` content part. This shim
implements that exact contract locally with mlx-whisper, so voice messages
are transcribed offline for free instead of hitting a paid cloud ASR model.

Run with the dedicated venv (see scripts/run_asr_shim_launchd.sh):

    ~/.local/share/qwen-asr/venv/bin/python scripts/asr_shim.py

Configuration (environment variables, all optional):
    ASR_HOST          bind address (default 127.0.0.1 — local only)
    ASR_PORT          listen port (default 8790)
    ASR_MODEL         mlx-whisper HF repo (default whisper-large-v3-turbo)
    ASR_LANGUAGE      default language when the request omits one ("" = auto)
    ASR_KEYTERMS_FILE path to a keyterms file used to bias transcription
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import mlx_whisper

HOST = os.environ.get("ASR_HOST", "127.0.0.1")
PORT = int(os.environ.get("ASR_PORT", "8790"))
MODEL = os.environ.get("ASR_MODEL", "mlx-community/whisper-large-v3-turbo")
DEFAULT_LANGUAGE = os.environ.get("ASR_LANGUAGE", "")
KEYTERMS_FILE = os.environ.get(
    "ASR_KEYTERMS_FILE",
    os.path.expanduser("~/Desktop/radar/.qwen/voice-keyterms.txt"),
)
MAX_INITIAL_PROMPT_TERMS = 60

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s asr-shim: %(message)s"
)
log = logging.getLogger("asr-shim")

# mlx-whisper runs inference on a single resident model; serialize calls so
# concurrent requests cannot corrupt the generation state. Voice messages
# arrive one at a time in practice, so this rarely blocks.
_INFER_LOCK = threading.Lock()

_MIME_EXT = {
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "application/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
    "audio/amr": ".amr",
    "audio/x-amr": ".amr",
}

# Map spoken-language names (as configured via general.voice.language) and
# common codes to Whisper ISO 639-1 codes. Unknown values fall back to
# auto-detection.
_LANGUAGE_MAP = {
    "russian": "ru",
    "русский": "ru",
    "ru": "ru",
    "english": "en",
    "en": "en",
    "chinese": "zh",
    "zh": "zh",
    "german": "de",
    "de": "de",
    "french": "fr",
    "fr": "fr",
    "spanish": "es",
    "es": "es",
    "ukrainian": "uk",
    "uk": "uk",
}


def decode_audio(data_url: str, format_hint: str) -> tuple[bytes, str]:
    """Decode a base64 data URL into raw bytes plus a file extension."""
    mime = ""
    b64 = data_url
    match = re.match(r"^data:([^;]+);base64,(.*)$", data_url, re.DOTALL)
    if match:
        mime = match.group(1).strip().lower()
        b64 = match.group(2)
    raw = base64.b64decode(b64)
    ext = _MIME_EXT.get(mime)
    if not ext:
        hint = (format_hint or "ogg").strip().lstrip(".")
        ext = "." + (hint or "ogg")
    return raw, ext


def resolve_language(asr_options: dict) -> Optional[str]:
    raw = str((asr_options or {}).get("language") or DEFAULT_LANGUAGE).strip().lower()
    if not raw:
        return None
    if raw in _LANGUAGE_MAP:
        return _LANGUAGE_MAP[raw]
    return raw if len(raw) <= 3 else None


def load_initial_prompt() -> str:
    """Build a Whisper initial_prompt from the keyterms file (domain bias)."""
    try:
        with open(KEYTERMS_FILE, encoding="utf-8") as handle:
            terms = [
                line.strip()
                for line in handle
                if line.strip() and not line.strip().startswith("#")
            ]
    except OSError:
        return ""
    return ", ".join(terms[:MAX_INITIAL_PROMPT_TERMS])


def transcribe(path: str, language: Optional[str], initial_prompt: str) -> str:
    with _INFER_LOCK:
        result = mlx_whisper.transcribe(
            path,
            path_or_hf_repo=MODEL,
            language=language,
            initial_prompt=initial_prompt or None,
            fp16=True,
            verbose=False,
        )
    return str(result.get("text") or "").strip()


def warmup() -> None:
    """Load and compile the model at startup so the first request is fast."""
    import numpy as np

    sample_rate = 16000
    frames = np.zeros(int(sample_rate * 0.3), dtype=np.int16)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        path = handle.name
    try:
        with wave.open(path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(frames.tobytes())
        transcribe(path, "en", "")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "ASRShim/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") in ("/health", "/healthz", ""):
            self._send_json(200, {"status": "ok", "model": MODEL})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(
                404, {"error": {"message": "not found", "type": "invalid_request_error"}}
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            transcript = self._handle(body)
        except Exception as exc:  # fail closed: report, never crash the server
            log.exception("transcription failed")
            self._send_json(
                500,
                {
                    "error": {
                        "message": f"transcription failed: {exc}",
                        "type": "server_error",
                    }
                },
            )
            return
        self._send_json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": transcript},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            },
        )

    def _handle(self, body: dict) -> str:
        audio_data = ""
        format_hint = ""
        for message in body.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "input_audio":
                    input_audio = part.get("input_audio") or {}
                    audio_data = input_audio.get("data") or ""
                    format_hint = input_audio.get("format") or ""
        if not audio_data:
            raise ValueError("no input_audio part in request")

        raw, ext = decode_audio(audio_data, format_hint)
        language = resolve_language(body.get("asr_options") or {})
        initial_prompt = load_initial_prompt()

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as handle:
            handle.write(raw)
            path = handle.name
        try:
            started = time.time()
            text = transcribe(path, language, initial_prompt)
            log.info(
                "transcribed %d bytes (%s) lang=%s in %.1fs -> %d chars",
                len(raw),
                ext,
                language or "auto",
                time.time() - started,
                len(text),
            )
            return text
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    log.info("loading model %s (first start may take a while)...", MODEL)
    started = time.time()
    warmup()
    log.info("model ready in %.1fs", time.time() - started)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("listening on http://%s:%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
