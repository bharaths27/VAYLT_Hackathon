"""The intent model client: fixed endpoint, required model id, no redirects, the key never leaks."""

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest

from engram.spark import _http
from engram.spark.intent_client import (
    ENDPOINT, MAX_TEXT, QUESTIONS, IntentClient, IntentConfigError, IntentDecision, IntentError, parse_reply,
)

KEY = "unit-test-placeholder-value"   # not a real key; only checked for leaks
MODEL = "intent-model-under-test"


def answers(preset="open_gripper", confidence=0.91, **probs):
    out = {name: {"type": "noul", "noul": probs.get(name, 0.1)}
           for name in ("ready", "directed", "negated", "future", "correction")}
    out["preset"] = {"type": "choice", "choice": preset, "confidence": confidence}
    return {"answers": out, "model": MODEL}


class FakeTransport:
    def __init__(self, status=200, reply=None, error=None):
        self.status, self.reply, self.error = status, reply if reply is not None else answers(), error
        self.sent = []

    def __call__(self, url, body, headers, timeout_s):
        self.sent.append((url, json.loads(body), dict(headers), timeout_s))
        if self.error:
            raise self.error
        return self.status, json.dumps(self.reply).encode()


def client(transport, **kw):
    return IntentClient(KEY, MODEL, transport=transport, **kw)


def test_the_model_id_is_required_from_the_environment():
    with pytest.raises(IntentConfigError, match="TYPESAFE_INTENT_MODEL"):
        IntentClient.from_env({"TYPESAFE_API_KEY": KEY})
    with pytest.raises(IntentConfigError, match="TYPESAFE_API_KEY"):
        IntentClient.from_env({"TYPESAFE_INTENT_MODEL": MODEL})
    c = IntentClient.from_env({"TYPESAFE_API_KEY": KEY, "TYPESAFE_INTENT_MODEL": MODEL})
    assert c.model == MODEL


def test_request_shape_goes_to_the_fixed_official_endpoint():
    t = FakeTransport()
    c = IntentClient.from_env({"TYPESAFE_API_KEY": KEY, "TYPESAFE_INTENT_MODEL": MODEL,
                               "TYPESAFE_URL": "https://example.invalid/v1"}, transport=t)
    d = c.classify("Open the claw", partial=True)
    url, body, headers, timeout_s = t.sent[0]
    assert url == ENDPOINT == "https://api.typesafe.ai/v1/systemone"
    assert headers["Authorization"] == f"Bearer {KEY}" and headers["Content-Type"] == "application/json"
    assert body["model"] == MODEL and body["questions"] == QUESTIONS
    assert body["state"]["transcript"] == "Open the claw" and body["state"]["transcript_is_partial"] is True
    assert set(QUESTIONS) == {"preset", "ready", "directed", "negated", "future", "correction"}
    assert QUESTIONS["preset"]["type"] == "choice" and QUESTIONS["negated"]["type"] == "noul"
    assert 0 < timeout_s <= 10
    assert isinstance(d, IntentDecision) and d.preset == "open_gripper" and d.confidence == 0.91


def test_timeout_is_bounded():
    with pytest.raises(ValueError):
        client(FakeTransport(), timeout_s=60)


def test_none_choice_means_no_preset():
    d = parse_reply(answers(preset="none", confidence=0.8, ready=0.2), MODEL)
    assert d.preset is None and d.ready == 0.2


@pytest.mark.parametrize("bad", [
    {"answers": {}},
    "not a dict",
    answers(preset="drive_forward"),                         # not a supported preset
    {**answers(), "answers": {**answers()["answers"], "ready": {"type": "noul", "noul": 1.5}}},
    {**answers(), "answers": {**answers()["answers"], "negated": {"type": "noul", "noul": True}}},
    {**answers(), "answers": {k: v for k, v in answers()["answers"].items() if k != "future"}},
    {**answers(), "answers": {**answers()["answers"], "preset": {"type": "choice", "choice": ["open_gripper"]}}},
    {**answers(), "answers": {**answers()["answers"], "preset": {"type": "choice", "choice": {"a": 1}}}},
    {**answers(), "answers": {**answers()["answers"], "preset": {"type": "choice", "choice": None}}},
])
def test_malformed_replies_raise(bad):
    with pytest.raises(IntentError):
        parse_reply(bad, MODEL)


@pytest.mark.parametrize("choice", [["open_gripper"], {"open_gripper": 1}])
def test_unhashable_preset_choice_is_an_intent_error_not_a_crash(choice):
    reply = answers()
    reply["answers"]["preset"]["choice"] = choice
    with pytest.raises(IntentError, match="preset"):
        client(FakeTransport(reply=reply)).classify("Open the claw", True)


def test_missing_preset_confidence_counts_as_zero():
    raw = answers()
    del raw["answers"]["preset"]["confidence"]
    assert parse_reply(raw, MODEL).confidence == 0.0


@pytest.mark.parametrize("status,match", [(302, "redirect refused"), (307, "redirect refused"), (401, "http 401"),
                                          (500, "http 500")])
def test_non_200_replies_raise(status, match):
    with pytest.raises(IntentError, match=match):
        client(FakeTransport(status=status)).classify("Open the claw", False)


def test_the_key_never_reaches_logs_errors_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    c = client(FakeTransport())
    c.classify("Open the claw", False)
    leaky = client(FakeTransport(error=OSError(f"connection reset; Authorization: Bearer {KEY}")))
    with pytest.raises(IntentError) as err:
        leaky.classify("Open the claw", False)
    with pytest.raises(IntentError) as err2:
        client(FakeTransport(status=403)).classify("Open the claw", False)
    assert caplog.records, "expected the client to log something"
    for text in (caplog.text, str(err.value), repr(err.value), str(err2.value), repr(c), str(c)):
        assert KEY not in text
    assert err.value.__cause__ is None and err.value.__context__ is None   # nothing chained that could print it


# ---- real HTTP on loopback: a redirect is never followed ----------------------------------------------------------


class _Server:
    def __init__(self, status=302):
        self.hits = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                outer.hits.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/start":
                    self.send_response(status)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    body = json.dumps(answers()).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Trickle:
    """Sends a reply one byte every 50 ms, for at most 3 s. phase: 'body' or 'headers'."""

    def __init__(self, phase):
        self.stop = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                reply = json.dumps(answers()).encode()
                # The long header keeps a header-phase trickle going for the whole 3 s.
                head = (f"HTTP/1.1 200 OK\r\nX-Pad: {'a' * 400}\r\n"
                        f"Content-Length: {len(reply) + 100_000}\r\n\r\n").encode()
                if phase == "body":
                    self.wfile.write(head)
                    slow = reply + b" " * 100_000
                else:
                    slow = head + reply
                t_end = time.monotonic() + 3.0
                try:
                    for i in range(len(slow)):
                        if outer.stop.is_set() or time.monotonic() > t_end:
                            return
                        self.wfile.write(slow[i:i + 1])
                        time.sleep(0.05)
                except OSError:
                    return          # the client hung up: what a deadline should cause

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/systemone"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def _no_http_workers_left(within_s=1.0):
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        if not any(t.name == "spark-http" and t.is_alive() for t in threading.enumerate()):
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("phase", ["body", "headers"])
def test_a_trickling_server_cannot_hold_the_caller_past_the_deadline(phase):
    srv = _Trickle(phase)
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            _http.post(srv.url, b"{}", {"Content-Type": "application/json"}, 0.5)
        assert time.monotonic() - t0 < 1.5            # each byte arrives well inside any per-read timeout
        assert _no_http_workers_left()                # the abandoned worker was unblocked and ended

        def to_trickle(_url, body, headers, timeout_s):
            return _http.post(srv.url, body, headers, timeout_s)

        t0 = time.monotonic()
        with pytest.raises(IntentError, match="TimeoutError"):
            client(to_trickle, timeout_s=0.5).classify("Open the claw", True)
        assert time.monotonic() - t0 < 1.5
    finally:
        srv.close()


def _http_workers():
    return sum(1 for t in threading.enumerate() if t.name == "spark-http" and t.is_alive())


def test_a_stalled_resolver_cannot_pile_up_workers(monkeypatch):
    assert _no_http_workers_left()
    release = threading.Event()

    def stuck_resolve(*args, **kwargs):          # a name lookup that never answers until the test ends
        release.wait(10)
        raise socket.gaierror("released by the test")

    monkeypatch.setattr(_http, "_resolve", stuck_resolve)
    outcomes = []
    try:
        for _ in range(3 * _http.MAX_OUTSTANDING):
            t0 = time.monotonic()
            with pytest.raises((TimeoutError, _http.Busy)) as err:
                _http.post("https://intent.invalid/v1/systemone", b"{}", {}, 0.1)
            outcomes.append(type(err.value).__name__)
            assert time.monotonic() - t0 < 0.5
            assert _http_workers() <= _http.MAX_OUTSTANDING
    finally:
        release.set()
    assert outcomes == ["TimeoutError"] * _http.MAX_OUTSTANDING + ["Busy"] * (2 * _http.MAX_OUTSTANDING)
    assert _no_http_workers_left()

    def no_such_host(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(_http, "_resolve", no_such_host)
    with pytest.raises(socket.gaierror):          # the slots came back: a new request runs again
        _http.post("https://intent.invalid/v1/systemone", b"{}", {}, 1.0)


class _StalledSocket:
    """A socket whose connect is never answered: it waits out the timeout it was given, forever if none."""

    release = threading.Event()
    made: list = []

    def __init__(self, *args):
        self.timeout = None
        _StalledSocket.made.append(self)

    def settimeout(self, t):
        self.timeout = t

    def connect(self, sockaddr):
        if not self.release.wait(self.timeout):
            raise socket.timeout("timed out")
        raise ConnectionRefusedError("released by the test")

    def close(self):
        pass


def test_connect_attempts_end_by_the_deadline_so_workers_never_pile_up(monkeypatch):
    assert _no_http_workers_left()
    _StalledSocket.release.clear()
    _StalledSocket.made.clear()
    monkeypatch.setattr(_http, "_resolve", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                               ("192.0.2.1", 443))])
    monkeypatch.setattr(_http, "_new_socket", _StalledSocket)
    try:
        for _ in range(2 * _http.MAX_OUTSTANDING):      # more than the cap: none may be refused as Busy
            t0 = time.monotonic()
            with pytest.raises(TimeoutError):
                _http.post("https://intent.invalid/v1/systemone", b"{}", {}, 0.2)
            assert time.monotonic() - t0 < 0.6
            assert _no_http_workers_left(within_s=0.5)  # the connect itself gave up at the deadline
        assert len(_StalledSocket.made) == 2 * _http.MAX_OUTSTANDING
        assert all(s.timeout is not None and 0 < s.timeout <= 0.2 for s in _StalledSocket.made)
    finally:
        _StalledSocket.release.set()


def test_overlong_transcripts_are_refused_and_nothing_is_sent():
    t = FakeTransport()
    c = client(t)
    c.classify("a" * MAX_TEXT, True)                     # exactly at the limit is fine
    assert len(t.sent) == 1
    with pytest.raises(IntentError, match="longer than"):
        c.classify("Open the claw " + "x" * MAX_TEXT + ", no, don't", False)
    assert len(t.sent) == 1                              # the long one never left the machine


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_refused_over_real_http(status):
    srv = _Server(status)
    try:
        url = f"http://127.0.0.1:{srv.port}/start"
        assert _http.post(url, b"{}", {"Content-Type": "application/json"}, 5) == (status, b"")

        def to_local(_url, body, headers, timeout_s):
            return _http.post(url, body, headers, timeout_s)

        with pytest.raises(IntentError, match="redirect refused"):
            client(to_local).classify("Open the claw", False)
        assert [p for p, _ in srv.hits] == ["/start", "/start"]      # /elsewhere was never requested
    finally:
        srv.close()
