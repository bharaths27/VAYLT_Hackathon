"""Audio for partial transcripts: a bounded PCM buffer and an optional local whisper.cpp client.

PcmWindow keeps every sample of the current utterance, in order, up to a maximum duration.
It never drops the start to make room: the first word may be "don't". When the window is
full, append() raises AudioOverflow and the utterance is invalid: the window refuses all
further audio AND every snapshot() until reset(), so nothing can transcribe a clipped
utterance (the words cut off could be "no, don't"); it must be said again. snapshot()
returns a 16 kHz mono 16-bit WAV of everything so far, ready for a partial transcript.
seal() ends the utterance for its final transcript: the audio is captured atomically and
any later append() is refused (WindowSealed); audio refused before the final is decided
(finalize()) makes the utterance invalid too.

WhisperServerClient talks to a resident whisper.cpp server (its HTTP server example,
POST /inference) on this machine only: the URL must be a loopback IP literal, no proxy is
used, redirects are not followed, and timeout_s bounds the whole request.
"""

from __future__ import annotations

import io
import ipaddress
import json
import re
import threading
import urllib.parse
import uuid
import wave
from typing import Callable

from . import _http
from ._http import Cancelled, CancelToken

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2          # bytes per sample: signed 16-bit little endian
CHANNELS = 1
DEFAULT_MAX_S = 20.0
MAX_ALLOWED_S = 120.0


class AudioOverflow(Exception):
    """The utterance is not whole: longer than the window, or audio for it was refused. Never transcribe
    or act on it; it must be said again."""


class WindowSealed(AudioOverflow):
    """Audio arrived after the utterance was sealed for its final transcript; refused. reset() starts the
    next utterance."""


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


class SealedUtterance:
    """The audio of one utterance as it stood when PcmWindow.seal() was called."""

    def __init__(self, audio: bytes, generation: int):
        self.audio = audio
        self.generation = generation
        self._clipped = False   # audio for this utterance was refused after the seal
        self._decided: bool | None = None


class PcmWindow:
    """Ordered, bounded PCM for one utterance. Safe to append from one thread and snapshot from another.

    seal() ends the utterance: under the lock it captures the final audio, and every later append()
    raises WindowSealed. finalize(seal) decides, also under the lock, whether the sealed audio is still
    the whole utterance: False if any audio for it was refused since the seal. So a final transcript is
    either of exactly the whole utterance, or not final; there is no moment where audio can slip in
    between the check and the capture.
    """

    def __init__(self, max_seconds: float = DEFAULT_MAX_S):
        if not 0.0 < max_seconds <= MAX_ALLOWED_S:
            raise ValueError(f"max_seconds must be in (0, {MAX_ALLOWED_S}]")
        self.max_bytes = int(max_seconds * SAMPLE_RATE) * SAMPLE_WIDTH
        self._lock = threading.Lock()
        self._generation = 0
        self.reset()

    def reset(self) -> None:
        """Start a new utterance. A seal of the previous one keeps what it recorded."""
        with self._lock:
            self._chunks: list[bytes] = []
            self._size = 0
            self._next_seq = 0
            self._full = False
            self._seal: SealedUtterance | None = None
            self._generation += 1

    @property
    def generation(self) -> int:
        """Which utterance the window holds; reset() moves it on."""
        with self._lock:
            return self._generation

    def seal(self, generation: int | None = None) -> SealedUtterance:
        """End the utterance and capture its final audio, atomically. Raises AudioOverflow (not final) if
        the utterance overflowed, or if the window has moved on to another utterance than `generation`."""
        with self._lock:
            if generation is not None and generation != self._generation:
                raise AudioOverflow("the window moved on to another utterance before this one was finished")
            if self._full or (self._seal is not None and self._seal._clipped):
                raise AudioOverflow("this utterance is not whole; it must be said again, not transcribed")
            if self._seal is None:
                self._seal = SealedUtterance(wav_bytes(b"".join(self._chunks)), self._generation)
            return self._seal

    def finalize(self, sealed: SealedUtterance) -> bool:
        """Decide, once: is the sealed audio still the whole utterance? False if audio for it was refused
        after the seal. A decision stands; audio arriving later is refused but changes nothing."""
        with self._lock:
            if sealed._decided is None:
                sealed._decided = not sealed._clipped
            return sealed._decided

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
            if self._seal is not None:
                if self._seal._decided is None:
                    self._seal._clipped = True     # the final being made would miss this audio: not final
                raise WindowSealed("this utterance has ended; audio for it is refused (reset() starts the next)")
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

    @property
    def overflowed(self) -> bool:
        """True once audio was refused for overflow: the utterance is invalid until reset()."""
        with self._lock:
            return self._full

    def intact(self, generation: int) -> bool:
        """Is utterance `generation` still the one held, and whole (no overflow, no audio refused)?"""
        with self._lock:
            return (generation == self._generation and not self._full
                    and not (self._seal is not None and self._seal._clipped))

    def snapshot(self) -> bytes:
        """A complete WAV file of everything heard so far, from the very first sample.

        Raises AudioOverflow once the utterance is not whole (it overflowed, or audio for it was
        refused after its seal): what was kept is only part of what was said, and must never be
        transcribed or acted on."""
        with self._lock:
            if self._full or (self._seal is not None and self._seal._clipped):
                raise AudioOverflow("this utterance is not whole; it must be said again, not transcribed")
            pcm = b"".join(self._chunks)
        return wav_bytes(pcm)


class WhisperError(Exception):
    pass


# (url, body, headers, timeout_s[, cancel=token]) -> (status, reply body). Swapped out in tests.
Transport = Callable[..., tuple[int, bytes]]


def _loopback_transport(url: str, body: bytes, headers: dict[str, str], timeout_s: float,
                        cancel: CancelToken | None = None) -> tuple[int, bytes]:
    return _http.post(url, body, headers, timeout_s, cancel=cancel)


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

    def transcribe(self, wav: bytes, cancel: CancelToken | None = None) -> str:
        """The text of this WAV. Raises Cancelled if `cancel` is cancelled meanwhile (connection closed)."""
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
            if cancel is None:
                status, data = self._transport(self.endpoint, body, headers, self.timeout_s)
            else:
                status, data = self._transport(self.endpoint, body, headers, self.timeout_s, cancel=cancel)
        except Cancelled:
            raise
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


# Whisper marks non-speech as [BLANK_AUDIO] / [MUSIC] or as a parenthesised sound description. A tag
# is dropped only when the WHOLE of it is such a marker: [BLANK_AUDIO], or an optional known
# descriptor, a known sound word and an optional "playing" ("[MUSIC]", "(upbeat music)", "(laughs)",
# "(soft music playing)"). Every word allowed here is checked by a test never to be a negation,
# correction, stop or deferral cue. Anything else in brackets or parentheses is speech and is kept:
# "(don't)", "[stop]", "(no, I mean close it)" reach the gate. Same shape as the console's
# voice_probe sound-tag rule, with the descriptor slot and square brackets allowlisted as well.
SOUND_DESCRIPTORS = ("upbeat", "soft", "gentle", "light", "dramatic", "loud", "quiet", "calm", "slow",
                     "tense", "suspenseful", "cheerful", "sad", "mellow", "eerie", "peaceful", "faint",
                     "distant", "background", "intense", "ambient", "instrumental", "classical", "electronic",
                     "piano", "guitar", "orchestral", "funky", "jazzy", "somber", "ominous", "bright")
SOUND_WORDS = ("music", "laugh", "laughs", "laughing", "laughter", "applause", "silence", "noise",
               "inaudible", "cough", "coughs", "coughing", "sigh", "sighs", "sighing", "breath", "breathing",
               "breathes", "clap", "claps", "clapping", "static", "beep", "beeps", "beeping", "bell", "bells",
               "whistle", "whistles", "whistling", "sound", "sounds", "typing", "footsteps", "wind",
               "foreign language", "blank_audio", "blank audio")
_DESCRIPTOR_RX = "|".join(SOUND_DESCRIPTORS)
_SOUND_RX = "|".join(w.replace(" ", r"\s+") for w in SOUND_WORDS)
_TAG_BODY = rf"\s*(?:(?:{_DESCRIPTOR_RX})\s+)?(?:{_SOUND_RX})(?:\s+playing)?\s*"
_SOUND_TAG = re.compile(rf"\[{_TAG_BODY}\]|\({_TAG_BODY}\)", re.IGNORECASE)


def speech_only(text: str) -> str:
    """The transcript without whisper's non-speech markers. Every other word, bracketed or not, is kept."""
    return " ".join(_SOUND_TAG.sub(" ", text).split())


class LiveTranscriber:
    """Partial transcripts while the user speaks, and the final one as soon as speech ends.

    A worker thread sends the window heard so far to the whisper server, one request at a time,
    whenever there is new audio. The newest partial waits in a single slot until the caller takes
    it with poll(), on the caller's own thread (so the gate's database work stays there).

    finish() is called at end of speech. Before it transcribes the whole utterance it:
    - stops partials: none is started, and none is handed out, after end of speech;
    - drops a partial that was not taken yet;
    - cancels the partial request in flight: its connection is closed at once, so this client
      stops waiting immediately and the server can drop the work.

    What the cancel frees depends on the server. whisper.cpp's server notices a closed connection
    only between steps. Measured on a workstation (tiny.en, 4 threads, a 1.25 s partial): cancelled
    50/150/250 ms after it was sent, the partial still held the server for about 0.40/0.19/0.13 s
    (not cancelled: about 0.47/0.37/0.27 s). The step in progress, mostly the encoder pass, which
    is full size even for short audio, still runs; only the work after it is skipped. For a final
    that never waits behind a partial, give it its own server instance: final_client.

    An utterance that is not whole is invalid: it overflowed the window, audio for it was refused
    after its seal, or the window moved on to another utterance. From then on no partial is sent or
    handed out, and finish() raises AudioOverflow instead of returning a transcript, so the caller
    asks for a repeat (VoiceIntegration.finish_live does) and nothing is proposed.

    A transcriber serves the utterance the window holds when it is made; call finish() once the
    audio producer has delivered all of it.
    """

    def __init__(self, client: WhisperServerClient, window: PcmWindow, min_audio_s: float = 0.5,
                 poll_s: float = 0.02, final_client: WhisperServerClient | None = None):
        self._client = client
        self._final_client = final_client or client
        self._window = window
        self._generation = window.generation      # the utterance this transcriber is for
        self._min_audio_s = min_audio_s
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._ended = threading.Event()
        self._token: CancelToken | None = None
        self._slot: tuple[int, str] | None = None
        self._revision = 0
        self._thread: threading.Thread | None = None
        self.cancelled = 0      # partial requests cancelled by finish()
        self.partials = 0       # partial requests started

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="spark-partial-stt", daemon=True)
        self._thread.start()

    def poll(self) -> tuple[int, str] | None:
        """The newest partial (revision, text) not taken yet, or None. Never one after finish(), and
        never one of an utterance that is not whole."""
        with self._lock:
            p, self._slot = self._slot, None
            return None if self._ended.is_set() or not self._window.intact(self._generation) else p

    def finish(self, join_s: float = 1.0) -> str:
        """End of speech: stop and cancel partials, then make the final transcript.

        The window is sealed first: the transcript is of exactly the audio that had arrived, and any
        later append is refused. Raises AudioOverflow (not final) if the utterance overflowed, if the
        window moved on to another utterance, or if audio for this one was refused while the final
        was being made. Never returns a transcript of part of an utterance."""
        with self._lock:
            self._ended.set()      # from now on: no new partial, and poll() hands out nothing
            token = self._token
        if token is not None:
            token.cancel()
        try:
            sealed = self._window.seal(self._generation)
            text = speech_only(self._final_client.transcribe(sealed.audio))
            if not self._window.finalize(sealed):
                raise AudioOverflow("audio for this utterance arrived while its final was made; it must be said again")
            return text
        finally:
            if self._thread is not None:
                self._thread.join(join_s)

    def _loop(self) -> None:
        heard = 0.0
        while not self._ended.is_set():
            if not self._window.intact(self._generation):   # not whole, or another utterance's audio now
                break
            dur = self._window.duration_s
            if dur < self._min_audio_s or dur <= heard:
                self._ended.wait(self._poll_s)
                continue
            token = CancelToken()
            with self._lock:
                if self._ended.is_set():
                    break
                self._token = token
                self.partials += 1
            try:
                text = speech_only(self._client.transcribe(self._window.snapshot(), cancel=token))
            except Cancelled:
                with self._lock:
                    self.cancelled += 1
                break
            except AudioOverflow:      # snapshot() refuses an overflowed utterance: no more partials
                break
            except WhisperError:
                text = ""
            finally:
                with self._lock:
                    self._token = None
            heard = dur
            with self._lock:
                if self._ended.is_set() or not self._window.intact(self._generation):
                    break
                if text:
                    self._revision += 1
                    self._slot = (self._revision, text)
