"""A cancelled request gives its worker slot back promptly, whatever step it was blocked in.

post() runs each request on a worker that holds one of MAX_OUTSTANDING slots. Each test cancels more
requests than there are slots while the worker is blocked in one step (TCP connect, TLS handshake,
sending the request, waiting for the reply), checks that every slot is free again within a short bound
(the deadline is far away), and that a following request is served instead of refused as Busy.
"""

import errno
import socket
import ssl
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from engram.spark import _http

DEADLINE_S = 5.0     # a leaked slot would be held this long
PROMPT_S = 0.5       # a released one is free within this


def _slots_free(within_s=PROMPT_S):
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        if _http._SLOTS._value == _http.MAX_OUTSTANDING and not any(
                t.name == "spark-http" and t.is_alive() for t in threading.enumerate()):
            return True
        time.sleep(0.01)
    return False


class _Ok:
    """A server that answers every POST with 200."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Silent:
    """Accepts connections and then does `mode`: "read" reads the request and never answers;
    "no-read" never reads at all (the client's send blocks once the buffers fill)."""

    def __init__(self, mode):
        self.stop = threading.Event()
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(0.05)
                while not outer.stop.is_set():
                    try:
                        if mode == "read" and self.request.recv(65536) == b"":
                            return
                        if mode == "no-read":
                            time.sleep(0.05)
                    except socket.timeout:
                        pass
                    except OSError:
                        return

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()


def _cancel_many(url, body=b"{}", cancel_after_s=0.2):
    """Cancel more requests than there are slots, each while it is blocked; all must come back fast."""
    for _ in range(_http.MAX_OUTSTANDING + 1):
        token = _http.CancelToken()
        timer = threading.Timer(cancel_after_s, token.cancel)
        timer.start()
        t0 = time.monotonic()
        with pytest.raises(_http.Cancelled):
            _http.post(url, body, {"Content-Type": "application/octet-stream"}, DEADLINE_S, cancel=token)
        assert time.monotonic() - t0 < cancel_after_s + PROMPT_S     # the caller is released at the cancel
        assert _slots_free(), "a cancelled request kept its worker slot"
        timer.join()


def _served_after(ok):
    assert _http.post(ok.url, b"{}", {}, 5.0) == (200, b"ok")        # not Busy


class _NeverConnects(socket.socket):
    """A TCP connect that never completes: the blocking form waits out its timeout, the non-blocking
    form stays in progress (and _http._writable is made to report no progress)."""

    def connect(self, address):
        time.sleep(self.gettimeout() or DEADLINE_S)
        raise socket.timeout("timed out")

    def connect_ex(self, address):
        return errno.EINPROGRESS


def test_cancelled_while_connecting_gives_the_slot_back(monkeypatch):
    assert _slots_free()
    ok = _Ok()
    try:
        monkeypatch.setattr(_http, "_new_socket", _NeverConnects)
        monkeypatch.setattr(_http, "_writable", lambda sock, timeout: time.sleep(timeout) or False, raising=False)
        _cancel_many("http://127.0.0.1:9/")
        monkeypatch.undo()
        _served_after(ok)
    finally:
        ok.close()


def test_cancelled_during_the_tls_handshake_gives_the_slot_back():
    assert _slots_free()
    silent, ok = _Silent("read"), _Ok()                              # never answers the ClientHello
    try:
        _cancel_many(f"https://127.0.0.1:{silent.port}/")
        _served_after(ok)
    finally:
        silent.close()
        ok.close()


def test_cancelled_while_its_request_cannot_be_delivered_gives_the_slot_back():
    # The server never reads. Where send buffers are bounded the send blocks; where the kernel takes
    # it all (Windows), the read of the reply blocks behind the unsent data, and shutting the socket
    # down does not wake it there.
    assert _slots_free()
    silent, ok = _Silent("no-read"), _Ok()
    try:
        _cancel_many(f"http://127.0.0.1:{silent.port}/", body=b"\x00" * (32 * 1024 * 1024), cancel_after_s=0.3)
        _served_after(ok)
    finally:
        silent.close()
        ok.close()


def test_cancelled_while_waiting_for_the_reply_gives_the_slot_back():
    assert _slots_free()
    silent, ok = _Silent("read"), _Ok()                              # reads the request, never answers
    try:
        _cancel_many(f"http://127.0.0.1:{silent.port}/")
        _served_after(ok)
    finally:
        silent.close()
        ok.close()


def test_the_sliced_reader_ends_at_its_own_deadline_even_without_an_abort():
    silent = _Silent("read")
    try:
        sock = socket.create_connection(("127.0.0.1", silent.port))
        try:
            reader = _http._AbortableSocket(sock, time.monotonic() + 0.2, threading.Event()).makefile("rb")
            t0 = time.monotonic()
            with pytest.raises(TimeoutError):
                reader.read(1)                                      # no reply ever comes
            assert time.monotonic() - t0 < 0.5
        finally:
            sock.close()
    finally:
        silent.close()


class _FullSendBuffer:
    """A connected socket whose send buffer never drains: a timed send() waits out its timeout having
    sent nothing (as a real one does), and a blocking sendall() waits out its own."""

    def __init__(self):
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def send(self, view):
        time.sleep(self.timeout or DEADLINE_S)
        raise socket.timeout("timed out")

    def sendall(self, data):
        time.sleep(self.timeout or DEADLINE_S)
        raise socket.timeout("timed out")


def test_a_send_that_cannot_proceed_stops_within_a_slice_of_the_abort():
    aborted = threading.Event()
    threading.Timer(0.1, aborted.set).start()
    wrapped = _http._AbortableSocket(_FullSendBuffer(), time.monotonic() + DEADLINE_S, aborted)
    t0 = time.monotonic()
    with pytest.raises(ConnectionAbortedError):
        wrapped.sendall(b"\x00" * 1024)
    assert time.monotonic() - t0 < 0.1 + PROMPT_S


class _SilentTLS:
    """A TLS socket whose peer never answers: non-blocking, do_handshake() wants to read; with a
    timeout, it waits it out (as a real one does)."""

    def __init__(self):
        self.timeout = None

    def setblocking(self, flag):
        self.timeout = None if flag else 0.0

    def settimeout(self, t):
        self.timeout = t

    def do_handshake(self):
        if self.timeout == 0.0:
            raise ssl.SSLWantReadError("the peer has not answered")
        time.sleep(self.timeout or DEADLINE_S)
        raise socket.timeout("timed out")


def test_a_tls_handshake_that_cannot_proceed_stops_within_a_slice_of_the_abort(monkeypatch):
    monkeypatch.setattr(_http, "_readable", lambda sock, timeout: time.sleep(timeout) or False)
    aborted = threading.Event()
    threading.Timer(0.1, aborted.set).start()
    t0 = time.monotonic()
    with pytest.raises(ConnectionAbortedError):
        _http._handshake(_SilentTLS(), time.monotonic() + DEADLINE_S, aborted)
    assert time.monotonic() - t0 < 0.1 + PROMPT_S
