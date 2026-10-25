"""Short-lived, single-operation authorization for physical effects.

The approval gate (Bharath's deterministic safety/authority gate, after the human approves on the
iPad) signs exactly one {operation_id, op, args} with a shared secret. The robot service verifies
it. Same secret on both sides: env ENGRAM_ROBOT_SECRET.

    token = sign(secret, operation_id, op, args, ttl_s=30)

CLI for testing:  python -m robot.auth <operation_id> <op> '<args json>'
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time


def args_digest(args: dict) -> str:
    canon = json.dumps(args or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(secret: str, operation_id: str, op: str, args: dict, ttl_s: float = 30, now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = {"operation_id": operation_id, "op": op, "args_sha256": args_digest(args), "exp": now + ttl_s}
    body = _b64(json.dumps(payload, sort_keys=True).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify(secret: str, token: str | None, operation_id: str, op: str, args: dict, ttl_max_s: float) -> tuple[bool, str]:
    if not token:
        return False, "missing authorization for physical effect"
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False, "bad signature"
        p = json.loads(_unb64(body))
    except Exception:
        return False, "malformed token"
    now = time.time()
    if p.get("exp", 0) < now:
        return False, "authorization expired"
    if p["exp"] - now > ttl_max_s + 1:
        return False, "authorization lifetime too long"
    if p.get("operation_id") != operation_id:
        return False, "token is for a different operation_id"
    if p.get("op") != op:
        return False, "token is for a different op"
    if p.get("args_sha256") != args_digest(args):
        return False, "args differ from what was approved"
    return True, "ok"


if __name__ == "__main__":
    secret = os.environ.get("ENGRAM_ROBOT_SECRET", "dev-secret-change-me")
    print(sign(secret, sys.argv[1], sys.argv[2], json.loads(sys.argv[3] if len(sys.argv) > 3 else "{}")))
