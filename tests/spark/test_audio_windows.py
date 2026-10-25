"""Audio window: ordered, bounded, never drops the start; whisper.cpp client is loopback only."""

import io
import json
import socket
import struct
import threading
import time
import tracemalloc
import wave
from array import array
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest

from engram.spark import _http
from engram.planner import FUTURE_HINTS, NEGATION
from engram.spark.audio_windows import (
    SAMPLE_RATE, SOUND_DESCRIPTORS, SOUND_WORDS, AudioOverflow, Cancelled, CancelToken, LiveTranscriber, OutOfOrder,
    PcmWindow, SAMPLE_WIDTH, WhisperError, WhisperServerClient, WindowSealed, speech_only,
)
from engram.spark.voice import CORRECTION, STOP_WORDS
from spark_fakes import REJECTIONS, SAMPLES


def pcm(*samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def read_wav(data):
    with wave.open(io.BytesIO(data), "rb") as wf:
        return wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.readframes(wf.getnframes())


def test_snapshot_is_a_16k_mono_wav_of_everything_so_far_in_order():
    w = PcmWindow(max_seconds=1.0)
    w.append(pcm(1, 2, 3), seq=0)
    first = w.snapshot()
    w.append(pcm(4, 5), seq=1)
    assert read_wav(first) == (16000, 1, 2, pcm(1, 2, 3))
    assert read_wav(w.snapshot()) == (16000, 1, 2, pcm(1, 2, 3, 4, 5))
    assert w.duration_s == pytest.approx(5 / SAMPLE_RATE)


def test_empty_snapshot_is_still_a_valid_wav():
    assert read_wav(PcmWindow().snapshot()) == (16000, 1, 2, b"")


def test_overflow_never_drops_the_start_and_makes_the_utterance_invalid():
    w = PcmWindow(max_seconds=0.001)               # 16 samples
    w.append(pcm(*range(10)))                      # e.g. "don't"
    assert read_wav(w.snapshot())[3] == pcm(*range(10)) and not w.overflowed
    with pytest.raises(AudioOverflow):
        w.append(pcm(*range(10)))                  # the rest, e.g. "...no, never mind", does not fit
    assert w.overflowed
    with pytest.raises(AudioOverflow):              # full from now on: no hole in the middle of the utterance
        w.append(pcm(1))
    with pytest.raises(AudioOverflow):              # and the prefix that was kept is never transcribed
        w.snapshot()
    w.reset()
    assert not w.overflowed
    w.append(pcm(7))
    assert read_wav(w.snapshot())[3] == pcm(7)


def test_chunks_must_arrive_in_order_and_hold_whole_samples():
    w = PcmWindow()
    w.append(pcm(1), seq=0)
    w.append(pcm(2), seq=1)
    with pytest.raises(ValueError):
        w.append(b"\x01")
    with pytest.raises(AudioOverflow):
        w.snapshot()                                            # a refused chunk voids the utterance too


def test_empty_chunks_store_nothing_but_keep_their_sequence_number():
    w = PcmWindow(max_seconds=0.001)
    for i in range(10_000):
        w.append(b"", seq=i)
    for _ in range(10_000):
        w.append(b"")
    assert w.duration_s == 0
    w.append(pcm(1), seq=20_000)
    w.append(b"", seq=20_001)
    assert w.duration_s == pytest.approx(1 / SAMPLE_RATE) and read_wav(w.snapshot())[3] == pcm(1)
    with pytest.raises(OutOfOrder):
        w.append(b"", seq=5)                                    # even an empty chunk out of order...
    with pytest.raises(AudioOverflow):
        w.snapshot()                                            # ...means the utterance is not whole


def test_window_length_is_bounded():
    with pytest.raises(ValueError):
        PcmWindow(max_seconds=0)
    with pytest.raises(ValueError):
        PcmWindow(max_seconds=3600)


# ---- whisper.cpp server client ---------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://10.0.0.5:8080", "http://192.168.1.20:8080", "http://localhost:8080", "https://127.0.0.1:8080",
    "http://127.0.0.1:8080/other", "http://user:pw@127.0.0.1:8080", "http://example.com:8080",
])
def test_whisper_client_refuses_anything_but_a_loopback_ip(url):
    with pytest.raises(ValueError):
        WhisperServerClient(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:0", "http://[::1]:0", "http://127.0.0.1", "http://127.0.0.1:", "http://127.0.0.1:abc",
    "http://127.0.0.1:65536",
])
def test_whisper_client_needs_an_explicit_real_port(url):
    # port 0 or no port used to pass and then send the audio to port 80, where another local service may listen
    with pytest.raises(ValueError):
        WhisperServerClient(url)


def test_a_connection_takes_the_default_port_only_when_none_is_given():
    deadline = time.monotonic() + 5
    assert _http._connection("http://127.0.0.1/x", deadline)[0].port == 80
    assert _http._connection("https://127.0.0.1/x", deadline)[0].port == 443
    assert _http._connection("http://127.0.0.1:8080/x", deadline)[0].port == 8080
    for url in ("http://127.0.0.1:0/x", "https://127.0.0.1:0/x"):
        with pytest.raises(ValueError):
            _http._connection(url, deadline)   # was: port 0 silently became 80 / 443


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://127.0.0.1:8080/", "http://[::1]:9000"])
def test_whisper_client_accepts_loopback(url):
    assert WhisperServerClient(url).endpoint.endswith("/inference")


class _Whisper:
    def __init__(self, redirect=False):
        self.hits = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.hits.append((self.path, self.headers["Content-Type"], body))
                if redirect and self.path == "/inference":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                reply = json.dumps({"text": "  Open the claw [BLANK]\n"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_whisper_client_posts_the_snapshot_to_inference(monkeypatch):
    # A proxy from the environment must not take a loopback request off this machine.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    srv = _Whisper()
    try:
        w = PcmWindow()
        w.append(pcm(1, 2, 3))
        text = WhisperServerClient(f"http://127.0.0.1:{srv.port}").transcribe(w.snapshot())
        assert text == "Open the claw [BLANK]"
        path, ctype, body = srv.hits[0]
        assert path == "/inference" and ctype.startswith("multipart/form-data; boundary=")
        assert b'name="file"' in body and b'name="response_format"\r\n\r\njson' in body
        assert w.snapshot() in body
    finally:
        srv.close()


def test_whisper_client_does_not_follow_redirects():
    srv = _Whisper(redirect=True)
    try:
        with pytest.raises(WhisperError, match="302"):
            WhisperServerClient(f"http://127.0.0.1:{srv.port}").transcribe(PcmWindow().snapshot())
        assert [h[0] for h in srv.hits] == ["/inference"]
    finally:
        srv.close()


# ---- end of speech cancels the partial in flight --------------------------------------------------------------


def _no_workers_left(within_s=1.0):
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        if not any(t.name == "spark-http" and t.is_alive() for t in threading.enumerate()):
            return True
        time.sleep(0.02)
    return False


class _OneAtATimeWhisper:
    """Like whisper.cpp's server: one request at a time; the n-th request "decodes" for seconds[n] and
    stops early if its client hangs up (checked every 10 ms). Records what happened."""

    def __init__(self, seconds):
        self.seconds = list(seconds)
        self.lock = threading.Lock()          # the server's single inference slot
        self.log = []                         # (n, event, t)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers["Content-Length"]))
                with outer.lock:
                    n = len(outer.events("start"))
                    outer.log.append((n, "start", time.monotonic()))
                    end = time.monotonic() + outer.seconds[min(n, len(outer.seconds) - 1)]
                    self.connection.settimeout(0.01)
                    while time.monotonic() < end:
                        try:
                            if self.connection.recv(1) == b"":
                                outer.log.append((n, "client gone", time.monotonic()))
                                return
                        except socket.timeout:
                            pass
                        except OSError:
                            outer.log.append((n, "client gone", time.monotonic()))
                            return
                    self.connection.settimeout(None)
                    reply = json.dumps({"text": f"text {n}"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(reply)))
                    self.end_headers()
                    self.wfile.write(reply)
                    outer.log.append((n, "done", time.monotonic()))

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def events(self, kind):
        return [e for e in self.log if e[1] == kind]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_cancel_closes_the_connection_and_returns_at_once():
    srv = _OneAtATimeWhisper([3.0])
    try:
        token = CancelToken()
        threading.Timer(0.2, token.cancel).start()
        t0 = time.monotonic()
        with pytest.raises(Cancelled):
            WhisperServerClient(srv.url, timeout_s=10).transcribe(PcmWindow().snapshot(), cancel=token)
        assert time.monotonic() - t0 < 0.5                     # did not wait for the 3 s "decode"
        assert _no_workers_left()
        end = time.monotonic() + 1.0
        while not srv.events("client gone") and time.monotonic() < end:
            time.sleep(0.01)
        assert srv.events("client gone") and not srv.events("done")   # the server could drop the work
    finally:
        srv.close()


class _SlowConnect(socket.socket):
    """A TCP connect that completes only after 1 s (after the test's cancel)."""

    def connect_ex(self, address):
        time.sleep(1.0)
        return super().connect_ex(address)


def test_a_request_cancelled_while_connecting_is_never_sent(monkeypatch):
    srv = _OneAtATimeWhisper([0.0])
    monkeypatch.setattr(_http, "_new_socket", _SlowConnect)   # the TCP connect takes 1 s
    try:
        token = CancelToken()
        threading.Timer(0.1, token.cancel).start()             # cancelled while still connecting
        t0 = time.monotonic()
        with pytest.raises(Cancelled):
            WhisperServerClient(srv.url, timeout_s=5).transcribe(PcmWindow().snapshot(), cancel=token)
        assert time.monotonic() - t0 < 0.7                      # released at the cancel, not after the connect
        time.sleep(1.2)                                         # the connect completes meanwhile...
        assert srv.log == []                                    # ...but the request is never sent
        assert _no_workers_left()
    finally:
        srv.close()


def test_a_request_cancelled_before_it_starts_is_never_sent():
    srv = _OneAtATimeWhisper([0.0])
    try:
        token = CancelToken()
        token.cancel()
        with pytest.raises(Cancelled):
            WhisperServerClient(srv.url).transcribe(PcmWindow().snapshot(), cancel=token)
        time.sleep(0.1)
        assert srv.log == []
    finally:
        srv.close()


def _speech(window, seconds):
    window.append(b"\x01\x00" * int(seconds * SAMPLE_RATE))


def test_end_of_speech_cancels_the_partial_so_the_final_does_not_queue_behind_it():
    srv = _OneAtATimeWhisper([3.0, 0.05])     # the partial would take 3 s; the final takes 0.05 s
    try:
        window = PcmWindow()
        _speech(window, 0.6)
        tr = LiveTranscriber(WhisperServerClient(srv.url, timeout_s=10), window)
        tr.start()
        end = time.monotonic() + 2.0
        while not srv.events("start") and time.monotonic() < end:
            time.sleep(0.01)                                     # the partial is being "decoded"
        _speech(window, 0.3)
        t0 = time.monotonic()
        final = tr.finish()
        assert time.monotonic() - t0 < 0.6                      # not ~3 s behind the partial
        assert final == "text 1" and tr.cancelled == 1 and tr.partials == 1
        assert srv.events("client gone")[0][0] == 0             # the partial's request was dropped
        assert tr.poll() is None
    finally:
        srv.close()


class _FakeWhisper:
    """In-process stand-in: partial calls (with a cancel token) block until released or cancelled."""

    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.started = threading.Semaphore(0)
        self.active = 0                                         # partial calls that have not returned
        self._lock = threading.Lock()

    def transcribe(self, wav, cancel=None):
        if cancel is None:
            self.calls.append("final")
            return "[BLANK_AUDIO] Open the claw."
        self.calls.append("partial")
        with self._lock:
            self.active += 1
        self.started.release()
        try:
            while not self.release.wait(0.005):
                if cancel.cancelled:
                    raise Cancelled("cancelled")
            return " Open the claw (wind) "
        finally:
            with self._lock:
                self.active -= 1


def test_partials_are_handed_out_by_poll_one_newest_at_a_time():
    fake, window = _FakeWhisper(), PcmWindow()
    fake.release.set()
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    assert tr.poll() is None
    _speech(window, 0.6)
    tr.start()
    end = time.monotonic() + 2.0
    got = None
    while got is None and time.monotonic() < end:
        got = tr.poll()
        time.sleep(0.005)
    assert got == (1, "Open the claw")                          # noise markers dropped
    assert tr.finish() == "Open the claw."


def test_nothing_partial_happens_after_end_of_speech():
    fake, window = _FakeWhisper(), PcmWindow()
    _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    assert fake.started.acquire(timeout=2)                      # a partial is in flight, held
    assert tr.finish() == "Open the claw." and tr.cancelled == 1
    with pytest.raises(WindowSealed):
        _speech(window, 1.0)                                    # more audio after the end of speech: refused
    fake.release.set()
    time.sleep(0.15)
    assert fake.calls == ["partial", "final"]                   # no new partial was started
    assert tr.poll() is None                                    # and none is handed out


def test_a_partial_not_taken_before_end_of_speech_is_dropped():
    fake, window = _FakeWhisper(), PcmWindow()
    fake.release.set()
    _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    end = time.monotonic() + 2.0
    while "partial" not in fake.calls and time.monotonic() < end:
        time.sleep(0.005)
    time.sleep(0.05)                                            # its text is waiting in the slot
    tr.finish()
    assert tr.poll() is None


def test_no_partial_starts_after_end_of_speech_even_when_none_was_in_flight():
    fake, window = _FakeWhisper(), PcmWindow()
    fake.release.set()
    _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    end = time.monotonic() + 2.0
    while 'partial' not in fake.calls and time.monotonic() < end:
        time.sleep(0.005)
    time.sleep(0.05)                                            # idle: no request in flight
    tr.finish()
    with pytest.raises(WindowSealed):
        _speech(window, 1.0)                                    # new audio after the end of speech: refused
    time.sleep(0.15)
    assert fake.calls.count('partial') == 1 and tr.cancelled == 0


def test_a_second_start_makes_no_second_worker():
    fake, window = _FakeWhisper(), PcmWindow()
    _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    assert fake.started.acquire(timeout=2)                      # its one worker's partial is in flight, held
    with pytest.raises(RuntimeError, match="once"):
        tr.start()
    assert not fake.started.acquire(timeout=0.2)                # no second worker sent a partial
    assert tr.finish() == "Open the claw." and tr.partials == 1 and tr.cancelled == 1
    assert fake.active == 0 and fake.calls == ["partial", "final"]   # nothing still holds the server


class _SlowCancelWhisper:
    """Partials whose cancellation is held (inside cancel(), on the cancelling thread) until the test lets
    it finish; the audio of every final is kept."""

    def __init__(self):
        self.in_flight, self.cancelling, self.let_cancel = threading.Event(), threading.Event(), threading.Event()
        self.finals = []

    def transcribe(self, wav, cancel=None):
        if cancel is None:
            self.finals.append(wav)
            return "Open the claw."

        def hold():
            self.cancelling.set()
            assert self.let_cancel.wait(5)

        assert cancel._attach(hold)
        self.in_flight.set()
        while not cancel.cancelled:
            time.sleep(0.005)
        raise Cancelled("cancelled")


def test_audio_after_the_end_of_speech_never_reaches_the_final_even_while_the_partial_is_cancelled():
    fake, window = _SlowCancelWhisper(), PcmWindow()
    _speech(window, 0.6)
    heard = window.snapshot()                                   # all of the utterance
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    assert fake.in_flight.wait(2)                               # a partial is in flight
    out = {}

    def finish():
        try:
            out["text"] = tr.finish()
        except AudioOverflow as e:
            out["error"] = e

    t = threading.Thread(target=finish)
    t.start()
    try:
        assert fake.cancelling.wait(2)                          # finish() is inside the cancel, held there
        with pytest.raises(WindowSealed):
            _speech(window, 0.3)                                # the next utterance starting: refused
    finally:
        fake.let_cancel.set()
        t.join(5)
    assert fake.finals == [heard]                               # the final was sent this utterance and no more
    assert "error" in out and "text" not in out                 # and audio arrived during it: not final
    assert tr.cancelled == 1


def test_a_seal_refused_at_the_end_of_speech_still_cancels_the_partial():
    fake, window = _FakeWhisper(), PcmWindow()
    _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    assert fake.started.acquire(timeout=2)                      # a partial is in flight, held
    with pytest.raises(ValueError):
        window.append(bytes(1))                                 # half a sample: refused, the utterance is not whole
    with pytest.raises(AudioOverflow):
        tr.finish()                                             # the seal refuses: no final
    assert tr.cancelled == 1 and fake.active == 0               # but the partial was cancelled all the same
    assert fake.calls == ["partial"]


def test_start_after_finish_is_refused():
    fake, window = _FakeWhisper(), PcmWindow()
    _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    assert tr.finish() == "Open the claw."                      # a final with no partials is fine
    with pytest.raises(RuntimeError, match="once"):
        tr.start()                                              # but the utterance is over
    time.sleep(0.1)
    assert fake.calls == ["final"] and tr.partials == 0


# ---- whisper's noise tags: only known markers are dropped --------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Open the claw (don't)",
    "Open the claw [don't]",
    "Open the claw (do not)",
    "Open the claw (no, I mean close it)",
    "Open the claw [actually, close it]",
    "Open the claw (stop)",
    "[STOP] Open the claw",
    "Open the claw (after calibration)",
    "Open the claw (later)",
    "Open the claw (no music)",          # "no" is a cue, not a sound descriptor
    "Open the claw (stop music)",
    "(don't laugh) Open the claw",
    "(upbeat music and don't)",          # a tag is dropped only as a whole
    "(I like music)",
])
def test_speech_inside_brackets_or_parentheses_is_kept(text):
    assert speech_only(text) == text


@pytest.mark.parametrize("text,clean", [
    ("[BLANK_AUDIO]", ""),
    ("[BLANK_AUDIO] Open the claw.", "Open the claw."),
    ("Open the claw. [MUSIC]", "Open the claw."),
    ("(upbeat music) Open the claw", "Open the claw"),
    ("Open (laughs) the claw", "Open the claw"),
    ("( soft music playing ) Close the claw", "Close the claw"),
    ("[ Silence ] Look around", "Look around"),
    ("Open the claw (applause)", "Open the claw"),
    ("(wind) Open the claw (coughing)", "Open the claw"),
    ("[Music] [BLANK_AUDIO]", ""),
])
def test_known_noise_tags_are_dropped(text, clean):
    assert speech_only(text) == clean


def test_no_word_the_noise_rule_can_drop_is_a_safety_cue():
    words = [w for phrase in SOUND_DESCRIPTORS + SOUND_WORDS + ("playing",) for w in phrase.split()]
    for w in words:
        for rx in (NEGATION, FUTURE_HINTS, CORRECTION, STOP_WORDS):
            assert not rx.search(w), (w, rx.pattern)


# ---- an overflowed utterance is invalid ------------------------------------------------------------------------


def test_an_overflowed_utterance_never_yields_a_final_transcript():
    fake, window = _FakeWhisper(), PcmWindow(max_seconds=1.0)
    _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    assert fake.started.acquire(timeout=2)                      # a partial of the first 0.6 s is in flight
    with pytest.raises(AudioOverflow):
        _speech(window, 0.6)                                    # the rest does not fit
    fake.release.set()                                          # the partial answers after the overflow...
    time.sleep(0.1)
    assert tr.poll() is None                                    # ...and is never handed out
    with pytest.raises(AudioOverflow):
        tr.finish()                                             # no final transcript of the kept prefix
    assert fake.calls == ["partial"]                            # nothing more was sent


def test_partials_stop_once_the_utterance_overflowed(monkeypatch):
    crashed = []
    monkeypatch.setattr(threading, "excepthook", lambda args: crashed.append(args.exc_type))
    fake, window = _FakeWhisper(), PcmWindow(max_seconds=1.0)
    fake.release.set()
    _speech(window, 0.6)
    with pytest.raises(AudioOverflow):
        _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    time.sleep(0.15)
    assert fake.calls == [] and tr.poll() is None               # the overflowed window is never sent
    with pytest.raises(AudioOverflow):
        tr.finish()
    assert fake.calls == [] and crashed == []                   # and the worker ended cleanly


# ---- the final is of the whole utterance, or it is not final -----------------------------------------------------


class _BlockingFinal:
    """Partials answer at once; the FINAL transcription blocks until the test releases it."""

    def __init__(self, text="Open the claw"):
        self.text, self.calls = text, []
        self.final_started, self.release = threading.Event(), threading.Event()

    def transcribe(self, wav, cancel=None):
        if cancel is not None:
            self.calls.append("partial")
            return self.text
        self.calls.append("final")
        self.final_started.set()
        assert self.release.wait(5), "the test never released the final"
        return self.text


def _finish_in_thread(tr):
    out = {}

    def run():
        try:
            out["text"] = tr.finish()
        except Exception as e:  # noqa: BLE001
            out["error"] = e

    t = threading.Thread(target=run)
    t.start()
    return t, out


@pytest.mark.parametrize("tail_s", [0.6, 0.05], ids=["tail overflows the window", "tail fits"])
def test_audio_appended_while_the_final_is_made_makes_it_not_final(tail_s):
    stt, window = _BlockingFinal(), PcmWindow(max_seconds=1.0)
    _speech(window, 0.6)                                        # "Open the claw"
    tr = LiveTranscriber(stt, window)
    t, out = _finish_in_thread(tr)
    assert stt.final_started.wait(2)                            # the final transcription is running
    with pytest.raises(WindowSealed):
        _speech(window, tail_s)                                 # "...no, don't" arrives late: refused
    stt.release.set()
    t.join(5)
    assert "text" not in out                                    # the prefix is never handed out as final
    assert isinstance(out["error"], AudioOverflow)              # the usual "not final, say it again"


def test_a_reset_during_the_final_cannot_hide_refused_audio():
    stt, window = _BlockingFinal(), PcmWindow(max_seconds=1.0)
    _speech(window, 0.6)
    tr = LiveTranscriber(stt, window)
    t, out = _finish_in_thread(tr)
    assert stt.final_started.wait(2)
    with pytest.raises(WindowSealed):
        _speech(window, 0.05)                                   # the tail is refused...
    window.reset()                                              # ...then the producer moves on,
    _speech(window, 0.3)                                        # and the next utterance is accepted
    stt.release.set()
    t.join(5)
    assert "text" not in out and isinstance(out["error"], AudioOverflow)


def test_once_the_final_is_decided_the_window_refuses_audio_until_reset():
    stt, window = _BlockingFinal(), PcmWindow(max_seconds=1.0)
    stt.release.set()
    _speech(window, 0.6)
    assert LiveTranscriber(stt, window).finish() == "Open the claw"   # the whole utterance: final
    with pytest.raises(WindowSealed):
        _speech(window, 0.05)                                   # nothing more for this utterance
    window.reset()
    _speech(window, 0.05)                                       # the next utterance starts clean
    assert window.snapshot() and not window.overflowed


def test_a_transcriber_never_finalizes_another_utterances_audio():
    stt, window = _BlockingFinal(), PcmWindow(max_seconds=1.0)
    stt.release.set()
    _speech(window, 0.6)
    tr = LiveTranscriber(stt, window)
    window.reset()                                              # the producer moved on before the end
    _speech(window, 0.3)
    with pytest.raises(AudioOverflow):
        tr.finish()
    assert "final" not in stt.calls


def test_seal_captures_exactly_what_had_arrived_and_decides_once():
    window = PcmWindow(max_seconds=1.0)
    window.append(pcm(1, 2, 3))
    sealed = window.seal()
    assert read_wav(sealed.audio)[3] == pcm(1, 2, 3)
    assert window.finalize(sealed) is True                      # nothing was refused: final
    with pytest.raises(WindowSealed):
        window.append(pcm(4))
    assert window.finalize(sealed) is True                      # the decision stands
    other = PcmWindow(max_seconds=1.0)
    other.append(pcm(1))
    sealed2 = other.seal()
    with pytest.raises(WindowSealed):
        other.append(pcm(2))                                    # refused before the decision...
    assert other.finalize(sealed2) is False                     # ...so not final
    with pytest.raises(AudioOverflow):
        other.snapshot()
    with pytest.raises(AudioOverflow):
        PcmWindow().seal(generation=-1)                         # another utterance than the one held


@pytest.mark.parametrize("next_s", [0.6, 0.1], ids=["next utterance long", "next utterance short"])
def test_partials_stop_when_the_window_moves_on_to_another_utterance(next_s):
    fake, window = _FakeWhisper(), PcmWindow()
    fake.release.set()
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    window.reset()                                              # this transcriber's utterance is gone
    _speech(window, next_s)                                     # audio of the next one
    tr.start()
    tr._thread.join(0.5)
    assert not tr._thread.is_alive()                            # it stops at once, whatever the next holds
    assert fake.calls == [] and tr.poll() is None               # never sent as this utterance's partial


class _ClipsAtSnapshot(PcmWindow):
    """Stops being whole between the partial loop's check and its snapshot (the race, made certain)."""

    def snapshot(self, generation=None):
        raise AudioOverflow("clipped between the check and the snapshot")


def test_a_window_clipped_between_check_and_snapshot_ends_the_partials_cleanly(monkeypatch):
    crashed = []
    monkeypatch.setattr(threading, "excepthook", lambda args: crashed.append(args.exc_type))
    fake, window = _FakeWhisper(), _ClipsAtSnapshot()
    fake.release.set()
    _speech(window, 0.6)
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    tr._thread.join(1.0)
    assert not tr._thread.is_alive() and crashed == [] and fake.calls == []


# ---- a missing or misplaced chunk: the utterance is not whole -------------------------------------------------


def test_an_out_of_order_chunk_is_a_kind_of_not_whole():
    assert issubclass(OutOfOrder, AudioOverflow) and issubclass(OutOfOrder, ValueError)


@pytest.mark.parametrize("arrival", [[0, 2, 3], [0, 2, 1, 3], [0, 1, 1, 2]], ids=["dropped", "reordered", "duplicated"])
def test_an_out_of_order_chunk_makes_the_utterance_invalid_until_reset(arrival):
    w = PcmWindow()
    refused = []
    for seq in arrival:                                         # a caller that catches the error and carries on
        try:
            w.append(pcm(seq + 1), seq=seq)
        except (AudioOverflow, OutOfOrder) as e:
            refused.append(type(e).__name__)
    assert refused and refused[0] == "OutOfOrder"
    first_bad = next(i for i, seq in enumerate(arrival) if seq != i)
    assert len(refused) == len(arrival) - first_bad              # from the first bad chunk on, all refused
    assert not w.intact(w.generation)
    with pytest.raises(AudioOverflow):
        w.snapshot()                                            # nothing with a hole in it is transcribed
    with pytest.raises(AudioOverflow):
        w.seal()                                                # and it can never be finalized
    w.reset()                                                   # the next utterance starts clean
    w.append(pcm(9), seq=0)
    assert read_wav(w.snapshot())[3] == pcm(9)


@pytest.mark.parametrize("arrival", [[0, 2, 3], [0, 2, 1, 3]], ids=["dropped", "reordered"])
def test_an_utterance_with_a_missing_chunk_never_yields_a_final_transcript(arrival):
    fake, window = _FakeWhisper(), PcmWindow()
    fake.release.set()
    for seq in arrival:
        try:
            window.append(pcm(*([1] * 8000)), seq=seq)          # 0.5 s of audio per chunk
        except (AudioOverflow, OutOfOrder):
            pass
    tr = LiveTranscriber(fake, window, poll_s=0.005)
    tr.start()
    time.sleep(0.1)
    assert tr.poll() is None and "partial" not in fake.calls   # no partial of an utterance with a hole
    with pytest.raises(AudioOverflow):
        tr.finish()                                             # and no final transcript
    assert "final" not in fake.calls


# ---- any refused chunk voids the utterance -------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(REJECTIONS))
def test_any_refused_chunk_voids_the_utterance_until_reset(kind):
    w = PcmWindow(max_seconds=5.0)
    w.append(SAMPLES, seq=0)
    with pytest.raises(Exception):
        REJECTIONS[kind](w)                                     # refused
    for _ in range(3):                                          # the producer carries on with valid audio...
        with pytest.raises(AudioOverflow):
            w.append(SAMPLES)                                   # ...and is told the utterance is void
    assert not w.intact(w.generation)
    with pytest.raises(AudioOverflow):
        w.snapshot()
    with pytest.raises(AudioOverflow):
        w.seal()
    stt = _FakeWhisper()
    stt.release.set()
    with pytest.raises(AudioOverflow):
        LiveTranscriber(stt, w).finish()
    assert "final" not in stt.calls
    w.reset()                                                   # the next utterance starts clean
    w.append(SAMPLES, seq=0)
    assert w.intact(w.generation) and read_wav(w.snapshot())[3] == SAMPLES


def test_an_empty_chunk_is_not_a_refusal():
    w = PcmWindow()
    w.append(SAMPLES, seq=0)
    w.append(b"", seq=1)                                        # a keep-alive: accepted, nothing stored
    w.append(SAMPLES, seq=2)
    assert w.intact(w.generation) and read_wav(w.snapshot())[3] == SAMPLES + SAMPLES


# ---- every read of an utterance is checked against its generation, atomically ------------------------------


def test_every_read_of_an_utterance_refuses_another_generation():
    w = PcmWindow()
    w.append(SAMPLES)
    old = w.generation
    sealed = w.seal(old)
    w.reset()
    w.append(SAMPLES)                                           # the next utterance
    assert not w.intact(old)
    with pytest.raises(AudioOverflow):
        w.snapshot(old)
    with pytest.raises(AudioOverflow):
        w.duration(old)
    with pytest.raises(AudioOverflow):
        w.seal(old)
    assert w.finalize(sealed) is False                          # an older utterance's seal is never final
    assert read_wav(w.snapshot(w.generation))[3] == SAMPLES and w.duration(w.generation) == pytest.approx(0.05)


def test_a_reset_during_the_final_makes_it_not_final():
    stt, window = _BlockingFinal(), PcmWindow(max_seconds=1.0)
    _speech(window, 0.6)
    tr = LiveTranscriber(stt, window)
    t, out = _finish_in_thread(tr)
    assert stt.final_started.wait(2)                            # the final transcription is running
    window.reset()                                              # the producer moves on, with no late append
    _speech(window, 0.3)                                        # and the next utterance is accepted
    stt.release.set()
    t.join(5)
    assert "text" not in out and isinstance(out["error"], AudioOverflow)


class _MovesOnBeforeReading(PcmWindow):
    """Moves on to the next utterance (reset, then its audio) just before a partial reads the audio: the
    race between the partial loop's check and its read, made certain."""

    NEXT = (7).to_bytes(2, "little") * 8000                    # the next utterance's audio, recognisable

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.moved_on = False

    def snapshot(self, generation=None):
        if not self.moved_on:
            self.moved_on = True
            self.reset()
            self.append(self.NEXT)
        return super().snapshot() if generation is None else super().snapshot(generation)


class _RecordingWhisper:
    def __init__(self):
        self.sent = []

    def transcribe(self, wav, cancel=None):
        self.sent.append(wav)
        return "Open the claw"


def test_an_old_transcriber_never_sends_the_next_utterances_audio():
    stt, window = _RecordingWhisper(), _MovesOnBeforeReading()
    window.append(SAMPLES * 12)                                 # 0.6 s of this utterance
    tr = LiveTranscriber(stt, window, poll_s=0.005)
    tr.start()
    tr._thread.join(1.0)
    assert window.moved_on and not tr._thread.is_alive()        # it stopped: its utterance is gone
    assert not any(_MovesOnBeforeReading.NEXT in wav for wav in stt.sent)
    assert stt.sent == []                                       # nothing was sent for it at all


# ---- the window's memory is bounded by max_bytes, whatever is offered -------------------------------------


def _peak_while(fn):
    """The most memory allocated (above the start) while fn() runs; fn's exception, if any, is swallowed."""
    tracemalloc.start()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    try:
        fn()
    except AudioOverflow:
        pass
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    return peak


@pytest.mark.parametrize("wrap", [lambda b: b, memoryview], ids=["bytearray", "memoryview"])
def test_an_oversized_buffer_is_refused_before_any_copy(wrap):
    w = PcmWindow(max_seconds=1.0)                              # 32 000 bytes
    big = wrap(bytearray(16 * 1024 * 1024))                     # 16 MB offered
    refused = []
    peak = _peak_while(lambda: refused.append(1) or w.append(big))
    assert w.overflowed and not w.intact(w.generation)          # refused, and the utterance is void
    assert peak < 1024 * 1024                                   # never copied: far less than the 16 MB


def test_a_buffer_is_measured_in_bytes_not_items():
    w = PcmWindow(max_seconds=1000 / (SAMPLE_RATE * SAMPLE_WIDTH))   # 1000 bytes
    with pytest.raises(AudioOverflow):
        w.append(memoryview(array("h", [0] * 600)))             # 600 items, but 1200 bytes
    assert w.overflowed


def test_many_tiny_chunks_take_no_more_memory_than_the_window():
    two_bytes = (1).to_bytes(2, "little")
    tracemalloc.start()
    w = PcmWindow(max_seconds=1.0)                              # 32 000 bytes
    for _ in range(10_000):
        w.append(two_bytes)                                     # 20 000 bytes of audio in 10 000 chunks
    held = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    assert held < 2 * w.max_bytes                               # the window, not 10 000 objects
    assert w.duration_s == pytest.approx(20_000 / (SAMPLE_RATE * SAMPLE_WIDTH))
    assert read_wav(w.snapshot())[3] == two_bytes * 10_000
