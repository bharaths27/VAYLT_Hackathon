"""Audio for partial transcripts: a bounded PCM buffer and an optional local whisper.cpp client.

PcmWindow keeps every sample of the current utterance, in order, up to a maximum duration.
It never drops the start to make room: the first word may be "don't". When the window is
full, append() raises AudioOverflow and the window refuses all further audio until reset(),
so the caller must end the utterance instead of transcribing a clipped one. snapshot()
returns a 16 kHz mono 16-bit WAV of everything so far, ready for a partial transcript.

WhisperServerClient talks to a resident whisper.cpp server (its HTTP server example,
POST /inference) on this machine only: the URL must be a loopback IP literal, no proxy is
used, redirects are not followed, and timeout_s bounds the whole request.
"""

from __future__ import annotations

import io
import ipaddress
import json
import threading
import urllib.parse
import uuid
import wave
from typing import Callable

from . import _http

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2          # bytes per sample: signed 16-bit little endian
CHANNELS = 1
DEFAULT_MAX_S = 20.0
MAX_ALLOWED_S = 120.0


class AudioOverflow(Exception):
    """The utterance is longer than the window. Nothing was dropped; end the utterance."""


class OutOfOrder(ValueError):
    """A chunk arrived with the wrong sequence number."""


def wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


class PcmWindow:
    """Ordered, bounded PCM for one utterance. Safe to append from one thread and snapshot from another."""

    def __init__(self, max_seconds: float = DEFAULT_MAX_S):
        if not 0.0 < max_seconds <= MAX_ALLOWED_S:
            raise ValueError(f"max_seconds must be in (0, {MAX_ALLOWED_S}]")
        self.max_bytes = int(max_seconds * SAMPLE_RATE) * SAMPLE_WIDTH
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Start a new utterance."""
        with self._lock:
            self._chunks: list[bytes] = []
            self._size = 0
            self._next_seq = 0
            self._full = False

    def append(self, pcm: bytes, seq: int | None = None) -> None:
        """Add the next chunk. When seq is given it must be 0, 1, 2, ... in order.

        An empty chunk is checked and uses up its sequence number like any other, but stores
        nothing, so a stream of empty keep-alive chunks cannot grow the window past its bound.
        """
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise TypeError("pcm must be bytes")
        pcm = bytes(pcm)
        if len(pcm) % SAMPLE_WIDTH:
            raise ValueError("a chunk must hold whole 16-bit samples")
        with self._lock:
            if self._full:
                raise AudioOverflow("the window is full; end the utterance and reset()")
            if seq is not None and seq != self._next_seq:
                raise OutOfOrder(f"expected chunk {self._next_seq}, got {seq}")
            if self._size + len(pcm) > self.max_bytes:
                # Refuse from here on: accepting a later, smaller chunk would leave a hole mid-utterance.
                self._full = True
                raise AudioOverflow(f"utterance longer than {self.max_bytes // (SAMPLE_RATE * SAMPLE_WIDTH)} s")
            if pcm:
                self._chunks.append(pcm)
                self._size += len(pcm)
            self._next_seq += 1

    @property
    def duration_s(self) -> float:
        with self._lock:
            return self._size / (SAMPLE_RATE * SAMPLE_WIDTH)

    def snapshot(self) -> bytes:
        """A complete WAV file of everything heard so far, from the very first sample."""
        with self._lock:
            pcm = b"".join(self._chunks)
        return wav_bytes(pcm)


class WhisperError(Exception):
    pass


# (url, body, headers, timeout_s) -> (status, reply body). Swapped out in tests.
Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


def _loopback_transport(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    return _http.post(url, body, headers, timeout_s)


class WhisperServerClient:
    """Partial transcripts from a resident whisper.cpp server on this machine."""

    def __init__(self, url: str = "http://127.0.0.1:8080", timeout_s: float = 5.0,
                 transport: Transport | None = None):
        parts = urllib.parse.urlsplit(url)
        try:
            host = ipaddress.ip_address(parts.hostname or "")
        except ValueError:
            raise ValueError("the whisper server URL must use a loopback IP such as 127.0.0.1") from None
        if parts.scheme != "http" or not host.is_loopback or parts.username or parts.password \
                or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("the whisper server URL must be http://<loopback IP>:<port>")
        if not 0.0 < timeout_s <= 30.0:
            raise ValueError("timeout_s must be in (0, 30]")
        self.endpoint = f"http://{parts.netloc}/inference"
        self.timeout_s = timeout_s
        self._transport = transport or _loopback_transport

    def transcribe(self, wav: bytes) -> str:
        boundary = uuid.uuid4().hex
        parts = [
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="partial.wav"\r\n'
             "Content-Type: audio/wav\r\n\r\n").encode() + wav + b"\r\n",
        ]
        for name, value in (("response_format", "json"), ("temperature", "0.0")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        try:
            status, data = self._transport(self.endpoint, body, headers, self.timeout_s)
        except Exception as e:
            raise WhisperError(f"whisper request failed: {type(e).__name__}") from None
        if status != 200:
            raise WhisperError(f"whisper server http {status}")
        try:
            text = json.loads(data).get("text")
        except (ValueError, AttributeError):
            raise WhisperError("whisper reply was not JSON with a text field") from None
        if not isinstance(text, str):
            raise WhisperError("whisper reply was not JSON with a text field")
        return " ".join(text.split())
