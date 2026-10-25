"""Serve the console on IPv6 and IPv4 at once (uvicorn --host 0.0.0.0 is IPv4 only, --host :: IPv6 only on the Pi).

    python3 scripts/serve_dual.py --port 8000
    python3 scripts/serve_dual.py --port 8443 --ssl-keyfile certs/server.key --ssl-certfile certs/server.crt

Phone hotspots often give laptops only IPv6, while an iPad comes in over IPv4. Same idea as the brain's
scripts/serve_dual.py: one IPv6 socket that also takes IPv4, handed to uvicorn. IPv4 only on a machine without IPv6.
Works on Linux, macOS and Windows.
"""

import argparse
import os
import socket
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so server.main imports from console/


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
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--ssl-keyfile")
    ap.add_argument("--ssl-certfile")
    ap.add_argument("--log-level", default="info")
    a = ap.parse_args()
    sock = listen(a.port)
    # Server.run(sockets=...) rather than run(fd=...): the fd path assumes AF_UNIX, which Windows lacks
    uvicorn.Server(uvicorn.Config("server.main:app", ssl_keyfile=a.ssl_keyfile, ssl_certfile=a.ssl_certfile,
                                  log_level=a.log_level)).run(sockets=[sock])


if __name__ == "__main__":
    main()
