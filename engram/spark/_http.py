"""One small HTTP POST helper shared by the intent client and the local whisper client.

- Absolute deadline: timeout_s bounds the WHOLE exchange (resolve, connect, TLS handshake,
  send, status line, headers and body), not each socket operation. The exchange runs on a
  short-lived worker thread; the caller stops waiting at the deadline and raises TimeoutError.
- Bounded workers: an abandoned worker must end too, or repeated timeouts pile them up.
  The connection is opened here, not by http.client: each TCP connect attempt gets only the
  time left; for HTTPS the TLS socket is published as conn.sock BEFORE its handshake starts,
  so the deadline's shutdown reaches a handshake that trickles; after that, the same
  shutdown unblocks any read, and the worker checks the deadline between body chunks. Name
  resolution cannot be interrupted from Python, so at most MAX_OUTSTANDING workers may exist
  at once: when they are all still busy, post() fails fast with Busy instead of starting
  another thread.
- Cancellable: pass a CancelToken and cancel it from any thread; the connection is shut down at
  once and post() raises Cancelled without waiting for the server (used to drop a partial
  transcription the moment speech ends).
- Redirects are never followed (http.client has no redirect logic): a 3xx comes back as its
  status code, so a moved or hijacked endpoint cannot forward the request, or its
  Authorization header, anywhere else.
- No proxies: the request goes straight to the URL given. HTTPS verifies the certificate
  and the host name (SNI is sent), exactly as ssl.create_default_context() does.
- Replies are read up to a fixed size; only a 200 reply's body is read.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
import urllib.parse

MAX_REPLY_BYTES = 256 * 1024
MAX_OUTSTANDING = 4          # worker threads (live or abandoned) allowed at once, across all callers
_CHUNK = 16 * 1024
_SLOTS = threading.BoundedSemaphore(MAX_OUTSTANDING)

# Swapped out in tests: a resolver or connect that never answers, or a context trusting a test certificate.
_resolve = socket.getaddrinfo
_new_socket = socket.socket
_tls_context = ssl.create_default_context


class Busy(OSError):
    """Too many earlier requests are still unwinding; refusing to start another."""


class Cancelled(OSError):
    """The request was cancelled through its CancelToken; its connection was closed."""


class CancelToken:
    """Cancels one post() from any thread: the connection is shut down at once and post() raises
    Cancelled straight away, without waiting for the server. One token per request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._hook = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            hook = self._hook
        if hook is not None:
            hook()

    def _attach(self, hook) -> bool:  # noqa: ANN001
        """Run hook on cancel. False if already cancelled (the hook is then not kept)."""
        with self._lock:
            if self._cancelled:
                return False
            self._hook = hook
            return True

    def _detach(self) -> None:
        with self._lock:
            self._hook = None


def _open_tcp(host: str, port: int, deadline: float) -> socket.socket:
    """Connect, giving each attempt only the time left before the deadline."""
    last: OSError | None = None
    for family, kind, proto, _, sockaddr in _resolve(host, port, 0, socket.SOCK_STREAM):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline passed before connecting")
        sock = _new_socket(family, kind, proto)
        try:
            sock.settimeout(left)
            sock.connect(sockaddr)
        except OSError as e:
            last = e
            sock.close()
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock
    raise last or OSError(f"no address for {host}")


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, deadline: float):
        super().__init__(host, port, timeout=max(deadline - time.monotonic(), 0.001))
        self._deadline = deadline

    def connect(self) -> None:
        self.sock = _open_tcp(self.host, self.port, self._deadline)


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, deadline: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=max(deadline - time.monotonic(), 0.001), context=context)
        self._deadline = deadline
        self._tls = context

    def connect(self) -> None:
        raw = _open_tcp(self.host, self.port, self._deadline)
        try:
            tls = self._tls.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
        except BaseException:
            raw.close()
            raise
        self.sock = tls   # published first: from here on the deadline's shutdown can interrupt the handshake
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline passed before the TLS handshake")
        tls.settimeout(left)
        tls.do_handshake()   # verifies the certificate chain and the host name


def _connection(url: str, deadline: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = _HTTPSConnection(parts.hostname, parts.port or 443, deadline,
                                                            _tls_context())
    elif parts.scheme == "http":
        conn = _HTTPConnection(parts.hostname, parts.port or 80, deadline)
    else:
        raise ValueError("only http and https URLs are supported")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return conn, path


def _abort(conn: http.client.HTTPConnection) -> None:
    """Unblock a worker stuck on this connection: TLS handshake or read (called from the waiting thread)."""
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass


def post(url: str, body: bytes, headers: dict[str, str], timeout_s: float,
         cancel: CancelToken | None = None) -> tuple[int, bytes]:
    """POST and return (status, body) within timeout_s in total. Raises TimeoutError at the deadline,
    Cancelled as soon as `cancel` is cancelled (its connection is closed), Busy when too many earlier
    workers are still unwinding, OSError for connection failures and ValueError for an oversized reply."""
    deadline = time.monotonic() + timeout_s
    conn, path = _connection(url, deadline)
    if not _SLOTS.acquire(blocking=False):
        raise Busy(f"{MAX_OUTSTANDING} earlier requests are still outstanding")
    out: dict[str, object] = {}
    finished = threading.Event()   # set by the worker when done, or by a cancel

    def exchange() -> None:
        try:
            conn.connect()
            if cancel is not None and cancel.cancelled:
                raise Cancelled("cancelled while connecting")
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = bytearray()
            if resp.status == 200:
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("reply not complete by the deadline")
                    chunk = resp.read1(_CHUNK)   # at most one socket read, so the deadline is checked often
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > MAX_REPLY_BYTES:
                        raise ValueError("reply too large")
            out["reply"] = (resp.status, bytes(data))
        except BaseException as e:  # handed to the waiting thread, or dropped if it already gave up
            out["error"] = e
        finally:
            conn.close()
            _SLOTS.release()
            finished.set()

    def on_cancel() -> None:   # runs on the cancelling thread
        _abort(conn)
        finished.set()

    if cancel is not None and not cancel._attach(on_cancel):
        _SLOTS.release()
        raise Cancelled("cancelled before it was sent")
    worker = threading.Thread(target=exchange, name="spark-http", daemon=True)
    try:
        worker.start()
    except BaseException:
        _SLOTS.release()
        if cancel is not None:
            cancel._detach()
        raise
    try:
        finished.wait(max(0.0, deadline - time.monotonic()))
    finally:
        if cancel is not None:
            cancel._detach()
    if cancel is not None and cancel.cancelled:
        _abort(conn)
        raise Cancelled("cancelled; the connection was closed")
    if not finished.is_set():
        _abort(conn)
        raise TimeoutError(f"no complete reply within {timeout_s} s")
    if "error" in out:
        raise out["error"]  # type: ignore[misc]
    return out["reply"]  # type: ignore[return-value]
