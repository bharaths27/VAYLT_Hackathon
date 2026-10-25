"""Audio window: ordered, bounded, never drops the start; whisper.cpp client is loopback only."""

import io
import json
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from engram.spark.audio_windows import (
    SAMPLE_RATE, AudioOverflow, OutOfOrder, PcmWindow, WhisperError, WhisperServerClient,
)


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


def test_overflow_is_refused_and_the_start_of_the_utterance_is_kept():
    w = PcmWindow(max_seconds=0.001)               # 16 samples
    w.append(pcm(*range(10)))                      # e.g. "don't"
    with pytest.raises(AudioOverflow):
        w.append(pcm(*range(10)))
    with pytest.raises(AudioOverflow):              # full from now on: no hole in the middle of the utterance
        w.append(pcm(1))
    assert read_wav(w.snapshot())[3] == pcm(*range(10))
    w.reset()
    w.append(pcm(7))
    assert read_wav(w.snapshot())[3] == pcm(7)


def test_chunks_must_arrive_in_order_and_hold_whole_samples():
    w = PcmWindow()
    w.append(pcm(1), seq=0)
    with pytest.raises(OutOfOrder):
        w.append(pcm(3), seq=2)
    w.append(pcm(2), seq=1)
    with pytest.raises(ValueError):
        w.append(b"\x01")
    assert read_wav(w.snapshot())[3] == pcm(1, 2)


def test_empty_chunks_store_nothing_but_keep_their_sequence_number():
    w = PcmWindow(max_seconds=0.001)
    for i in range(10_000):
        w.append(b"", seq=i)
    for _ in range(10_000):
        w.append(b"")
    assert w._chunks == [] and w.duration_s == 0
    w.append(pcm(1), seq=20_000)
    with pytest.raises(OutOfOrder):
        w.append(b"", seq=5)
    w.append(b"", seq=20_001)
    assert len(w._chunks) == 1 and read_wav(w.snapshot())[3] == pcm(1)


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
