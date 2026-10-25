"""Console voice (console/server/voice.py) and the STT probe (scripts/voice_probe.py).

Hermetic: ffmpeg, whisper-cli and piper are fakes; the whisper.cpp server is a stdlib HTTP server
on 127.0.0.1 inside the test. WAV clips are made here with the wave module.
"""

import array
import asyncio
import gc
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import types
import wave
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from console.server import config, voice  # noqa: E402
import voice_probe  # noqa: E402

_real_synth_py = voice._synth_py


def wav_bytes(rate=16000, channels=1, width=2, frames=1600, fill=b"\x10\x20"):
    size = frames * channels * width
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes((fill * (size // len(fill) + 1))[:size])
    return buf.getvalue()


CONVERTED = wav_bytes(frames=800, fill=b"\x7f\x01")  # what the fake ffmpeg writes


def run(coro):
    return asyncio.run(coro)


class FakeSpeechProcs:
    """Stands in for subprocess.run. ffmpeg writes CONVERTED to its output path; whisper-cli
    prints self.heard and remembers the bytes of the file it was handed."""

    def __init__(self):
        self.heard = "pick up the beaker"
        self.calls = []
        self.delay = 0.0
        self.active = self.max_active = 0
        self.lock = threading.Lock()
        self.upload_after_ffmpeg = None
        self.whisper_saw = None
        self.gate = None  # threading.Event: hold every process until it is set
        self.time_out_on = set()  # whisper-cli call numbers (1-based) that raise TimeoutExpired
        self.decoded_seconds = None  # None: ffmpeg writes CONVERTED; else this much 16 kHz audio
        self.obeys_t = True  # a real ffmpeg stops at -t; a broken converter might not
        self.extra_bytes = 0  # bytes after the data chunk that the WAV header does not count
        self.ffmpeg_wrote = None

    def __call__(self, cmd, **kwargs):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.gate:
                self.gate.wait(2)
            time.sleep(self.delay)
            tool = Path(cmd[0]).name
            self.calls.append((tool, list(cmd)))
            if tool == "whisper-cli" and self.tools().count("whisper-cli") in self.time_out_on:
                raise subprocess.TimeoutExpired(cmd, 40)
            if tool == "ffmpeg":
                src, dst = Path(cmd[cmd.index("-i") + 1]), Path(cmd[-1])
                dst.write_bytes(self.decode(cmd))
                self.ffmpeg_wrote = dst.stat().st_size
                self.upload_after_ffmpeg = src.read_bytes()
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            if tool == "whisper-cli":
                self.whisper_saw = Path(cmd[cmd.index("-f") + 1]).read_bytes()
                return subprocess.CompletedProcess(cmd, 0, f" {self.heard}\n".encode(), b"")
            raise AssertionError(f"unexpected process: {cmd}")
        finally:
            with self.lock:
                self.active -= 1

    def decode(self, cmd):
        if self.decoded_seconds is None:
            return CONVERTED
        seconds = self.decoded_seconds
        if self.obeys_t:
            seconds = min(seconds, float(cmd[cmd.index("-t") + 1]))
        return wav_bytes(frames=int(seconds * 16000)) + b"\0" * self.extra_bytes

    def tools(self):
        return [tool for tool, _ in self.calls]


@pytest.fixture
def stt(monkeypatch, tmp_path):
    whisper_bin = tmp_path / "whisper-cli"
    whisper_bin.write_bytes(b"")
    model = tmp_path / "ggml-tiny.en.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(config, "WHISPER_BIN", str(whisper_bin))
    monkeypatch.setattr(config, "WHISPER_MODEL", str(model))
    monkeypatch.setattr(config, "FFMPEG_BIN", "ffmpeg")
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", "")
    monkeypatch.setattr(config, "WHISPER_SERVER_TIMEOUT_S", 5.0)
    installed = {"ffmpeg"}
    monkeypatch.setattr(voice, "shutil", types.SimpleNamespace(
        which=lambda name: f"/usr/bin/{name}" if name in installed else None))
    procs = FakeSpeechProcs()
    procs.installed = installed
    monkeypatch.setattr(voice, "subprocess", types.SimpleNamespace(run=procs))
    return procs


# ------------------------------------------------------------------ input handling
def test_wav_upload_is_not_overwritten_by_the_ffmpeg_output(stt):
    upload = wav_bytes(rate=44100, channels=2)  # a .wav that still needs converting
    text, why = run(voice.transcribe(upload, ".wav"))
    assert (text, why) == ("pick up the beaker", "ok")
    ((tool, cmd),) = [c for c in stt.calls if c[0] == "ffmpeg"]
    assert Path(cmd[cmd.index("-i") + 1]).resolve() != Path(cmd[-1]).resolve()
    assert stt.upload_after_ffmpeg == upload  # ffmpeg's output did not land on its input
    assert stt.whisper_saw == CONVERTED  # whisper read the converted file


@pytest.mark.parametrize("suffix", [".wav", ".webm", ""])
def test_native_16k_mono_pcm_skips_ffmpeg(stt, suffix):
    upload = wav_bytes()
    text, _ = run(voice.transcribe(upload, suffix))
    assert text == "pick up the beaker"
    assert stt.tools() == ["whisper-cli"]
    assert stt.whisper_saw == upload


def test_native_pcm_needs_no_ffmpeg_installed(stt):
    stt.installed.clear()
    assert run(voice.transcribe(wav_bytes(), ".wav"))[0] == "pick up the beaker"
    assert stt.tools() == ["whisper-cli"]


@pytest.mark.parametrize("audio,suffix", [
    (wav_bytes(rate=44100), ".wav"),
    (wav_bytes(rate=8000), ".wav"),
    (wav_bytes(channels=2), ".wav"),
    (wav_bytes(width=1), ".wav"),
    (wav_bytes(width=4), ".wav"),
    (b"RIFF\x00\x00", ".wav"),  # truncated header
    (b"\x1aE\xdf\xa3 not a wav, a webm", ".webm"),
])
def test_anything_else_is_converted(stt, audio, suffix):
    text, _ = run(voice.transcribe(audio, suffix))
    assert text == "pick up the beaker"
    assert stt.tools() == ["ffmpeg", "whisper-cli"]
    assert stt.whisper_saw == CONVERTED


def test_non_native_without_ffmpeg_says_so(stt):
    stt.installed.clear()
    text, why = run(voice.transcribe(wav_bytes(rate=44100), ".wav"))
    assert text is None and "not installed" in why and stt.calls == []


# ------------------------------------------------------------------ resident whisper.cpp server
@pytest.mark.parametrize("url,endpoint", [
    ("http://127.0.0.1:8178", "http://127.0.0.1:8178/inference"),
    ("http://localhost:8178/", "http://127.0.0.1:8178/inference"),
    ("http://localhost/", "http://127.0.0.1/inference"),
    ("http://[::1]:8178", "http://[::1]:8178/inference"),
    ("  http://127.0.0.1:8178/whisper/  ", "http://127.0.0.1:8178/whisper/inference"),
])
def test_loopback_server_url_is_used(monkeypatch, url, endpoint):
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", url)
    assert voice.whisper_server_endpoint() == endpoint


@pytest.mark.parametrize("url", [
    "", "http://192.168.1.20:8178", "http://example.com:8178", "http://0.0.0.0:8178",
    "http://127.0.0.1.example.com:8178", "http://localhost.example.com", "http://127.0.0.1@example.com:8178",
    "http://user:pw@127.0.0.1:8178", "ftp://127.0.0.1:8178", "file:///tmp/sock", "http://127.0.0.1:99999",
    "http://127.0.0.1:8178/?next=http://example.com", "127.0.0.1:8178", "localhost:8178",
])
def test_non_loopback_server_url_is_ignored(monkeypatch, url):
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", url)
    assert voice.whisper_server_endpoint() is None


def test_ignored_server_url_never_gets_the_audio(stt, monkeypatch):
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", "http://192.168.1.20:8178")
    monkeypatch.setattr(voice, "_server_transcribe", lambda *a: pytest.fail("audio sent off loopback"))
    assert run(voice.transcribe(wav_bytes(), ".wav"))[0] == "pick up the beaker"
    assert stt.tools() == ["whisper-cli"]


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # the 'slow' case hangs up on us; fine
        pass


@pytest.fixture
def whisper_server():
    state = {"mode": "ok", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append((self.path, self.headers.get("Content-Type", ""), body))
            mode = state["mode"]
            if mode == "slow":
                time.sleep(1.5)
            if mode == "trickle":  # answers, one byte every 0.1 s: no single read ever times out
                reply = b'{"text": "late"}'.ljust(30)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                for i in range(len(reply)):
                    self.wfile.write(reply[i:i + 1])
                    self.wfile.flush()
                    time.sleep(0.1)
                return
            if mode == "500":
                self.send_response(500)
                self.end_headers()
                return
            reply = {"ok": b'{"text": " hello from the server\\n"}', "slow": b'{"text": "late"}',
                     "nojson": b"<html>busy</html>", "notext": b'{"error": "no model"}'}[mode]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    srv = _QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()
    srv.server_close()


def test_server_transcript_is_used_when_it_answers(stt, whisper_server, monkeypatch):
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", whisper_server["url"])
    upload = wav_bytes()
    text, why = run(voice.transcribe(upload, ".wav"))
    assert text == "hello from the server" and "server" in why
    assert stt.calls == []  # neither ffmpeg nor whisper-cli ran
    ((path, ctype, body),) = whisper_server["requests"]
    assert path == "/inference" and ctype.startswith("multipart/form-data")
    assert b'name="file"' in body and upload in body


def test_localhost_never_waits_on_the_resolver(stt, whisper_server, monkeypatch):
    import socket
    asked, real = [], socket.getaddrinfo

    def stalled(host, *a, **k):  # a resolver that hangs for "localhost" (anyio passes it as bytes)
        asked.append(host.decode() if isinstance(host, bytes) else host)
        if asked[-1] == "localhost":
            threading.Event().wait(5)
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", stalled)
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", whisper_server["url"].replace("127.0.0.1", "localhost"))
    start = time.monotonic()
    text, why = run(voice.transcribe(wav_bytes(), ".wav"))
    assert text == "hello from the server" and "localhost" not in asked
    assert time.monotonic() - start < 2


def test_server_alone_is_enough(stt, whisper_server, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", whisper_server["url"])
    monkeypatch.setattr(config, "WHISPER_BIN", str(tmp_path / "no-cli"))
    assert run(voice.transcribe(wav_bytes(), ".wav"))[0] == "hello from the server"


def _closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("mode", ["500", "nojson", "notext", "slow", "down"])
def test_server_failure_falls_back_to_whisper_cli(stt, whisper_server, monkeypatch, mode):
    whisper_server["mode"] = mode
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", _closed_port_url() if mode == "down" else whisper_server["url"])
    monkeypatch.setattr(config, "WHISPER_SERVER_TIMEOUT_S", 0.3)
    started = time.monotonic()
    text, why = run(voice.transcribe(wav_bytes(), ".wav"))
    assert text == "pick up the beaker" and "whisper server failed" in why
    assert stt.tools() == ["whisper-cli"]
    assert time.monotonic() - started < 1.4  # the timeout bounds the wait (slow server sleeps 1.5 s)


def test_server_failure_without_cli_reports_the_server_error(stt, whisper_server, monkeypatch, tmp_path):
    whisper_server["mode"] = "500"
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", whisper_server["url"])
    monkeypatch.setattr(config, "WHISPER_BIN", str(tmp_path / "no-cli"))
    text, why = run(voice.transcribe(wav_bytes(), ".wav"))
    assert text is None and "whisper server failed" in why and stt.calls == []


# ------------------------------------------------------------------ one speech job at a time
def test_concurrent_stt_requests_queue(stt):
    stt.delay = 0.05

    async def burst():
        return await asyncio.gather(*(voice.transcribe(wav_bytes(rate=44100), ".wav") for _ in range(4)))

    results = run(burst())
    assert [t for t, _ in results] == ["pick up the beaker"] * 4
    assert len(stt.calls) == 8 and stt.max_active == 1


class FakeSynth:
    def __init__(self):
        self.calls = []
        self.delay = 0.0
        self.active = self.max_active = 0
        self.lock = threading.Lock()
        self.during = None  # optional hook(out) called mid-write
        self.gate = None  # threading.Event: hold every job until it is set

    def __call__(self, text, out, version):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.gate:
                self.gate.wait(2)
            time.sleep(self.delay)
            self.calls.append((text, Path(out), version))
            data = wav_bytes(frames=400)
            with open(out, "wb") as f:
                f.write(data[:30])
                if self.during:
                    self.during(Path(out))
                f.write(data[30:])
            return True
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def piper(monkeypatch, tmp_path):
    model = tmp_path / "voices" / "en_US-test.onnx"
    model.parent.mkdir()
    model.write_bytes(b"onnx-v1")
    monkeypatch.setattr(config, "PIPER_MODEL", str(model))
    monkeypatch.setattr(config, "TTS_CACHE", tmp_path / "tts_cache")
    monkeypatch.setattr(voice, "_piper_voice", None)
    monkeypatch.setattr(voice, "_piper_voice_version", None)
    monkeypatch.setattr(voice, "_synth_cli", lambda text, out: False)
    synth = FakeSynth()
    monkeypatch.setattr(voice, "_synth_py", synth)
    return types.SimpleNamespace(model=model, cache=tmp_path / "tts_cache", synth=synth)


def test_concurrent_tts_requests_queue(piper):
    piper.synth.delay = 0.05

    async def burst():
        return await asyncio.gather(*(voice.synthesize(w) for w in ["one", "two", "three", "four"]))

    paths = run(burst())
    assert all(p and p.exists() for p in paths) and len(set(paths)) == 4
    assert piper.synth.max_active == 1


def test_same_words_at_once_synthesize_once(piper):
    piper.synth.delay = 0.05

    async def burst():
        return await asyncio.gather(*(voice.synthesize("approve it") for _ in range(3)))

    assert len(set(run(burst()))) == 1 and len(piper.synth.calls) == 1


# ------------------------------------------------------------------ Piper cache
def test_cache_file_appears_whole_or_not_at_all(piper):
    final = voice._tts_cache_path("hello", voice.voice_version())
    seen = {}

    def during(tmp):
        seen["tmp"] = tmp
        seen["final_exists_mid_write"] = final.exists()

    piper.synth.during = during
    assert run(voice.synthesize("hello")) == final
    assert seen["tmp"].parent == final.parent and seen["tmp"] != final
    assert seen["final_exists_mid_write"] is False
    assert final.read_bytes() == wav_bytes(frames=400)
    assert sorted(piper.cache.iterdir()) == [final]  # no temp file left behind


@pytest.mark.parametrize("failure", ["returns_false", "raises"])
def test_failed_synthesis_leaves_no_cache_entry(piper, monkeypatch, failure):
    def broken(text, out, version):
        Path(out).write_bytes(b"RIFF half")
        if failure == "raises":
            raise RuntimeError("piper crashed")
        return False

    monkeypatch.setattr(voice, "_synth_py", broken)
    assert run(voice.synthesize("hello")) is None
    assert list(piper.cache.iterdir()) == []


def test_cache_is_scoped_to_the_voice(piper, tmp_path):
    first = run(voice.synthesize("hello"))
    assert run(voice.synthesize("hello")) == first and len(piper.synth.calls) == 1  # cache hit

    st = piper.model.stat()
    os.utime(piper.model, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # same bytes, new mtime
    after_mtime = run(voice.synthesize("hello"))
    assert after_mtime != first and len(piper.synth.calls) == 2

    os.utime(piper.model, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    piper.model.write_bytes(b"onnx-v2!")  # new size
    after_size = run(voice.synthesize("hello"))
    assert after_size not in (first, after_mtime)

    Path(f"{piper.model}.json").write_text("{}")  # the voice config changed too
    after_cfg = run(voice.synthesize("hello"))
    assert after_cfg not in (first, after_mtime, after_size)

    other = tmp_path / "other.onnx"
    other.write_bytes(piper.model.read_bytes())
    os.utime(other, ns=(piper.model.stat().st_atime_ns, piper.model.stat().st_mtime_ns))
    config.PIPER_MODEL = str(other)  # restored by the fixture's monkeypatch
    assert run(voice.synthesize("hello")) not in (first, after_mtime, after_size, after_cfg)
    assert run(voice.synthesize("goodbye")) != run(voice.synthesize("hello"))


def test_resident_piper_voice_is_reused_until_the_voice_changes(piper, monkeypatch):
    loads = []

    class FakePiperVoice:
        @classmethod
        def load(cls, path):
            loads.append(path)
            return cls()

        def synthesize_wav(self, text, wf):
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x01" * 2000)

    fake = types.ModuleType("piper")
    fake.PiperVoice = FakePiperVoice
    monkeypatch.setitem(sys.modules, "piper", fake)
    monkeypatch.setattr(voice, "_synth_py", _real_synth_py)  # the real one, with a fake piper
    assert run(voice.synthesize("one")) and run(voice.synthesize("two"))
    assert len(loads) == 1  # loaded once, reused
    st = piper.model.stat()
    os.utime(piper.model, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert run(voice.synthesize("one"))
    assert len(loads) == 2  # new voice on disk -> reloaded



# ------------------------------------------------------------------ voice_probe
def test_word_error_rate():
    assert voice_probe.word_error_rate("pick up the beaker", "Pick up the beaker.") == 0
    assert voice_probe.word_error_rate("a b c d", "a x c") == 0.5
    assert voice_probe.word_error_rate("a", "a b c") == 2.0
    assert voice_probe.word_error_rate("hello", "[BLANK_AUDIO] Hello!") == 0
    assert voice_probe.word_error_rate("", "anything") is None


def test_word_error_rate_keeps_real_words_in_parentheses_and_any_script():
    assert voice_probe.word_error_rate("pick (up) the beaker", "pick the beaker") == 0.25  # "(up)" is a word
    assert voice_probe.word_error_rate("pick up the beaker", "(upbeat music) pick up the beaker (laughs)") == 0
    assert voice_probe.word_error_rate("pick up the beaker", "[ Silence ] pick up the beaker") == 0
    assert voice_probe.words("Ouvre la pince, café crème!") == ["ouvre", "la", "pince", "café", "crème"]
    assert voice_probe.word_error_rate("le café", "le cafe") == 0.5  # an accent is a different word
    assert voice_probe.words("don't stop") == ["don't", "stop"]


def _clips(tmp_path, **clips):
    folder = tmp_path / "clips"
    folder.mkdir()
    for name, (audio, ref) in clips.items():
        (folder / f"{name}.wav").write_bytes(audio)
        if ref is not None:
            (folder / f"{name}.txt").write_text(ref + "\n", encoding="utf-8")
    return folder


def test_probe_reports_what_stt_heard(stt, tmp_path, capsys):
    folder = _clips(tmp_path, a=(wav_bytes(), "pick up the beaker"), b=(wav_bytes(rate=44100), None))
    stt.heard = "Pick up a beaker."
    code = voice_probe.main([str(folder), "--out", str(tmp_path / "probe.json")])
    report = json.loads(capsys.readouterr().out)
    assert report == json.loads((tmp_path / "probe.json").read_text())
    a, b = report["clips"]
    assert a["transcript"] == "Pick up a beaker." and a["reference"] == "pick up the beaker"
    assert a["wer"] == 0.25 and a["conversion"] == "native" and a["latency_s"] >= 0
    assert b["transcript"] == "Pick up a beaker." and b["wer"] is None and b["conversion"] == "ffmpeg"
    assert stt.tools() == ["whisper-cli", "ffmpeg", "whisper-cli"]  # the server's own STT path ran
    assert report["summary"]["transcribed"] == 2 and report["summary"]["mean_wer"] == 0.25 and code == 0


def test_probe_never_fills_in_the_reference(stt, tmp_path, capsys, monkeypatch):
    folder = _clips(tmp_path, a=(wav_bytes(), "pick up the beaker"), b=(wav_bytes(), "pick up the beaker"))
    stt.heard = ""  # whisper heard nothing
    monkeypatch.setattr(voice_probe.voice, "transcribe", _fail_second(voice.transcribe))
    code = voice_probe.main([str(folder)])
    a, b = json.loads(capsys.readouterr().out)["clips"]
    assert a["ok"] and a["transcript"] == "" and a["wer"] == 1.0
    assert not b["ok"] and b["transcript"] is None and b["wer"] is None and "not installed" in b["reason"]
    assert code == 1


def _fail_second(real):
    calls = []

    async def transcribe(audio, suffix, details=None):
        calls.append(1)
        if len(calls) == 2:
            return None, "STT not installed: {}"
        return await real(audio, suffix, details)

    return transcribe


def test_probe_imports_no_robot_code(tmp_path):
    code = ("import sys; sys.path.insert(0, 'scripts'); import voice_probe; "
            "bad = [m for m in sys.modules if m.split('.')[0] in "
            "('engram', 'engram_robot', 'robot_agent', 'perception', 'vaylt', 'commands', 'skills') "
            "or m.startswith('console.server.') and m not in ('console.server.config', 'console.server.voice')]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ------------------------------------------------------------------ bounded admission (review fix 1)
def test_stt_queue_refuses_jobs_past_the_limit(stt, monkeypatch):
    monkeypatch.setattr(config, "STT_MAX_PENDING", 2)
    stt.gate = threading.Event()

    async def burst():
        held = [asyncio.ensure_future(voice.transcribe(wav_bytes(), ".wav")) for _ in range(2)]
        await asyncio.sleep(0.1)  # both admitted: one running (held), one waiting
        third = await voice.transcribe(wav_bytes(), ".wav")  # refused at once, never queued
        stt.gate.set()
        return third, await asyncio.gather(*held)

    third, held = run(burst())
    assert third[0] is None and third[1].startswith("busy:") and "limit 2" in third[1]
    assert [t for t, _ in held] == ["pick up the beaker"] * 2
    assert stt.tools() == ["whisper-cli"] * 2
    assert run(voice.transcribe(wav_bytes(), ".wav"))[0] == "pick up the beaker"  # slots come back
    assert (voice._STT.jobs, voice._STT.bytes) == (0, 0)

def test_a_cancelled_request_keeps_its_slot_until_its_process_exits(stt, monkeypatch):
    monkeypatch.setattr(config, "STT_MAX_PENDING", 1)
    stt.gate = threading.Event()

    async def scenario():
        first = asyncio.ensure_future(voice.transcribe(wav_bytes(), ".wav"))
        await asyncio.sleep(0.1)  # running, held at the gate
        first.cancel()  # the iPad gave up; whisper-cli is still running
        await asyncio.sleep(0.05)
        while_running = await voice.transcribe(wav_bytes(), ".wav")
        stt.gate.set()
        for _ in range(100):  # wait for the abandoned process to finish
            if voice._STT.jobs == 0:
                break
            await asyncio.sleep(0.02)
        return while_running, await voice.transcribe(wav_bytes(), ".wav")

    while_running, after = run(scenario())
    assert while_running[0] is None and while_running[1].startswith("busy:")
    assert after[0] == "pick up the beaker"


class _Clip:
    """Stands in for a clip's audio; unlike bytes it can be watched with a weakref."""

    def __init__(self, n):
        self.n = n


def test_cancel_and_resubmit_cannot_get_past_the_limits():
    q = voice._SpeechQueue("test", lambda: 2, lambda: 100)
    gate = threading.Event()
    dropped = {}  # clip number -> (jobs, bytes) still counted at the moment its audio was let go

    def clip(n):
        c = _Clip(n)
        weakref.finalize(c, lambda: dropped.setdefault(n, (q.jobs, q.bytes)))
        return c

    def job(c):
        gate.wait(2)
        return c.n

    async def scenario():
        running = asyncio.ensure_future(q.run(job, clip(0), size=40))
        await asyncio.sleep(0.05)  # running, held at the gate
        for i in range(1, 30):  # the iPad gives up and asks again, over and over
            waiting = asyncio.ensure_future(q.run(job, clip(i), size=40))
            await asyncio.sleep(0.01)
            assert (q.jobs, q.bytes, len(q._waiting)) == (2, 80, 1)
            with pytest.raises(voice.VoiceBusy):
                await q.run(job, _Clip(-1), size=40)
            waiting.cancel()
            await asyncio.sleep(0.01)
            assert (q.jobs, q.bytes, len(q._waiting)) == (1, 40, 0)
            del waiting  # the caller lets go of its cancelled request too
        gc.collect()
        # every cancelled clip is gone, and went before its slot was given back
        assert dropped == {i: (2, 80) for i in range(1, 30)}
        gate.set()
        return await running

    assert run(scenario()) == 0
    gc.collect()
    assert dropped[0] == (1, 40)  # the finished job, too, let go of its audio before its slot
    assert (q.jobs, q.bytes) == (0, 0)


class JobFailed(RuntimeError):
    pass


def _with_gc_off(fn):
    """Run fn with the cyclic garbage collector off: anything freed in here was freed by
    reference counting alone, not by a later GC pass."""
    was_on = gc.isenabled()
    gc.disable()
    try:
        return fn()
    finally:
        if was_on:
            gc.enable()


def _freed_soon(freed, seconds=1.0):
    deadline = time.monotonic() + seconds
    while not freed and time.monotonic() < deadline:
        time.sleep(0.01)
    return bool(freed)


def _fail_with(clip):
    raise ValueError("bad sample")  # this frame, in a traceback, holds the clip


@pytest.mark.parametrize("chained", ["plain", "context", "cause"])
def test_a_failed_job_lets_go_of_its_audio_without_waiting_for_gc(chained):
    q = voice._SpeechQueue("test", lambda: 1)
    freed = []

    def job(clip):  # this frame, in the traceback, holds the clip
        if chained == "plain":
            raise JobFailed("whisper fell over")
        try:
            _fail_with(clip)
        except ValueError as inner:  # the inner error's traceback holds the clip too
            if chained == "cause":
                raise JobFailed("whisper fell over") from inner
            raise JobFailed("whisper fell over")

    def clip():
        c = _Clip(1)
        weakref.finalize(c, freed.append, True)
        return c

    async def scenario():
        try:
            await q.run(job, clip())
        except JobFailed as e:
            return type(e), str(e), list(getattr(e, "__notes__", [])), bool(freed)

    (kind, message, notes, freed_on_arrival), freed_after = _with_gc_off(lambda: (run(scenario()), _freed_soon(freed)))
    assert freed_on_arrival and freed_after  # gone by the time the caller sees the error
    assert (kind, message) == (JobFailed, "whisper fell over")  # the caller sees the same error
    if sys.version_info >= (3, 11):
        assert any("in job" in note for note in notes)  # and where it failed, as text
    assert (q.jobs, q.bytes) == (0, 0)


def test_a_failed_stt_job_lets_go_of_the_upload_without_waiting_for_gc(stt):
    stt.time_out_on = {1}  # whisper-cli times out
    freed = []

    def upload():  # array, unlike bytes, can be watched with a weakref
        audio = array.array("B", wav_bytes())
        weakref.finalize(audio, freed.append, True)
        return audio

    async def scenario():
        try:
            await voice.transcribe(upload(), ".wav")
        except subprocess.TimeoutExpired as e:
            return type(e), str(e)

    outcome, freed_after = _with_gc_off(lambda: (run(scenario()), _freed_soon(freed)))
    assert outcome == (subprocess.TimeoutExpired, "Command '" + str(stt.calls[0][1]) + "' timed out after 40 seconds")
    assert freed_after  # nothing in the queue, its worker or the failed job keeps the upload alive
    assert (voice._STT.jobs, voice._STT.bytes) == (0, 0)


def test_cancelled_waiting_stt_request_gives_back_its_slot_and_never_runs(stt, monkeypatch):
    clip = wav_bytes()
    monkeypatch.setattr(config, "STT_MAX_PENDING", 2)
    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 2 * len(clip))
    stt.gate = threading.Event()

    async def scenario():
        running = asyncio.ensure_future(voice.transcribe(clip, ".wav"))
        await asyncio.sleep(0.05)
        for _ in range(10):
            waiting = asyncio.ensure_future(voice.transcribe(clip, ".wav"))
            await asyncio.sleep(0.01)
            assert (voice._STT.jobs, voice._STT.bytes) == (2, 2 * len(clip))
            assert (await voice.transcribe(clip, ".wav"))[1].startswith("busy:")
            waiting.cancel()
            await asyncio.sleep(0.01)
            assert (voice._STT.jobs, voice._STT.bytes, len(voice._STT._waiting)) == (1, len(clip), 0)
        stt.gate.set()
        return await running

    assert run(scenario())[0] == "pick up the beaker"
    assert stt.tools() == ["whisper-cli"]  # the cancelled requests never ran
    assert (voice._STT.jobs, voice._STT.bytes) == (0, 0)


def test_stt_queue_caps_the_audio_it_holds(stt, monkeypatch):
    clip = wav_bytes()
    monkeypatch.setattr(config, "STT_MAX_PENDING", 10)
    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 2 * len(clip) + len(clip) // 2)
    stt.gate = threading.Event()

    async def burst():
        held = [asyncio.ensure_future(voice.transcribe(clip, ".wav")) for _ in range(2)]
        await asyncio.sleep(0.1)
        third = await voice.transcribe(clip, ".wav")  # would push the held audio past the budget
        stt.gate.set()
        return third, await asyncio.gather(*held)

    third, held = run(burst())
    assert third[0] is None and third[1].startswith("busy:") and "bytes" in third[1]
    assert [t for t, _ in held] == ["pick up the beaker"] * 2
    text, why = run(voice.transcribe(clip * 3, ".wav"))  # bigger than the whole budget on its own
    assert text is None and why.startswith("audio too large")
    assert stt.tools() == ["whisper-cli"] * 2
    assert (voice._STT.jobs, voice._STT.bytes) == (0, 0)


def test_tts_queue_refuses_jobs_past_the_limit(piper, monkeypatch):
    monkeypatch.setattr(config, "TTS_MAX_PENDING", 1)
    piper.synth.gate = threading.Event()

    async def burst():
        held = asyncio.ensure_future(voice.synthesize("one"))
        await asyncio.sleep(0.1)
        with pytest.raises(voice.VoiceBusy, match="busy"):
            await voice.synthesize("two")
        piper.synth.gate.set()
        return await held

    first = run(burst())
    assert first.exists() and len(piper.synth.calls) == 1

    async def cache_hit_while_full():
        piper.synth.gate = threading.Event()
        held = asyncio.ensure_future(voice.synthesize("three"))
        await asyncio.sleep(0.1)
        cached = await voice.synthesize("one")  # a cached reply is not a job; served even when full
        piper.synth.gate.set()
        await held
        return cached

    assert run(cache_hit_while_full()) == first
    assert (voice._TTS.jobs, voice._TTS.bytes) == (0, 0)


def _console_web_deps():
    import importlib.util

    return any(importlib.util.find_spec(m) for m in ("python_multipart", "multipart"))


@pytest.mark.skipif(not _console_web_deps(), reason="python-multipart (console/requirements.txt) not installed")
def test_busy_endpoints_answer_503(monkeypatch):
    from fastapi.testclient import TestClient

    from console.server import main

    async def busy_tts(text):
        raise voice.VoiceBusy("busy: 8 speech jobs already waiting or running (limit 8)")

    async def busy_stt(audio, suffix, **kwargs):
        return None, "busy: 4 speech jobs already waiting or running (limit 4)"

    monkeypatch.setattr(main.voice, "synthesize", busy_tts)
    monkeypatch.setattr(main.voice, "transcribe", busy_stt)
    client = TestClient(main.app)
    r = client.get("/tts", params={"text": "hello"})
    assert r.status_code == 503 and r.json()["detail"].startswith("busy:")
    r = client.post("/stt", files={"audio": ("a.wav", wav_bytes(), "audio/wav")}, data={"auto": "1"})
    assert r.status_code == 503
    assert r.json() == {"ok": False, "reason": "busy: 4 speech jobs already waiting or running (limit 4)"}


@pytest.mark.skipif(not _console_web_deps(), reason="python-multipart (console/requirements.txt) not installed")
def test_stt_upload_is_read_bounded_and_oversize_is_refused(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.datastructures import UploadFile

    from console.server import main

    reads, real_read = [], UploadFile.read

    async def spy(self, size=-1):
        data = await real_read(self, size)
        reads.append((size, len(data)))
        return data

    async def never(audio, suffix, **kwargs):
        pytest.fail("an oversized upload reached the STT queue")

    monkeypatch.setattr(UploadFile, "read", spy)
    monkeypatch.setattr(main.voice, "transcribe", never)
    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 4096)
    # within the request cap (so the endpoint, not the upload guard, sees it), over the audio limit
    r = TestClient(main.app).post("/stt", files={"audio": ("a.wav", b"\x00" * 10_000, "audio/wav")}, data={"auto": "0"})
    assert r.status_code == 413 and r.json() == {"ok": False, "reason": "audio too large: over 4096 bytes"}
    assert reads == [(4097, 4097)]  # one bounded read; the rest of the upload is never pulled into memory

# ------------------------------------------------------------------ /stt upload guard + reservations (review fix 10)
needs_web = pytest.mark.skipif(not _console_web_deps(), reason="python-multipart (console/requirements.txt) not installed")
BOUNDARY = "vaylt-test-boundary"
FORM_TYPE = (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())


def multipart(audio: bytes, auto: str = "0") -> bytes:
    return (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="auto"\r\n\r\n{auto}\r\n'
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="audio"; filename="a.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n").encode() + audio + f"\r\n--{BOUNDARY}--\r\n".encode()


def _asgi(app, headers, chunks, path="/stt", method="POST", hang_up=False):
    """Drive the ASGI app directly, the way a server would. Returns (status, body, pulled):
    pulled counts the receive() calls the app made and the body bytes it took."""
    pulled = {"calls": 0, "bytes": 0}
    chunks = iter(chunks)
    upcoming = [next(chunks, None)]

    async def receive():
        pulled["calls"] += 1
        chunk = upcoming[0]
        if chunk is None:
            return {"type": "http.disconnect"}
        upcoming[0] = next(chunks, None)
        pulled["bytes"] += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": upcoming[0] is not None or hang_up}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": list(headers), "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000)}
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"), pulled


def _queue_is_empty():
    return (voice._STT.jobs, voice._STT.bytes) == (0, 0)


@needs_web
def test_stt_content_length_over_the_cap_is_refused_unread(monkeypatch):
    from console.server import main

    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 4096)
    body = multipart(b"\x00" * 50_000)
    assert len(body) > main.stt_upload_cap()
    status, out, pulled = _asgi(main.app, [FORM_TYPE, (b"content-length", str(len(body)).encode())], [body])
    assert status == 413 and json.loads(out) == {"ok": False, "reason": "audio too large: over 4096 bytes"}
    assert pulled == {"calls": 0, "bytes": 0}  # the app never pulled a byte of the body
    assert _queue_is_empty()


@needs_web
def test_a_chunked_stt_upload_is_cut_off_just_past_the_cap(monkeypatch):
    from console.server import main

    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 4096)
    cap, chunk = main.stt_upload_cap(), 1024
    body = multipart(b"\x00" * (3 * cap))
    status, out, pulled = _asgi(main.app, [FORM_TYPE, (b"transfer-encoding", b"chunked")],
                                (body[i:i + chunk] for i in range(0, len(body), chunk)))
    assert status == 413 and json.loads(out)["reason"] == "audio too large: over 4096 bytes"
    assert cap < pulled["bytes"] <= cap + chunk  # cut off at the first chunk past the cap
    assert _queue_is_empty()  # its reservation was released


@needs_web
def test_the_upload_guard_leaves_other_routes_alone(monkeypatch):
    from console.server import main

    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 4096)
    huge = (b"content-length", str(10 ** 9).encode())
    assert _asgi(main.app, [huge], [b""], path="/no-such-route")[0] == 404
    assert _asgi(main.app, [huge], [b""], method="PUT")[0] == 405


@needs_web
def test_concurrent_uploads_never_hold_more_than_the_queue_limit(stt, monkeypatch):
    import httpx
    from starlette import formparsers

    from console.server import main

    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 100_000)
    monkeypatch.setattr(config, "STT_MAX_PENDING", 10)  # only the byte limit binds here
    held = []

    class Watched(voice._SpeechQueue):  # records every value the queue's byte count takes
        def __setattr__(self, name, value):
            if name == "bytes":
                held.append(value)
            super().__setattr__(name, value)

    monkeypatch.setattr(voice, "_STT", Watched("stt", lambda: config.STT_MAX_PENDING,
                                               lambda: config.STT_MAX_PENDING_BYTES))
    parsed, real_parse = [], formparsers.MultiPartParser.parse

    async def counted_parse(self):
        parsed.append(1)
        return await real_parse(self)

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", counted_parse)
    stt.gate = threading.Event()  # whisper holds the first job, so the second waits in the queue
    clip = wav_bytes(frames=20_000)  # 40 044 bytes: two fit in 100 000, a third does not

    async def burst():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://console") as c:
            asyncio.get_running_loop().call_later(0.5, stt.gate.set)
            return await asyncio.gather(*(c.post("/stt", files={"audio": ("a.wav", clip, "audio/wav")},
                                                 data={"auto": "0"}) for _ in range(5)))

    replies = run(burst())
    assert sorted(r.status_code for r in replies) == [200, 200, 503, 503, 503]
    assert all(r.json()["reason"].startswith("busy:") for r in replies if r.status_code == 503)
    assert max(held) <= 100_000  # reserved and queued audio never passed the aggregate limit
    assert len(parsed) == 2  # the refused uploads were never parsed, spooled or read
    assert _queue_is_empty()  # released after success


@needs_web
def test_reservations_are_released_after_failure_refusal_and_disconnect(stt, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from console.server import main

    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 100_000)
    client = TestClient(main.app, raise_server_exceptions=False)
    upload = {"files": {"audio": ("a.wav", wav_bytes(), "audio/wav")}, "data": {"auto": "0"}}

    stt.time_out_on = {1}  # whisper-cli times out: the request fails with 500
    assert client.post("/stt", **upload).status_code == 500 and _queue_is_empty()

    monkeypatch.setattr(config, "WHISPER_MODEL", str(tmp_path / "missing.bin"))  # STT answers "not installed"
    assert client.post("/stt", **upload).status_code == 503 and _queue_is_empty()

    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 2000)  # the clip is bigger than one may be
    assert client.post("/stt", **upload).status_code == 413 and _queue_is_empty()

    broken = (b"--" + BOUNDARY.encode() + b"\r\nnot a part header\r\n")  # malformed multipart
    status, _, _ = _asgi(main.app, [FORM_TYPE, (b"content-length", str(len(broken)).encode())], [broken])
    assert status == 400 and _queue_is_empty()

    body = multipart(wav_bytes())  # the client hangs up half way through its upload
    status, _, pulled = _asgi(main.app, [FORM_TYPE, (b"content-length", str(len(body)).encode())],
                              [body[: len(body) // 2]], hang_up=True)
    assert pulled["bytes"] == len(body) // 2 and _queue_is_empty()


@needs_web
def test_an_upload_longer_than_its_content_length_is_not_read_past_its_reservation(stt, monkeypatch):
    from starlette.datastructures import UploadFile

    from console.server import main

    reads, real_read = [], UploadFile.read

    async def spy(self, size=-1):
        data = await real_read(self, size)
        reads.append((size, len(data)))
        return data

    monkeypatch.setattr(UploadFile, "read", spy)
    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 100_000)
    body = multipart(wav_bytes(frames=20_000))  # 40 KB of audio, declared as 1000 bytes
    status, out, _ = _asgi(main.app, [FORM_TYPE, (b"content-length", b"1000")], [body])
    assert status == 413 and json.loads(out)["reason"] == "audio too large: over 1000 bytes"
    assert reads == [(1001, 1001)]  # read no further than the 1000 bytes reserved, plus one
    assert stt.calls == [] and _queue_is_empty()


def test_a_reservation_holds_room_until_its_job_takes_it_or_it_is_released():
    q = voice._SpeechQueue("test", lambda: 2, lambda: 100)
    r = q.reserve(60)
    assert (q.jobs, q.bytes) == (1, 60)
    with pytest.raises(voice.VoiceBusy):
        q.reserve(50)  # 110 bytes would pass the limit
    r.trim(30)
    assert (q.jobs, q.bytes) == (1, 30)
    with pytest.raises(ValueError):
        r.trim(40)  # a reservation never grows
    assert run(q.run(lambda x: x * 2, 21, size=25, reservation=r)) == 42
    assert (q.jobs, q.bytes) == (0, 0)  # the job gave the room back when it finished
    r.release()  # spent: releasing it changes nothing
    assert (q.jobs, q.bytes) == (0, 0)
    with pytest.raises(ValueError):
        run(q.run(lambda: None, reservation=r))  # and it cannot be used twice
    small = q.reserve(10)
    with pytest.raises(ValueError):
        run(q.run(lambda: None, size=11, reservation=small))  # too small for the job
    small.release()
    small.release()  # releasing twice gives the room back once
    assert (q.jobs, q.bytes) == (0, 0)
    with q.reserve(40):
        assert (q.jobs, q.bytes) == (1, 40)
    assert (q.jobs, q.bytes) == (0, 0)  # a with-block releases it


def test_a_reserved_job_cancelled_while_it_waits_gives_its_room_back():
    q = voice._SpeechQueue("test", lambda: 2, lambda: 100)
    gate = threading.Event()

    async def scenario():
        running = asyncio.ensure_future(q.run(gate.wait, 2, size=30))
        await asyncio.sleep(0.05)
        waiting = asyncio.ensure_future(q.run(lambda: "never", size=20, reservation=q.reserve(40)))
        await asyncio.sleep(0.05)
        assert (q.jobs, q.bytes) == (2, 50)  # the waiting job holds its trimmed reservation
        waiting.cancel()
        await asyncio.sleep(0.05)
        held_after_cancel = (q.jobs, q.bytes)
        gate.set()
        await running
        return held_after_cancel

    assert run(scenario()) == (1, 30)
    assert (q.jobs, q.bytes) == (0, 0)


@pytest.mark.skipif(not _console_web_deps(), reason="python-multipart (console/requirements.txt) not installed")
def test_stt_handler_drops_the_clip_before_running_the_command(monkeypatch):
    import inspect

    from fastapi.testclient import TestClient

    from console.server import main

    seen = {}

    async def fake_transcribe(data, suffix, **kw):
        return "open the gripper", "ok"

    async def slow_command(text, source):  # runs while /stt awaits it: look at the handler's frame
        caller = inspect.currentframe().f_back
        seen["handler"] = caller.f_code.co_name
        seen["holds_clip"] = "data" in caller.f_locals
        return {"ok": True}

    monkeypatch.setattr(main.voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(main, "handle_command", slow_command)
    r = TestClient(main.app).post("/stt", files={"audio": ("a.wav", wav_bytes(), "audio/wav")}, data={"auto": "1"})
    assert r.status_code == 200 and r.json()["text"] == "open the gripper"
    assert seen == {"handler": "stt", "holds_clip": False}


# ------------------------------------------------------------------ server deadline (review fix 2)
@pytest.mark.parametrize("raw,expected", [
    ("inf", 30.0), ("-inf", 30.0), ("nan", 30.0), ("-5", 30.0), ("0", 30.0), ("1e9", 30.0),
    ("120.5", 30.0), ("soon", 30.0), ("", 30.0), ("0.5", 0.5), ("45", 45.0), ("120", 120.0),
])
def test_whisper_server_timeout_is_validated(monkeypatch, raw, expected):
    monkeypatch.setenv("ENGRAM_WHISPER_SERVER_TIMEOUT_S", raw)
    assert config._seconds("ENGRAM_WHISPER_SERVER_TIMEOUT_S", 30.0, 120.0) == expected


@pytest.mark.parametrize("raw,expected", [("0", 4), ("-3", 4), ("65", 4), ("1.5", 4), ("lots", 4), ("1", 1), ("64", 64)])
def test_queue_limits_are_validated(monkeypatch, raw, expected):
    monkeypatch.setenv("ENGRAM_STT_MAX_PENDING", raw)
    assert config._count("ENGRAM_STT_MAX_PENDING", 4, 64) == expected


@pytest.mark.parametrize("bad", ["inf", "nan", "-5", "1e9"])
def test_bad_settings_fall_back_to_defaults_at_startup(bad):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM_")}
    env.update(ENGRAM_WHISPER_SERVER_TIMEOUT_S=bad, ENGRAM_STT_MAX_PENDING=bad, ENGRAM_STT_MAX_SECONDS=bad,
               ENGRAM_STT_MAX_PENDING_BYTES=bad, ENGRAM_TTS_MAX_PENDING=bad)
    code = ("from console.server import config as c; "
            "print(c.WHISPER_SERVER_TIMEOUT_S, c.STT_MAX_PENDING, c.STT_MAX_PENDING_BYTES, c.TTS_MAX_PENDING, "
            "c.STT_MAX_SECONDS)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["30.0", "4", str(32 * 1024 * 1024), "8", "30.0"]


def test_a_server_that_never_finishes_answering_hits_the_overall_deadline(stt, whisper_server, monkeypatch):
    whisper_server["mode"] = "trickle"  # 30 bytes, one every 0.1 s: about 3 s in all
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", whisper_server["url"])
    monkeypatch.setattr(config, "WHISPER_SERVER_TIMEOUT_S", 0.5)
    started = time.monotonic()
    text, why = run(voice.transcribe(wav_bytes(), ".wav"))
    assert text == "pick up the beaker" and "no answer within 0.5 s" in why
    assert stt.tools() == ["whisper-cli"]
    assert time.monotonic() - started < 2.0


# ------------------------------------------------------------------ probe keeps going (review fix 3)
def test_probe_records_an_stt_timeout_and_keeps_going(stt, tmp_path, capsys):
    folder = _clips(tmp_path, a=(wav_bytes(), "pick up the beaker"), b=(wav_bytes(), "pick up the beaker"),
                    c=(wav_bytes(), None))
    stt.time_out_on = {2}  # whisper-cli times out on the second clip
    code = voice_probe.main([str(folder)])
    report = json.loads(capsys.readouterr().out)
    a, b, c = report["clips"]
    assert a["ok"] and c["ok"] and a["transcript"] == c["transcript"] == "pick up the beaker"
    assert not b["ok"] and b["transcript"] is None and b["wer"] is None and "TimeoutExpired" in b["reason"]
    assert b["latency_s"] >= 0
    assert report["summary"]["transcribed"] == 2 and report["summary"]["failed"] == 1
    assert code == 1


# ------------------------------------------------------------------ decoded length (review fix 4)
def test_long_clip_is_cut_short_by_ffmpeg_and_refused(stt, monkeypatch):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    stt.decoded_seconds = 600  # a few bytes of compressed audio that decode to ten minutes
    text, why = run(voice.transcribe(b"\x1aE\xdf\xa3 small but long webm", ".webm"))
    assert (text, why) == (None, "audio too long: 3.0 s (limit 2 s)")
    ((_, cmd),) = stt.calls
    assert cmd[cmd.index("-t") + 1] == "3" and int(cmd[cmd.index("-fs") + 1]) == voice.wav_byte_limit()
    assert cmd[cmd.index("-map_metadata") + 1] == "-1"  # metadata is not copied into the output
    assert stt.ffmpeg_wrote <= voice.wav_byte_limit()  # ffmpeg stopped at -t
    assert stt.tools() == ["ffmpeg"]  # whisper never ran


@pytest.mark.parametrize("native", [True, False])
def test_a_clip_right_at_the_limit_is_transcribed(stt, monkeypatch, native):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    if native:
        audio = wav_bytes(frames=32000)  # 2.0 s
    else:
        stt.decoded_seconds, audio = 2.0, wav_bytes(rate=44100)
    assert run(voice.transcribe(audio, ".wav"))[0] == "pick up the beaker"


def test_a_native_wav_over_the_limit_is_refused(stt, monkeypatch):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    text, why = run(voice.transcribe(wav_bytes(frames=40000), ".wav"))  # 2.5 s, needs no ffmpeg
    assert (text, why) == (None, "audio too long: 2.5 s (limit 2 s)")
    assert stt.calls == []


@pytest.mark.parametrize("decoded_seconds,extra_bytes", [(60, 0), (1, 4 * 1024 * 1024)],
                         ids=["converter-ignores-t", "header-undercounts-file"])
def test_an_oversized_wav_is_refused_before_anything_reads_it(stt, whisper_server, monkeypatch,
                                                              decoded_seconds, extra_bytes):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", whisper_server["url"])
    stt.decoded_seconds, stt.obeys_t, stt.extra_bytes = decoded_seconds, False, extra_bytes
    reads = []
    real_read_bytes = Path.read_bytes

    def spy(self):
        reads.append(self.stat().st_size)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    text, why = run(voice.transcribe(wav_bytes(rate=44100), ".wav"))
    assert (text, why) == (None, f"file too large: {stt.ffmpeg_wrote} bytes (limit {voice.wav_byte_limit()} bytes)")
    assert stt.ffmpeg_wrote > voice.wav_byte_limit()  # the converter really did misbehave
    assert reads and max(reads) <= voice.wav_byte_limit()  # and nothing read that file
    assert whisper_server["requests"] == [] and stt.tools() == ["ffmpeg"]

def test_a_converter_that_writes_the_wrong_format_is_refused(stt):
    stt.decode = lambda cmd: wav_bytes(rate=44100, channels=2)  # not what whisper reads
    text, why = run(voice.transcribe(wav_bytes(rate=8000), ".wav"))
    assert (text, why) == (None, "audio is not 16 kHz mono 16-bit PCM after conversion")
    assert stt.tools() == ["ffmpeg"]


# ------------------------------------------------------------------ native = the fmt chunk (review fix 6)
PCM_1S = b"\x10\x20" * 16000  # 1 s of 16-bit mono samples at 16 kHz


def riff(*chunks, data_size=None):
    """A RIFF/WAVE file from (id, body) chunks, each padded to an even size."""
    body = b"WAVE"
    for cid, data in chunks:
        size = data_size if cid == b"data" and data_size is not None else len(data)
        body += cid + struct.pack("<I", size) + data + (b"\0" if len(data) % 2 else b"")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def fmt(tag=1, channels=1, rate=16000, align=2, bits=16, extra=b""):
    return b"fmt ", struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits) + extra


def info_list(size):
    """A LIST/INFO chunk holding a comment of `size` bytes: metadata, not audio."""
    return b"LIST", b"INFO" + b"ICMT" + struct.pack("<I", size) + b"x" * size


def with_riff_size(clip, riff_size):
    return clip[:4] + struct.pack("<I", riff_size) + clip[8:]


# 1 s of audio in a plain WAV: a 44-byte header whose sizes are exactly true
PLAIN_HEADER_1S = (b"RIFF" + struct.pack("<I", 36 + 32000) + b"WAVE" + b"fmt "
                   + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", 32000))
PLAIN_1S = PLAIN_HEADER_1S + PCM_1S


PCM_GUID = bytes.fromhex("0100000000001000800000aa00389b71")  # KSDATAFORMAT_SUBTYPE_PCM
FLOAT_GUID = bytes.fromhex("0300000000001000800000aa00389b71")  # KSDATAFORMAT_SUBTYPE_IEEE_FLOAT


def extensible(valid_bits=16, subtype=PCM_GUID, cb_size=22):
    """A WAVE_FORMAT_EXTENSIBLE fmt chunk for 16 kHz mono in 16-bit containers (front-centre)."""
    return fmt(tag=0xFFFE, extra=struct.pack("<HHI", cb_size, valid_bits, 4) + subtype)


EXTENSIBLE = extensible()  # cbSize 22, 16 valid bits, PCM subtype: native audio


def test_12_bit_audio_in_2_byte_samples_is_converted(stt, tmp_path):
    twelve = riff(fmt(bits=12), (b"data", PCM_1S))
    clip = tmp_path / "twelve.wav"
    clip.write_bytes(twelve)
    with wave.open(str(clip), "rb") as w:  # what fooled the old check: 2 bytes per sample
        assert (w.getsampwidth(), w.getnchannels(), w.getframerate()) == (2, 1, 16000)
    assert voice._native_data(clip) is None
    text, _ = run(voice.transcribe(twelve, ".wav"))
    assert text == "pick up the beaker"
    assert stt.tools() == ["ffmpeg", "whisper-cli"] and stt.whisper_saw == CONVERTED
    assert voice_probe.probe_clip(clip)["conversion"] == "ffmpeg"


@pytest.mark.parametrize("audio", [
    riff(extensible(valid_bits=12), (b"data", PCM_1S)),  # 12 valid bits in 16-bit containers
    riff(extensible(subtype=FLOAT_GUID), (b"data", PCM_1S)),  # float, not PCM
    riff(extensible(cb_size=0), (b"data", PCM_1S)),  # cbSize says there is no extension
    riff(fmt(tag=0xFFFE, extra=b"\x00\x00"), (b"data", PCM_1S)),  # an 18-byte fmt chunk: none at all
    riff(fmt(tag=3, align=4, bits=32), (b"data", PCM_1S)),  # 32-bit float
    riff(fmt(align=4), (b"data", PCM_1S)),  # block size disagrees with 16-bit mono
    riff((b"data", PCM_1S), fmt()),  # data before any fmt chunk
    riff((b"fmt ", b"\x01\x00\x01\x00"), (b"data", PCM_1S)),  # fmt chunk too short
    riff(fmt()),  # no data chunk
    b"RIFX" + riff(fmt(), (b"data", PCM_1S))[4:],  # big-endian RIFF
], ids=["extensible-12-valid-bits", "extensible-float-subtype", "extensible-cbsize-0", "extensible-short-fmt",
        "float", "block-align", "data-first", "short-fmt", "no-data", "rifx"])
def test_wavs_that_are_not_plain_16_bit_pcm_are_converted(stt, audio):
    assert run(voice.transcribe(audio, ".wav"))[0] == "pick up the beaker"
    assert stt.tools() == ["ffmpeg", "whisper-cli"] and stt.whisper_saw == CONVERTED


@pytest.mark.parametrize("ffmpeg", ["installed", "absent"])
def test_extensible_pcm_16_is_native_and_reaches_whisper_as_a_plain_wav(stt, tmp_path, ffmpeg):
    if ffmpeg == "absent":
        stt.installed.clear()
    clip = riff(EXTENSIBLE, (b"data", PCM_1S))
    path = tmp_path / "extensible.wav"
    path.write_bytes(clip)
    assert voice._native_data(path) == (68, 32000, False)  # native audio, never sent as it is
    details = {}
    assert run(voice.transcribe(clip, ".wav", details=details))[0] == "pick up the beaker"
    assert details == {"conversion": "native-stripped"} and stt.tools() == ["whisper-cli"]  # no ffmpeg
    assert stt.whisper_saw[:44] == PLAIN_HEADER_1S and stt.whisper_saw == PLAIN_1S  # tag 1, true sizes


def test_the_fmt_chunk_is_read_bounded_whatever_size_it_declares(monkeypatch, tmp_path):
    clip = riff(fmt(), (b"data", PCM_1S))
    path = tmp_path / "huge-fmt.wav"
    path.write_bytes(clip[:16] + struct.pack("<I", 0x7FFFFFF0) + clip[20:])  # fmt chunk "is" 2 GB
    reads = []

    class Watched:
        def __init__(self, f):
            self.f = f

        def read(self, n=-1):
            reads.append(n)
            if not 0 < n <= 64 * 1024:
                raise AssertionError(f"unbounded read of {n} bytes")
            return self.f.read(n)

        def __getattr__(self, name):
            return getattr(self.f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.f.__exit__(*exc)

    monkeypatch.setattr(voice, "open", lambda *a, **k: Watched(open(*a, **k)), raising=False)
    assert voice._native_data(path) is None  # its data chunk is never reached: not native
    assert reads and max(reads) <= 40  # header-sized reads only


def test_plain_16_bit_pcm_still_skips_ffmpeg_around_other_chunks(stt):
    native = riff((b"LIST", b"odd"), fmt(), (b"fact", b"\x00\x7d\x00\x00"), (b"data", PCM_1S))
    assert run(voice.transcribe(native, ".wav"))[0] == "pick up the beaker"
    assert stt.tools() == ["whisper-cli"] and stt.whisper_saw == native


@pytest.mark.parametrize("clip", [
    riff(fmt(), (b"data", PCM_1S), data_size=0xFFFFFFFF),
    with_riff_size(riff(fmt(), (b"data", PCM_1S)), 0xFFFFFFFF),
    with_riff_size(riff(fmt(), (b"data", PCM_1S)), 36),
    riff(fmt(), (b"data", PCM_1S), info_list(10)),
    riff(fmt(), (b"data", PCM_1S), (b"data", PCM_1S)),
    riff(fmt(), (b"data", PCM_1S + b"\x01")),
], ids=["streaming-data-size", "riff-size-too-big", "riff-size-too-small", "chunk-after-data",
        "second-data-chunk", "half-a-sample"])
def test_a_native_wav_whose_header_disagrees_with_its_bytes_reaches_whisper_plain(stt, monkeypatch, tmp_path, clip):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    path = tmp_path / "clip.wav"
    path.write_bytes(clip)
    assert voice._native_data(path) == (44, 32000, False)  # 1 s of audio; the header says otherwise
    details = {}
    assert run(voice.transcribe(clip, ".wav", details=details))[0] == "pick up the beaker"
    assert details == {"conversion": "native-stripped"} and stt.tools() == ["whisper-cli"]
    assert stt.whisper_saw[:44] == PLAIN_HEADER_1S  # the header whisper reads has the true sizes
    assert stt.whisper_saw == PLAIN_1S


def test_a_native_wav_whose_header_is_exact_reaches_whisper_unchanged(stt, tmp_path):
    path = tmp_path / "plain.wav"
    path.write_bytes(PLAIN_1S)
    assert voice._native_data(path) == (44, 32000, True)
    details = {}
    assert run(voice.transcribe(PLAIN_1S, ".wav", details=details))[0] == "pick up the beaker"
    assert details == {"conversion": "native"} and stt.whisper_saw == PLAIN_1S


def test_the_whisper_server_also_gets_the_plain_file(stt, whisper_server, monkeypatch):
    monkeypatch.setattr(config, "WHISPER_SERVER_URL", whisper_server["url"])
    streamed = riff(fmt(), (b"data", PCM_1S), data_size=0xFFFFFFFF)
    assert run(voice.transcribe(streamed, ".wav"))[0] == "hello from the server"
    ((_, _, body),) = whisper_server["requests"]
    assert PLAIN_1S in body and streamed[:44] not in body


def test_a_converter_output_with_unset_sizes_reaches_whisper_plain(stt):
    stt.decode = lambda cmd: riff(fmt(), (b"data", PCM_1S), data_size=0xFFFFFFFF)
    details = {}
    assert run(voice.transcribe(wav_bytes(rate=44100), ".wav", details=details))[0] == "pick up the beaker"
    assert details == {"conversion": "ffmpeg"} and stt.whisper_saw == PLAIN_1S


def _real_ffmpeg():
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


REAL_FFMPEG = _real_ffmpeg()


@pytest.mark.skipif(not REAL_FFMPEG, reason="no real ffmpeg on this machine")
@pytest.mark.parametrize("upload,expected", [
    (wav_bytes(rate=44100, channels=2, frames=int(44100 * 1.5)), ("pick up the beaker", "ok")),
    (wav_bytes(rate=44100, channels=2, frames=44100 * 5), (None, "audio too long: 3.0 s (limit 2 s)")),
    (riff(fmt(channels=2, rate=44100, align=4), info_list(200_000), (b"data", b"\x10\x20" * 2 * 44100)),
     ("pick up the beaker", "ok")),
], ids=["1.5s-44k-stereo", "5s-44k-stereo", "1s-44k-stereo-with-200KB-metadata"])
def test_real_ffmpeg_output_passes_the_native_check(stt, monkeypatch, upload, expected):
    monkeypatch.setattr(config, "FFMPEG_BIN", REAL_FFMPEG)
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    stt.installed.add(REAL_FFMPEG)
    real_run = subprocess.run

    def procs(cmd, **kwargs):  # the real ffmpeg; whisper-cli stays the fake
        if cmd[0] == REAL_FFMPEG:
            stt.calls.append(("ffmpeg", list(cmd)))
            return real_run(cmd, **kwargs)
        return stt(cmd, **kwargs)

    monkeypatch.setattr(voice, "subprocess", types.SimpleNamespace(run=procs))
    assert run(voice.transcribe(upload, ".wav")) == expected
    if expected[0]:
        assert stt.tools() == ["ffmpeg", "whisper-cli"] and stt.whisper_saw[:4] == b"RIFF"
        assert len(stt.whisper_saw) <= voice.wav_byte_limit() and b"ICMT" not in stt.whisper_saw


# ------------------------------------------------------------------ length vs file size (review fixes 8, 9)
@pytest.mark.parametrize("ffmpeg", ["installed", "absent"])
@pytest.mark.parametrize("comment", [1000, 200_000], ids=["under-the-file-cap", "over-the-file-cap"])
def test_a_short_native_clip_with_big_metadata_never_needs_ffmpeg(stt, monkeypatch, comment, ffmpeg):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    if ffmpeg == "absent":
        stt.installed.clear()
    clip = riff(fmt(), info_list(comment), (b"data", PCM_1S))  # 1 s of audio
    over_cap = len(clip) > voice.wav_byte_limit()
    assert over_cap == (comment == 200_000)
    details = {}
    assert run(voice.transcribe(clip, ".wav", details=details))[0] == "pick up the beaker"
    assert stt.tools() == ["whisper-cli"]  # ffmpeg was never run
    if over_cap:  # the metadata is dropped here: a plain 44-byte header and the same audio
        assert details == {"conversion": "native-stripped"}
        assert stt.whisper_saw == riff(fmt(), (b"data", PCM_1S))
    else:
        assert details == {"conversion": "native"} and stt.whisper_saw == clip


def test_a_stray_odd_byte_is_not_counted_as_audio(tmp_path):
    clip = tmp_path / "odd.wav"
    clip.write_bytes(riff(fmt(), (b"data", PCM_1S + b"\x01")))  # half a sample at the end
    assert voice._native_data(clip) == (44, len(PCM_1S), False)


def test_stripping_reads_the_audio_in_bounded_blocks(stt, monkeypatch):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 5.0)
    audio = PCM_1S * 3  # 96 000 bytes: more than one copy block
    clip = riff(fmt(), info_list(300_000), (b"data", audio))
    assert len(clip) > voice.wav_byte_limit() and len(audio) > voice._COPY_BLOCK
    reads = []

    class Watched:
        def __init__(self, f):
            self.f = f

        def read(self, n=-1):
            reads.append(n)
            return self.f.read(n)

        def __getattr__(self, name):
            return getattr(self.f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.f.__exit__(*exc)

    monkeypatch.setattr(voice, "open", lambda *a, **k: Watched(open(*a, **k)), raising=False)
    details = {}
    assert run(voice.transcribe(clip, ".wav", details=details))[0] == "pick up the beaker"
    assert details == {"conversion": "native-stripped"} and stt.whisper_saw == riff(fmt(), (b"data", audio))
    assert reads and all(0 < n <= voice._COPY_BLOCK for n in reads)  # never "read it all"


def test_an_oversized_native_clip_whose_audio_is_too_long_is_refused_unread(stt, monkeypatch):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    clip = riff(fmt(), (b"data", PCM_1S * 10))  # 10 s of audio: over the length AND the file cap
    assert len(clip) > voice.wav_byte_limit()
    reads = []
    real_read_bytes = Path.read_bytes

    def spy(self):
        reads.append(self.stat().st_size)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    assert run(voice.transcribe(clip, ".wav")) == (None, "audio too long: 10.0 s (limit 2 s)")
    assert reads == [] and stt.calls == []  # judged from its chunk headers alone


def test_probe_reports_what_the_path_actually_did(stt, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "STT_MAX_SECONDS", 2.0)
    folder = _clips(tmp_path, a=(wav_bytes(), None), b=(riff(fmt(), info_list(200_000), (b"data", PCM_1S)), None),
                    c=(wav_bytes(rate=44100), None), d=(wav_bytes(frames=48000), None))
    voice_probe.main([str(folder)])
    a, b, c, d = json.loads(capsys.readouterr().out)["clips"]
    assert (a["conversion"], b["conversion"], c["conversion"]) == ("native", "native-stripped", "ffmpeg")
    assert (d["conversion"], d["reason"]) == ("native", "audio too long: 3.0 s (limit 2 s)")  # native, refused on length



def test_probe_never_reads_a_clip_over_the_queue_byte_limit(stt, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 64 * 1024)
    folder = _clips(tmp_path, a=(wav_bytes(), None))
    with open(folder / "b.wav", "wb") as f:
        f.truncate(1024 * 1024)  # 1 MiB, sparse: nothing written
    opened, sent = [], []
    real_open, real_transcribe = open, voice.transcribe

    def watched_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    async def watched_transcribe(audio, suffix, details=None):
        sent.append(len(audio))
        return await real_transcribe(audio, suffix, details)

    monkeypatch.setattr(voice_probe, "open", watched_open, raising=False)
    monkeypatch.setattr(voice_probe.voice, "transcribe", watched_transcribe)
    code = voice_probe.main([str(folder)])
    a, b = json.loads(capsys.readouterr().out)["clips"]
    assert a["ok"] and a["conversion"] == "native"
    assert (b["ok"], b["transcript"], b["conversion"]) == (False, None, None)
    assert b["reason"] == "audio too large: 1048576 bytes (limit 65536)"
    assert opened == ["a.wav"] and sent == [len(wav_bytes())]  # the big clip was never opened or sent
    assert code == 1


def test_probe_reads_at_most_one_byte_past_the_limit(stt, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "STT_MAX_PENDING_BYTES", 64 * 1024)
    folder = _clips(tmp_path, a=(wav_bytes(), None))  # small when its size is checked...
    reads, real_open = [], open

    class Watched:
        def __init__(self, f):
            self.f = f

        def read(self, n=-1):
            reads.append(n)
            return self.f.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.f.__exit__(*exc)

    def growing_open(path, *args, **kwargs):  # ...and 1 MiB bigger by the time it is opened
        with real_open(path, "ab") as f:
            f.write(bytes(1024 * 1024))
        return Watched(real_open(path, *args, **kwargs))

    monkeypatch.setattr(voice_probe, "open", growing_open, raising=False)
    voice_probe.main([str(folder)])
    (a,) = json.loads(capsys.readouterr().out)["clips"]
    size = (folder / "a.wav").stat().st_size
    assert reads == [64 * 1024 + 1]  # one bounded read, stopped just past the limit
    assert (a["transcript"], a["reason"]) == (None, f"audio too large: {size} bytes (limit 65536)")



# ------------------------------------------------------------------ probe reference (review fix 5)
def test_probe_records_a_bad_reference_and_keeps_going(stt, tmp_path, capsys):
    folder = _clips(tmp_path, a=(wav_bytes(), None), b=(wav_bytes(), None), c=(wav_bytes(), "pick up the beaker"))
    (folder / "a.txt").write_bytes(b"\xff\xfe pick up \xfa")  # not UTF-8
    (folder / "b.txt").mkdir()  # there, but cannot be read as a file
    code = voice_probe.main([str(folder)])
    report = json.loads(capsys.readouterr().out)
    a, b, c = report["clips"]
    assert "UnicodeDecodeError" in a["reference_error"] and a["reference"] is None and a["wer"] is None
    assert b["reference_error"] and b["reference"] is None and b["wer"] is None
    assert [x["transcript"] for x in (a, b, c)] == ["pick up the beaker"] * 3  # all still transcribed
    assert c["reference_error"] is None and c["wer"] == 0
    assert report["summary"]["reference_errors"] == 2 and report["summary"]["failed"] == 0 and code == 0
