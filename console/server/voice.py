"""Local voice. TTS: Piper (python API, then CLI). STT: whisper.cpp tiny.en, through a resident
whisper.cpp server on loopback when one is configured, else the whisper-cli binary.
Both are optional. If they are missing the endpoints say so and the iPad falls
back to its own on-device speechSynthesis (works offline) and typed commands.

Speech jobs run one at a time (one queue for STT, one for TTS), so a burst of requests waits
its turn instead of starting a pile of whisper / piper processes on the Pi. Each queue admits a
bounded number of jobs (and STT a bounded total of audio bytes); past that a request is refused
as busy straight away, and the iPad treats busy like unavailable.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import traceback
import wave
from pathlib import Path
from urllib.parse import urlsplit

from . import config


class VoiceBusy(RuntimeError):
    """A speech queue is full. Say so; the iPad falls back to typed commands / its own voice."""


def _detached(error: BaseException) -> BaseException:
    """The same exception object (type and message unchanged) with the tracebacks of it and of
    its whole __cause__ / __context__ chain cut loose. A failed job's frames hold its audio;
    left attached they would keep it alive, in a reference cycle, until the next cyclic GC,
    outside the queue's byte limit. Where it failed is kept as text, in a note."""
    seen, chain = set(), [error]
    while chain:
        e = chain.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        tb = e.__traceback__
        if tb is not None:
            if hasattr(e, "add_note"):  # Python 3.11+
                e.add_note("in the speech worker:\n" + "".join(traceback.format_tb(tb)).rstrip())
            e.__traceback__ = None
        chain += [e.__cause__, e.__context__]
    return error


class _Job:
    __slots__ = ("future", "fn", "args", "size")

    def __init__(self, future, fn, args, size: int) -> None:
        self.future, self.fn, self.args, self.size = future, fn, args, size


class Reservation:
    """Room held in a speech queue (one job slot and `size` bytes) before the audio is read.
    Hand it to run(..., reservation=...) for the job; on every other way out, release() it (it
    is a context manager). trim() gives back what the audio turned out not to need."""

    __slots__ = ("_queue", "size", "_held")

    def __init__(self, queue: "_SpeechQueue", size: int) -> None:
        self._queue, self.size, self._held = queue, size, True

    def trim(self, size: int) -> None:
        self._queue._trim(self, size)

    def release(self) -> None:
        self._queue._unreserve(self)

    def __enter__(self) -> "Reservation":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class _SpeechQueue:
    """One worker thread, so two jobs of a kind never overlap, with bounded admission: at most
    max_jobs() jobs waiting or running (or reserved), holding at most max_bytes() of audio
    between them. reserve() takes the room before the audio is even read.

    The waiting line is our own deque, not ThreadPoolExecutor's queue (which keeps a cancelled
    job and its audio until the worker reaches it). A slot is given back only together with the
    job's arguments: a job cancelled while it waits is taken out of the line in the same locked
    step, and a running job keeps its slot until it finishes, even if its caller has gone."""

    def __init__(self, name: str, max_jobs, max_bytes=lambda: None) -> None:
        self._name = name
        self._max_jobs, self._max_bytes = max_jobs, max_bytes
        self._cond = threading.Condition()
        self._waiting = collections.deque()
        self._worker = None
        self.jobs = self.bytes = 0

    def _admit(self, size: int) -> None:
        """Raise VoiceBusy unless one more job of `size` bytes fits. Hold the lock."""
        max_jobs, max_bytes = self._max_jobs(), self._max_bytes()
        if max_bytes is not None and size > max_bytes:
            raise VoiceBusy(f"audio too large: {size} bytes (limit {max_bytes})")
        if self.jobs >= max_jobs:
            raise VoiceBusy(f"busy: {self.jobs} speech jobs already waiting or running (limit {max_jobs})")
        if max_bytes is not None and self.bytes + size > max_bytes:
            raise VoiceBusy(f"busy: {self.bytes} bytes of audio already queued (limit {max_bytes})")

    def reserve(self, size: int) -> Reservation:
        """Hold a job slot and `size` bytes now, before the audio is read; VoiceBusy if full."""
        with self._cond:
            self._admit(size)
            self.jobs += 1
            self.bytes += size
            return Reservation(self, size)

    def _trim(self, reservation: Reservation, size: int) -> None:
        with self._cond:
            if not reservation._held or not 0 <= size <= reservation.size:
                raise ValueError(f"cannot trim a reservation of {reservation.size} bytes to {size}")
            self.bytes -= reservation.size - size
            reservation.size = size

    def _unreserve(self, reservation: Reservation) -> None:
        with self._cond:
            if reservation._held:  # not handed to a job, not released yet
                reservation._held = False
                self.jobs -= 1
                self.bytes -= reservation.size

    async def run(self, fn, *args, size: int = 0, reservation: Reservation | None = None):
        """Queue fn(*args) as a job of `size` bytes. With a reservation, the job takes over its
        slot and bytes (trimmed to `size`) instead of being admitted afresh."""
        future = concurrent.futures.Future()
        future.add_done_callback(self._drop_if_cancelled)
        with self._cond:
            if reservation is None:
                self._admit(size)
            elif reservation._queue is not self or not reservation._held or size > reservation.size:
                raise ValueError("the reservation is spent, released, too small or not this queue's")
            if self._worker is None:
                worker = threading.Thread(target=self._work, name=f"{self._name}-speech", daemon=True)
                worker.start()
                self._worker = worker
            if reservation is None:
                self.jobs += 1
                self.bytes += size
            else:  # the job holds the reserved slot and bytes from here on
                self.bytes -= reservation.size - size
                reservation.size, reservation._held = size, False
            self._waiting.append(_Job(future, fn, args, size))
            self._cond.notify()
        del fn, args  # the job holds them now, and lets go of them when it gives its slot back
        try:
            return await asyncio.wrap_future(future)
        finally:
            # A failure re-raised here carries this frame (and the callers' frames, which hold
            # the audio) in its traceback; the future holds that same exception. Drop the
            # frame's reference to the future so the two do not form a cycle.
            del future

    def _drop_if_cancelled(self, future) -> None:
        """A caller gave up. If its job is still waiting, take it out of the line (dropping its
        audio) and give its slot back. If the worker already has it, the worker does that."""
        if not future.cancelled():
            return
        with self._cond:
            for job in self._waiting:
                if job.future is future:
                    self._waiting.remove(job)
                    self._release(job)
                    return

    def _release(self, job: _Job) -> None:
        """Give a job's slot back, dropping its arguments (the audio) first. Hold the lock."""
        job.fn = job.args = None
        self.jobs -= 1
        self.bytes -= job.size

    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._waiting:
                    self._cond.wait()
                job = self._waiting.popleft()
            self._run(job)
            del job  # pin nothing of a finished job while idle

    def _run(self, job: _Job) -> None:
        started = job.future.set_running_or_notify_cancel()  # False: cancelled after it left the line
        ok, value = False, None
        if started:
            try:
                ok, value = True, job.fn(*job.args)
            except BaseException as e:  # noqa: BLE001 - handed to the caller
                value = _detached(e)  # its frames hold the audio: let them go now, not at GC time
        with self._cond:
            self._release(job)
        if started:
            if ok:
                job.future.set_result(value)
            else:
                job.future.set_exception(value)


_STT = _SpeechQueue("stt", lambda: config.STT_MAX_PENDING, lambda: config.STT_MAX_PENDING_BYTES)
_TTS = _SpeechQueue("tts", lambda: config.TTS_MAX_PENDING)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_piper_voice = None
_piper_voice_version = None  # voice_version() the resident voice was loaded from


def tts_status() -> dict:
    model = Path(config.PIPER_MODEL)
    py = False
    try:
        import piper  # noqa: F401
        py = True
    except Exception:  # noqa: BLE001
        pass
    return {"piper_model": model.exists(), "piper_python": py, "piper_cli": shutil.which(config.PIPER_BIN) is not None}


def stt_status() -> dict:
    return {"whisper_bin": Path(config.WHISPER_BIN).exists() or shutil.which(config.WHISPER_BIN) is not None,
            "whisper_model": Path(config.WHISPER_MODEL).exists(), "ffmpeg": shutil.which(config.FFMPEG_BIN) is not None,
            "whisper_server": whisper_server_endpoint() is not None}


# ------------------------------------------------------------------ TTS
def voice_version() -> tuple | None:
    """Which Piper voice is on disk: resolved path, size and mtime of the model and of its
    .onnx.json config. Any change is a different voice. None when the model is missing."""
    model = Path(config.PIPER_MODEL)
    parts = []
    for path in (model, Path(f"{model}.json")):
        try:
            st = path.stat()
        except OSError:
            if path is model:
                return None
            continue
        parts.append((str(path.resolve()), st.st_size, st.st_mtime_ns))
    return tuple(parts)


def _tts_cache_path(text: str, version: tuple) -> Path:
    """Cache name from the words AND the voice, so a changed voice never serves old audio."""
    return config.TTS_CACHE / (hashlib.sha256(repr((version, text)).encode()).hexdigest()[:32] + ".wav")


def _resident_voice(version: tuple):
    """The resident PiperVoice for this voice version: loaded once (again only when the voice on
    disk changes), or None without the piper package. Only the TTS worker calls this."""
    global _piper_voice, _piper_voice_version
    try:
        from piper import PiperVoice
    except Exception:  # noqa: BLE001
        return None
    if _piper_voice is None or _piper_voice_version != version:  # first use, or the voice changed
        _piper_voice = PiperVoice.load(config.PIPER_MODEL)
        _piper_voice_version = version
    return _piper_voice


def _synth_py(text: str, out: Path, version: tuple) -> bool:
    piper_voice = _resident_voice(version)
    if piper_voice is None:
        return False
    with wave.open(str(out), "wb") as wf:
        piper_voice.synthesize_wav(text, wf)
    return out.exists() and out.stat().st_size > 100


def _warm_job() -> bool:
    version = voice_version()
    piper_voice = None if version is None else _resident_voice(version)
    if piper_voice is None:
        return False
    with wave.open(io.BytesIO(), "wb") as wf:  # one throwaway reply: the first synthesis is slow too
        piper_voice.synthesize_wav("Ready.", wf)
    return True


async def warm_tts() -> bool:
    """Load the resident Piper voice now, as a job on the TTS queue (so it cannot race a reply),
    rather than on the first reply. Measured on this box with the lessac-medium voice: loading
    takes ~7.7 s and the first synthesis after it ~2.5 s more; later replies take 44-92 ms (the
    piper CLI, which loads the voice per reply, ~740 ms). False when there is nothing to warm
    (no piper package, no model) or loading failed; replies then use the CLI as before."""
    try:
        return await _TTS.run(_warm_job)
    except Exception:  # noqa: BLE001 - a voice that will not load is the first reply's problem, as before
        return False


def _synth_cli(text: str, out: Path) -> bool:
    if not shutil.which(config.PIPER_BIN):
        return False
    p = subprocess.run([config.PIPER_BIN, "--model", config.PIPER_MODEL, "--output_file", str(out)],
                       input=text.encode(), capture_output=True, timeout=30)
    return p.returncode == 0 and out.exists() and out.stat().st_size > 100


def _synth_job(text: str, out: Path, version: tuple) -> bool:
    if out.exists():  # a queued request for the same words already made it
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write a temp file in the cache folder, then rename it over the final name in one step:
    # a reader sees the whole file or no file, never a half-written one.
    fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=".wav", dir=out.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        if not (_synth_py(text, tmp, version) or _synth_cli(text, tmp)):
            return False
        os.replace(tmp, out)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        tmp.unlink(missing_ok=True)


async def synthesize(text: str) -> Path | None:
    """Path of a WAV of the words, or None when Piper is not there. Raises VoiceBusy when the
    TTS queue is full."""
    text = text.strip()[:400]
    version = voice_version()
    if not text or version is None:
        return None
    out = _tts_cache_path(text, version)
    if out.exists():
        return out
    ok = await _TTS.run(_synth_job, text, out, version)
    return out if ok else None


# ------------------------------------------------------------------ STT
def whisper_server_endpoint() -> str | None:
    """The /inference URL of the resident whisper.cpp server, or None when it is off. A host
    that is not loopback is ignored: recordings of the room stay on this board."""
    raw = (config.WHISPER_SERVER_URL or "").strip()
    if not raw:
        return None
    try:
        u = urlsplit(raw)
        u.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return None
    if (u.scheme not in ("http", "https") or u.hostname not in _LOOPBACK_HOSTS
            or "@" in u.netloc or u.query or u.fragment):
        return None
    # A numeric host: no resolver call (which no deadline can cancel) ever sits in front of the clip.
    host = "127.0.0.1" if u.hostname == "localhost" else u.hostname
    netloc = (f"[{host}]" if ":" in host else host) + (f":{u.port}" if u.port else "")
    return f"{u.scheme}://{netloc}{u.path.rstrip('/')}/inference"


# fmt chunk of the one format whisper.cpp reads as-is: (format tag 1 = PCM, 1 channel, 16000 Hz,
# 2-byte blocks, 16 bits per sample).
_NATIVE_FMT = (1, 1, 16000, 2, 16)
_EXTENSIBLE = 0xFFFE
_PCM_SUBTYPE = bytes.fromhex("0100000000001000800000aa00389b71")  # KSDATAFORMAT_SUBTYPE_PCM


def _native_data(path: Path) -> tuple[int, int, bool] | None:
    """(offset, size, exact) of the audio in the data chunk if the file holds the audio
    whisper.cpp reads: a RIFF/WAVE file whose fmt chunk says PCM, 16 bits per sample, 1 channel,
    16000 Hz. Else None, and the clip goes through ffmpeg. The fmt chunk is read here directly,
    with small bounded reads (at most 40 bytes of it): the wave module only reports whole bytes
    per sample, so 12-bit audio in 2-byte samples would pass it. PCM means format tag 1, or
    WAVE_FORMAT_EXTENSIBLE with cbSize >= 22, 16 valid bits and the PCM subtype GUID; any
    other extensible layout (fewer valid bits, a float subtype, a short extension) is not
    native, and so is a file with more than one fmt chunk. Judged from the bytes, not the name.

    size counts only whole samples actually in the file. exact is True only when the header
    says exactly that: the RIFF size is the file size - 8, the data chunk's declared size is
    that size, and the data chunk ends the file (nothing after it, so no second data chunk). A
    file that is not exact (a streaming size like 0xFFFFFFFF, a RIFF size that disagrees, odd or
    trailing bytes) must not reach whisper as it is: a reader may trust the declared sizes. An
    extensible file is never exact either, so whisper always gets it as a plain tag-1 WAV."""
    try:
        with open(path, "rb") as f:
            file_size = os.fstat(f.fileno()).st_size
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:] != b"WAVE":
                return None
            riff_size = int.from_bytes(head[4:8], "little")
            fmt, extensible = None, False
            for _ in range(64):  # a bounded walk over the chunks
                chunk = f.read(8)
                if len(chunk) < 8:
                    return None
                cid, size = chunk[:4], int.from_bytes(chunk[4:], "little")
                if cid == b"fmt ":
                    if fmt is not None:  # a second fmt chunk: which one a reader believes is unclear
                        return None
                    body = f.read(min(size, 40))
                    if size < 16 or len(body) < 16:
                        return None
                    tag, channels, rate, _byte_rate, align, bits = struct.unpack("<HHIIHH", body[:16])
                    extensible = tag == _EXTENSIBLE
                    if extensible:  # PCM only with the full extension, all 16 bits valid, PCM subtype
                        cb_size, valid_bits = struct.unpack("<HH", body[16:20]) if len(body) == 40 else (0, 0)
                        pcm = cb_size >= 22 and valid_bits == 16 and body[24:40] == _PCM_SUBTYPE
                        tag = 1 if pcm else None
                    fmt = (tag, channels, rate, align, bits)
                    f.seek(size - len(body) + (size & 1), os.SEEK_CUR)
                elif cid == b"data":
                    if fmt != _NATIVE_FMT:  # also None: data before any fmt chunk
                        return None
                    offset = f.tell()
                    present = min(size, file_size - offset) // 2 * 2
                    exact = (not extensible and riff_size == file_size - 8 and size == present
                             and offset + present == file_size)
                    return offset, present, exact
                else:
                    f.seek(size + (size & 1), os.SEEK_CUR)  # chunks are padded to even sizes
    except OSError:
        pass
    return None


_COPY_BLOCK = 64 * 1024


def _strip_to_data(src: Path, offset: int, size: int, dst: Path) -> None:
    """Write a native clip as a plain WAV: a 44-byte header, then its data chunk copied in
    bounded blocks (never read whole into memory). Drops every other chunk (metadata)."""
    header = (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
              + b"data" + struct.pack("<I", size))
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(header)
        fin.seek(offset)
        left = size
        while left:
            block = fin.read(min(left, _COPY_BLOCK))
            if not block:
                raise OSError(f"{src.name} ended inside its data chunk")
            fout.write(block)
            left -= len(block)


def wav_byte_limit() -> int:
    """Largest WAV FILE that STT will hand to whisper: STT_MAX_SECONDS of 16 kHz mono 16-bit PCM,
    plus the extra second ffmpeg may write (see _transcribe_job) and a little slack for the
    header. A cap on the whole file; the length limit is measured on the data chunk alone."""
    return int((config.STT_MAX_SECONDS + 2) * 16000 * 2) + 4096


def _too_long(seconds: float) -> str:
    return f"audio too long: {seconds:.1f} s (limit {config.STT_MAX_SECONDS:g} s)"


def _too_large(path: Path) -> str:
    return f"file too large: {path.stat().st_size} bytes (limit {wav_byte_limit()} bytes)"


class _WhisperHTTP:
    """One long-lived HTTP connection pool for the resident whisper.cpp server, on its own
    event-loop thread. Every clip reuses it (and its keep-alive connection): building an httpx
    client per clip cost ~200 ms, most of it loading the CA bundle into a new SSL context.

    It is httpcore's plain pool (httpx only encodes the multipart body), so a plain-http
    loopback URL creates no TLS context at all; an https one gets one, once, when it first
    connects. It never reads proxy settings from the environment and never follows a
    redirect. Each POST runs under ONE overall deadline, with the same limit on each connect,
    read and write. Created on first use; close() at shutdown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pool = None  # touched only on the loop thread

    def _running_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever, name="whisper-http", daemon=True)
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def post(self, endpoint: str, wav: Path, fields: dict, limit: float) -> tuple[str, str]:
        """POST the clip; TimeoutError if the whole exchange takes longer than `limit` s."""
        coro = asyncio.wait_for(self._post(endpoint, wav, fields, limit), limit)
        return asyncio.run_coroutine_threadsafe(coro, self._running_loop()).result(timeout=limit + 5)

    async def _post(self, endpoint: str, wav: Path, fields: dict, limit: float) -> tuple[str, str]:
        import httpcore
        import httpx

        if self._pool is None:
            self._pool = httpcore.AsyncConnectionPool(max_connections=1)
        request = httpx.Request("POST", endpoint, data=fields,
                                files={"file": (wav.name, wav.read_bytes(), "audio/wav")})
        reply = await self._pool.request("POST", endpoint, headers=request.headers.raw, content=request.read(),
                                         extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), limit)})
        if reply.status != 200:  # a redirect included: it is not followed
            raise ValueError(f"HTTP {reply.status}")
        text = json.loads(reply.content)["text"]
        if not isinstance(text, str):
            raise ValueError("reply has no text")
        return text.strip(), "ok (whisper server)"

    def close(self) -> None:
        """Close the pool and stop its loop thread. A later post() starts afresh."""
        with self._lock:
            loop, thread, self._loop, self._thread = self._loop, self._thread, None, None
        if loop is None:
            return

        async def shut() -> None:
            pool, self._pool = self._pool, None
            if pool is not None:
                await pool.aclose()

        try:
            asyncio.run_coroutine_threadsafe(shut(), loop).result(timeout=5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            if not thread.is_alive():
                loop.close()


_WHISPER_HTTP = _WhisperHTTP()


def close() -> None:
    """Shutdown: close the whisper server's connection pool and its loop thread."""
    _WHISPER_HTTP.close()


def _server_transcribe(endpoint: str, wav: Path) -> tuple[str | None, str]:
    """POST the clip to whisper.cpp's /inference under ONE overall deadline, so a server that
    accepts and then trickles or never answers cannot hold the STT worker. (None, reason) on
    any failure; the caller then uses whisper-cli."""
    limit = config.WHISPER_SERVER_TIMEOUT_S
    fields = {"response_format": "json"}
    if config.WHISPER_AUDIO_CTX:
        fields["audio_ctx"] = str(config.WHISPER_AUDIO_CTX)
    if config.WHISPER_PROMPT:
        fields["prompt"] = config.WHISPER_PROMPT
    try:
        return _WHISPER_HTTP.post(endpoint, wav, fields, limit)
    except (TimeoutError, asyncio.TimeoutError, concurrent.futures.TimeoutError):
        return None, f"whisper server failed: no answer within {limit:g} s"
    except Exception as e:  # noqa: BLE001 - any failure means: use whisper-cli instead
        return None, f"whisper server failed: {type(e).__name__}: {str(e)[:120]}"


def _transcribe_job(audio: bytes, suffix: str, details: dict | None = None) -> tuple[str | None, str]:
    st = stt_status()
    server = whisper_server_endpoint()
    cli = st["whisper_bin"] and st["whisper_model"]
    if not (server or cli):
        return None, f"STT not installed: {st}"
    with tempfile.TemporaryDirectory() as d:
        # Different names: an upload called *.wav must never be the file ffmpeg writes.
        src = Path(d) / f"upload{suffix}"
        src.write_bytes(audio)
        native = _native_data(src)  # reads only the chunk headers
        if native is not None:  # 16 kHz mono 16-bit PCM: never through ffmpeg
            wav, conversion = src, "native"
        elif st["ffmpeg"]:
            wav, conversion = Path(d) / "converted-16k-mono.wav", "ffmpeg"
        else:
            return None, f"STT not installed: {st}"
        if details is not None:
            details["conversion"] = conversion  # the route, recorded even if the clip is refused below
        if conversion == "ffmpeg":
            # Bound the DECODED audio while ffmpeg writes it (-t length, -fs file size): a small,
            # long compressed clip must not grow into a WAV that fills memory or /tmp. ffmpeg may
            # write one second past the limit so the check below can tell "too long" from
            # "exactly at the limit".
            conv = subprocess.run([config.FFMPEG_BIN, "-y", "-loglevel", "error", "-i", str(src),
                                   "-t", f"{config.STT_MAX_SECONDS + 1:g}", "-fs", str(wav_byte_limit()),
                                   "-map_metadata", "-1", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                                  capture_output=True, timeout=20)
            if conv.returncode != 0:
                return None, "ffmpeg failed: " + conv.stderr.decode()[-200:]
            if wav.stat().st_size > wav_byte_limit():
                return None, _too_large(wav)  # size first: a runaway output is never read
            native = _native_data(wav)
            if native is None:
                return None, "audio is not 16 kHz mono 16-bit PCM after conversion"
        offset, size, exact = native
        if size / 32000 > config.STT_MAX_SECONDS:
            return None, _too_long(size / 32000)  # from the data chunk alone, before it is read
        if not exact or wav.stat().st_size > wav_byte_limit():
            # Its header does not say exactly what it holds, or extra chunks (metadata) make it
            # too big: whisper gets a plain WAV of just the audio, with its true length.
            plain = Path(d) / "plain-16k-mono.wav"
            _strip_to_data(wav, offset, size, plain)
            wav = plain
            if conversion == "native":
                conversion = "native-stripped"
                if details is not None:
                    details["conversion"] = conversion
        server_why = ""
        if server:
            text, server_why = _server_transcribe(server, wav)
            if text is not None or not cli:
                return text, server_why
        cmd = [config.WHISPER_BIN, "-m", config.WHISPER_MODEL, "-f", str(wav), "-nt", "-np", "-t", "4"]
        if config.WHISPER_AUDIO_CTX:
            cmd += ["-ac", str(config.WHISPER_AUDIO_CTX)]
        if config.WHISPER_PROMPT:
            cmd += ["--prompt", config.WHISPER_PROMPT]
        p = subprocess.run(cmd, capture_output=True, timeout=40)
        if p.returncode != 0:
            return None, "whisper failed: " + p.stderr.decode()[-200:] + (f" ({server_why})" if server_why else "")
        return p.stdout.decode().strip(), f"ok (whisper-cli; {server_why})" if server_why else "ok"


def reserve_stt(size: int) -> Reservation:
    """Room in the STT queue for a clip of up to `size` bytes, taken before it is read (so a
    full queue answers busy without the upload ever being held in memory). VoiceBusy if full."""
    return _STT.reserve(size)


async def transcribe(audio: bytes, suffix: str, details: dict | None = None,
                     reservation: Reservation | None = None) -> tuple[str | None, str]:
    """(text, reason). If a dict is given as details, details["conversion"] is set to what the
    clip went through: "native", "native-stripped" (rewritten as a plain WAV of just its audio)
    or "ffmpeg". A reservation from reserve_stt() is used for the job instead of a fresh
    admission check."""
    try:
        return await _STT.run(_transcribe_job, audio, suffix, details, size=len(audio), reservation=reservation)
    except VoiceBusy as e:
        return None, str(e)
