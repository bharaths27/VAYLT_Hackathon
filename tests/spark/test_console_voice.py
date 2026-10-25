"""Console voice (console/server/voice.py) and the STT probe (scripts/voice_probe.py).

Hermetic: ffmpeg, whisper-cli and piper are fakes; the whisper.cpp server is a stdlib HTTP server
on 127.0.0.1 inside the test. WAV clips are made here with the wave module.
"""

import asyncio
import gc
import io
import json
import os
import socket
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
                dst.write_bytes(CONVERTED)
                self.upload_after_ffmpeg = src.read_bytes()
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            if tool == "whisper-cli":
                self.whisper_saw = Path(cmd[cmd.index("-f") + 1]).read_bytes()
                return subprocess.CompletedProcess(cmd, 0, f" {self.heard}\n".encode(), b"")
            raise AssertionError(f"unexpected process: {cmd}")
        finally:
            with self.lock:
                self.active -= 1

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
    ("http://localhost:8178/", "http://localhost:8178/inference"),
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
    assert a["wer"] == 0.25 and a["native_pcm"] is True and a["latency_s"] >= 0
    assert b["transcript"] == "Pick up a beaker." and b["wer"] is None and b["native_pcm"] is False
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

    async def transcribe(audio, suffix):
        calls.append(1)
        if len(calls) == 2:
            return None, "STT not installed: {}"
        return await real(audio, suffix)

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


def test_a_failed_job_pins_nothing_once_its_caller_is_done():
    q = voice._SpeechQueue("test", lambda: 1)
    dropped = threading.Event()

    def job(c):
        raise RuntimeError("whisper fell over")  # its traceback holds this frame, and c

    async def scenario():
        c = _Clip(1)
        weakref.finalize(c, dropped.set)
        task = asyncio.ensure_future(q.run(job, c))
        del c
        with pytest.raises(RuntimeError):
            await task

    run(scenario())
    gc.collect()
    assert dropped.is_set()  # the idle worker does not keep the failed job (or its audio)
    assert (q.jobs, q.bytes) == (0, 0)


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

    async def busy_stt(audio, suffix):
        return None, "busy: 4 speech jobs already waiting or running (limit 4)"

    monkeypatch.setattr(main.voice, "synthesize", busy_tts)
    monkeypatch.setattr(main.voice, "transcribe", busy_stt)
    client = TestClient(main.app)
    r = client.get("/tts", params={"text": "hello"})
    assert r.status_code == 503 and r.json()["detail"].startswith("busy:")
    r = client.post("/stt", files={"audio": ("a.wav", wav_bytes(), "audio/wav")}, data={"auto": "1"})
    assert r.status_code == 503
    assert r.json() == {"ok": False, "reason": "busy: 4 speech jobs already waiting or running (limit 4)"}


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
    env.update(ENGRAM_WHISPER_SERVER_TIMEOUT_S=bad, ENGRAM_STT_MAX_PENDING=bad,
               ENGRAM_STT_MAX_PENDING_BYTES=bad, ENGRAM_TTS_MAX_PENDING=bad)
    code = ("from console.server import config as c; "
            "print(c.WHISPER_SERVER_TIMEOUT_S, c.STT_MAX_PENDING, c.STT_MAX_PENDING_BYTES, c.TTS_MAX_PENDING)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["30.0", "4", str(32 * 1024 * 1024), "8"]


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
