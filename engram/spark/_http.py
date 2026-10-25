"""One small HTTP POST helper shared by the intent client and the local whisper client.

- Absolute deadline: timeout_s bounds the WHOLE exchange (connect, send, status line,
  headers and body), not each socket operation. A server that trickles one byte at a time
  cannot hold the caller past the deadline: the exchange runs on a short-lived worker
  thread, the caller stops waiting at the deadline and raises TimeoutError, and the socket
  is shut down so the abandoned worker ends as well. The worker also checks the deadline
  between body chunks.
- Redirects are never followed (http.client has no redirect logic): a 3xx comes back as its
  status code, so a moved or hijacked endpoint cannot forward the request, or its
  Authorization header, anywhere else.
- No proxies: the request goes straight to the URL given. HTTPS verifies the certificate
  and host name.
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
_CHUNK = 16 * 1024


def _connection(url: str, timeout_s: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=timeout_s, context=ssl.create_default_context())
    elif parts.scheme == "http":
        conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout_s)
    else:
        raise ValueError("only http and https URLs are supported")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return conn, path


def _abort(conn: http.client.HTTPConnection) -> None:
    """Unblock a worker stuck reading from this connection (called from the waiting thread)."""
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass


def post(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    """POST and return (status, body) within timeout_s in total. Raises TimeoutError at the deadline,
    OSError for connection failures and ValueError for an oversized reply."""
    deadline = time.monotonic() + timeout_s
    conn, path = _connection(url, timeout_s)
    out: dict[str, object] = {}

    def exchange() -> None:
        try:
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

    worker = threading.Thread(target=exchange, name="spark-http", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        _abort(conn)
        raise TimeoutError(f"no complete reply within {timeout_s} s")
    if "error" in out:
        raise out["error"]  # type: ignore[misc]
    return out["reply"]  # type: ignore[return-value]
