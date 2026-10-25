"""HTTPS in _http.post: verification stays on, and a stalled TLS handshake cannot outlive the deadline.

The certificate is made at test time by the openssl command (a throwaway, self-signed, for
127.0.0.1 only), so no key material lives in the repository. Tests that need it are skipped
when openssl is not installed; the stalled-handshake test needs no certificate at all.
"""

import json
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from engram.spark import _http
from engram.spark.intent_client import IntentClient
from spark_fakes import reply_body


def _workers():
    return sum(1 for t in threading.enumerate() if t.name == "spark-http" and t.is_alive())


def _no_workers_left(within_s=1.0):
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        if _workers() == 0:
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    exe = shutil.which("openssl")
    if not exe:
        pytest.skip("the openssl command is needed to make a throwaway test certificate")
    d = tmp_path_factory.mktemp("tls")
    crt, key = d / "cert.pem", d / "key.pem"
    subprocess.run([exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", str(key), "-out", str(crt), "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True, timeout=60)
    return crt, key


class _TLSServer:
    """A real HTTPS server on 127.0.0.1 that answers like the intent model. Records the SNI names it saw."""

    def __init__(self, crt, key):
        self.sni = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = reply_body()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(crt), str(key))
        ctx.sni_callback = lambda sock, name, c: self.sni.append(name)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_https_with_verification_on_still_works(monkeypatch, cert):
    crt, key = cert
    monkeypatch.setattr(_http, "_tls_context", lambda: ssl.create_default_context(cafile=str(crt)))
    srv = _TLSServer(crt, key)
    try:
        url = f"https://127.0.0.1:{srv.port}/v1/systemone"
        status, data = _http.post(url, b"{}", {"Content-Type": "application/json"}, 5.0)
        assert status == 200 and json.loads(data)["answers"]["preset"]["choice"] == "open_gripper"

        def to_local(_url, body, headers, timeout_s):
            return _http.post(url, body, headers, timeout_s)

        assert IntentClient("placeholder", "intent-model-under-test", transport=to_local)(
            "Open the claw", False).preset == "open_gripper"
    finally:
        srv.close()
    assert _no_workers_left()


def test_https_refuses_an_untrusted_certificate_and_a_wrong_host_name(monkeypatch, cert):
    crt, key = cert
    srv = _TLSServer(crt, key)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):              # default trust store: not trusted
            _http.post(f"https://127.0.0.1:{srv.port}/", b"{}", {}, 5.0)
        monkeypatch.setattr(_http, "_tls_context", lambda: ssl.create_default_context(cafile=str(crt)))
        monkeypatch.setattr(_http, "_resolve", lambda host, port, *a: [
            (_http.socket.AF_INET, _http.socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
        with pytest.raises(ssl.SSLCertVerificationError, match="match|mismatch"):   # trusted, wrong name
            _http.post(f"https://intent.example:{srv.port}/", b"{}", {}, 5.0)
        assert "intent.example" in srv.sni                              # SNI carries the host name
    finally:
        srv.close()
    assert _no_workers_left()


class _StuckHandshake:
    """Accepts TCP, then answers the TLS ClientHello one byte every 50 ms (for at most 3 s), never finishing."""

    def __init__(self):
        self.stop = threading.Event()
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(1.0)
                try:
                    self.request.recv(4096)                       # the ClientHello
                except OSError:
                    return
                slow = b"\x16\x03\x03\x40\x00" + b"\x00" * 1000   # a handshake record header, then its body
                end = time.monotonic() + 3.0
                for i in range(len(slow)):
                    if outer.stop.is_set() or time.monotonic() > end:
                        return
                    try:
                        self.request.sendall(slow[i:i + 1])
                    except OSError:
                        return                                    # the client hung up: what the deadline should do
                    time.sleep(0.05)

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.url = f"https://127.0.0.1:{self.srv.server_address[1]}/v1/systemone"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()


class _SlowConnect(socket.socket):
    """A real socket whose TCP connect takes DELAY seconds, so the handshake starts late in the budget."""

    DELAY = 0.0

    def connect_ex(self, address):
        time.sleep(self.DELAY)
        return super().connect_ex(address)


@pytest.mark.parametrize("connect_delay", [0.0, 0.4], ids=["fast connect", "connect uses most of the budget"])
def test_a_trickling_tls_handshake_cannot_outlive_the_deadline(monkeypatch, connect_delay):
    assert _no_workers_left()
    monkeypatch.setattr(_SlowConnect, "DELAY", connect_delay)
    monkeypatch.setattr(_http, "_new_socket", _SlowConnect)
    srv = _StuckHandshake()
    try:
        for _ in range(_http.MAX_OUTSTANDING + 2):         # more calls than slots: none may come back Busy
            t0 = time.monotonic()
            with pytest.raises(TimeoutError):
                _http.post(srv.url, b"{}", {}, 0.6)
            assert time.monotonic() - t0 < 1.0
            assert _workers() <= _http.MAX_OUTSTANDING
            # Ended by the deadline: not by the per-call socket timeout, not by the server giving up after 3 s.
            assert _no_workers_left(within_s=0.25)
    finally:
        srv.close()


def test_tls_socket_is_published_before_its_handshake_starts():
    """What lets the deadline's shutdown reach a handshake: conn.sock IS the TLS socket by then."""
    listener = socket.create_server(("127.0.0.1", 0))
    seen = []

    class Probe(ssl.SSLSocket):
        def do_handshake(self, block=False):
            seen.append(conn.sock is self)
            raise ssl.SSLError("probe stops here")

    ctx = ssl.create_default_context()
    ctx.sslsocket_class = Probe
    try:
        conn = _http._HTTPSConnection("127.0.0.1", listener.getsockname()[1], time.monotonic() + 2.0, ctx)
        with pytest.raises(ssl.SSLError, match="probe"):
            conn.connect()
        assert seen == [True]
        conn.close()
    finally:
        listener.close()
