"""Run the brain API on IPv6 and IPv4 at once (uvicorn --host :: is IPv6 only on the Pi).

    python scripts/serve_dual.py --port 8001

Phone hotspots often give the Mac only IPv6, while an iPad may come in over IPv4. One IPv6 socket that also
takes IPv4 is handed to uvicorn; IPv4 only on a machine without IPv6. Works on Linux, macOS and Windows.
"""

import argparse
import os
import socket
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so engram imports from the repo


def listen(port: int) -> socket.socket:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        host = "::"
    except OSError:  # no IPv6 here
        sock, host = socket.socket(socket.AF_INET, socket.SOCK_STREAM), "0.0.0.0"
    if os.name != "nt":  # on Windows this would let a second copy bind the same port without an error
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    a = ap.parse_args()
    sock = listen(a.port)
    # Server.run(sockets=...) rather than run(fd=...): the fd path assumes AF_UNIX, which Windows lacks
    uvicorn.Server(uvicorn.Config("engram.api:create_app", factory=True, log_level="info")).run(sockets=[sock])


if __name__ == "__main__":
    main()
