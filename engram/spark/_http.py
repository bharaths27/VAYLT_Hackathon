"""One small HTTP POST helper shared by the intent client and the local whisper client.

- Absolute deadline: timeout_s bounds the WHOLE exchange (resolve, connect, TLS handshake,
  send, status line, headers and body), not each socket operation. The exchange runs on a
  short-lived worker thread; the caller stops waiting at the deadline and raises TimeoutError.
- Bounded workers: an abandoned worker must end too, or repeated timeouts and cancels pile them
  up. Every blocking step ends at the deadline and stops early on an abort (cancel or deadline):
  the TCP connect and sending the request wait in short slices that check for the abort; the
  TLS socket is published as conn.sock BEFORE its handshake starts, so the abort's shutdown
  reaches a handshake that trickles, and the same shutdown unblocks any read; the worker checks
  the deadline between body chunks. Name resolution cannot be interrupted from Python (an
  IP-literal host needs none), so at most MAX_OUTSTANDING workers may exist at once: when they
  are all still busy, post() fails fast with Busy instead of starting another thread.
- Cancellable: pass a CancelToken and cancel it from any thread; the connection is aborted at
  once, post() raises Cancelled without waiting for the server, and the worker gives its slot
  back within a slice (used to drop a partial transcription the moment speech ends).
- Redirects are never followed (http.client has no redirect logic): a 3xx comes back as its
  status code, so a moved or hijacked endpoint cannot forward the request, or its
  Authorization header, anywhere else.
- No proxies: the request goes straight to the URL given. HTTPS verifies the certificate
  and the host name (SNI is sent), exactly as ssl.create_default_context() does.
- Replies are read up to a fixed size; only a 200 reply's body is read.
"""

from __future__ import annotations

import errno
import http.client
import io
import os
import select
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


_SLICE_S = 0.02   # connect and send never block longer than this, so an abort is seen within it
_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY}


def _writable(sock: socket.socket, timeout: float) -> bool:   # swapped out in tests
    _, w, x = select.select([], [sock], [sock], timeout)
    return bool(w or x)


def _connect(sock: socket.socket, sockaddr, deadline: float, aborted: threading.Event) -> None:  # noqa: ANN001
    """A TCP connect that ends at the deadline, or within a slice of an abort."""
    sock.setblocking(False)
    err = sock.connect_ex(sockaddr)
    while err in _IN_PROGRESS:
        if _writable(sock, _step(deadline, aborted, "connect")):
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if err:
        raise OSError(err, os.strerror(err))


def _open_tcp(host: str, port: int, deadline: float, aborted: threading.Event) -> socket.socket:
    """Connect by the deadline, or give up within a slice of an abort. The host-name lookup before it is
    the one step that cannot be interrupted; an IP-literal host (the loopback whisper server) needs none."""
    last: OSError | None = None
    for family, kind, proto, _, sockaddr in _resolve(host, port, 0, socket.SOCK_STREAM):
        if aborted.is_set():
            raise ConnectionAbortedError("connect aborted")
        if deadline - time.monotonic() <= 0:
            raise TimeoutError("deadline passed before connecting")
        sock = _new_socket(family, kind, proto)
        try:
            _connect(sock, sockaddr, deadline, aborted)
        except (TimeoutError, ConnectionAbortedError):
            sock.close()
            raise
        except OSError as e:
            last = e
            sock.close()
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.settimeout(max(deadline - time.monotonic(), 0.001))
        return sock
    raise last or OSError(f"no address for {host}")


def _readable(sock: socket.socket, timeout: float) -> bool:
    r, _, x = select.select([sock], [], [sock], timeout)
    return bool(r or x)


def _step(deadline: float, aborted: threading.Event, what: str) -> float:
    """The length of the next slice; raises once the deadline passed or the request was aborted."""
    if aborted.is_set():
        raise ConnectionAbortedError(f"{what} aborted")
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError(f"{what} did not finish by the deadline")
    return min(_SLICE_S, left)


def _handshake(tls: ssl.SSLSocket, deadline: float, aborted: threading.Event) -> None:
    """The TLS handshake (it verifies the certificate chain and the host name), in slices."""
    tls.setblocking(False)
    while True:
        wait = _step(deadline, aborted, "TLS handshake")
        try:
            tls.do_handshake()
            return
        except ssl.SSLWantReadError:
            _readable(tls, wait)
        except ssl.SSLWantWriteError:
            _writable(tls, wait)


def _send_all(sock: socket.socket, data, deadline: float, aborted: threading.Event) -> None:  # noqa: ANN001
    """sendall() in slices. (Shutting a socket down does not wake a blocked send on every platform.)"""
    view = memoryview(data).cast("B")
    while view:
        sock.settimeout(_step(deadline, aborted, "sending the request"))
        try:
            sent = sock.send(view)
        except socket.timeout:
            continue          # nothing was sent in this slice; the same bytes are offered again
        view = view[sent:]


class _SlicedReader(io.RawIOBase):
    """What http.client reads the reply through: recv in slices, so the read ends at the deadline or
    within a slice of an abort. (Shutting a socket down does not wake a blocked recv on every platform:
    on Windows it stays blocked while unsent data is still queued.)"""

    def __init__(self, owner: "_AbortableSocket"):
        self._owner = owner

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # noqa: ANN001
        o = self._owner
        while True:
            o._sock.settimeout(_step(o._deadline, o._aborted, "reading the reply"))
            try:
                return o._sock.recv_into(b)
            except socket.timeout:
                continue

    def close(self) -> None:
        if not self.closed:
            super().close()
            self._owner._reader_closed()


class _AbortableSocket:
    """The connected socket as http.client sees it: sendall() and makefile() go through the sliced
    send and read above; everything else is the real socket's. Like socket.makefile(), an open reader
    keeps the socket open after close() (http.client closes the connection and lets the response read
    the rest of a "Connection: close" reply)."""

    def __init__(self, sock: socket.socket, deadline: float, aborted: threading.Event):
        self._sock, self._deadline, self._aborted = sock, deadline, aborted
        self._readers = 0
        self._close_wanted = False

    def sendall(self, data) -> None:  # noqa: ANN001
        _send_all(self._sock, data, self._deadline, self._aborted)

    def makefile(self, mode: str = "rb", *args, **kwargs) -> io.BufferedReader:  # noqa: ANN002, ANN003
        if mode != "rb":
            raise ValueError("only binary reads are supported")
        self._readers += 1
        return io.BufferedReader(_SlicedReader(self))

    def _reader_closed(self) -> None:
        self._readers -= 1
        if self._close_wanted and self._readers == 0:
            self._sock.close()

    def close(self) -> None:
        self._close_wanted = True
        if self._readers == 0:
            self._sock.close()

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._sock, name)


class _Interruptible:
    """What both connections share: every blocking step (connect, TLS handshake, send, read) waits in
    slices that end at the deadline and stop within a slice of _abort()."""

    sock: socket.socket | _AbortableSocket | None

    def _setup(self, deadline: float) -> None:
        self._deadline = deadline
        self._aborted = threading.Event()

    def _ready(self, sock: socket.socket) -> None:
        self.sock = _AbortableSocket(sock, self._deadline, self._aborted)


class _HTTPConnection(_Interruptible, http.client.HTTPConnection):
    def __init__(self, host: str, port: int, deadline: float):
        super().__init__(host, port, timeout=max(deadline - time.monotonic(), 0.001))
        self._setup(deadline)

    def connect(self) -> None:
        self._ready(_open_tcp(self.host, self.port, self._deadline, self._aborted))


class _HTTPSConnection(_Interruptible, http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, deadline: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=max(deadline - time.monotonic(), 0.001), context=context)
        self._setup(deadline)
        self._tls = context

    def connect(self) -> None:
        raw = _open_tcp(self.host, self.port, self._deadline, self._aborted)
        try:
            tls = self._tls.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
        except BaseException:
            raw.close()
            raise
        self.sock = tls   # published first, so _abort() can also shut it down during the handshake
        _handshake(tls, self._deadline, self._aborted)
        self._ready(tls)


def _connection(url: str, deadline: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urllib.parse.urlsplit(url)
    port = parts.port   # ValueError for a port that is not a number in 0-65535
    if port == 0:       # never a destination; `port or 80` would have sent the body to port 80
        raise ValueError("port 0 is not a valid destination port")
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = _HTTPSConnection(parts.hostname, 443 if port is None else port, deadline,
                                                            _tls_context())
    elif parts.scheme == "http":
        conn = _HTTPConnection(parts.hostname, 80 if port is None else port, deadline)
    else:
        raise ValueError("only http and https URLs are supported")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return conn, path


def _abort(conn: http.client.HTTPConnection) -> None:
    """Unblock a worker stuck on this connection, whatever step it is in (called from another thread):
    a connect or send in progress sees the abort within a slice; a TLS handshake or a read is woken
    by shutting the published socket down."""
    aborted = getattr(conn, "_aborted", None)
    if aborted is not None:
        aborted.set()
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
