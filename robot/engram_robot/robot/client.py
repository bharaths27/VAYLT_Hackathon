"""Client for the brain Pi. Standard library only: copy this one file to the brain.

    from client import RobotClient
    bot = RobotClient("http://10.0.0.2:8200", secret=os.environ["ENGRAM_ROBOT_SECRET"], name="brain")
    print(bot.execute("observe"))                                # read-only: no approval needed
    r = bot.execute("pick_tagged", {"tag_id": 7}, approved=True)  # physical: only after approval
    if r["status"] != "SUCCEEDED": ...                            # UNKNOWN is not success
    bot.close()

Heartbeats: while the client is open it pings /robot/heartbeat every 0.4 s. If the brain crashes,
hangs or loses the network, the robot stops moving within link_timeout_s (1.5 s) -> E_LINK_LOST.

approved=True is the gate: the brain's deterministic safety/authority check (and the human approval
on the iPad) must happen BEFORE passing it. Without it, physical ops come back REJECTED/E_AUTH.
"""
import hashlib
import hmac
import base64
import json
import threading
import time
import urllib.error
import urllib.request
import uuid


def _sign(secret, operation_id, op, args, ttl_s=30):
    canon = json.dumps(args or {}, sort_keys=True, separators=(",", ":"))
    payload = {"operation_id": operation_id, "op": op,
               "args_sha256": hashlib.sha256(canon.encode()).hexdigest(), "exp": time.time() + ttl_s}
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
    return body + "." + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


class RobotClient:
    def __init__(self, base_url="http://10.0.0.2:8200", secret="dev-secret-change-me", name="brain",
                 heartbeat=True, timeout_s=90):
        self.base, self.secret, self.name, self.timeout = base_url.rstrip("/"), secret, name, timeout_s
        self._caps = None
        self._running = heartbeat
        self.link_ok = None
        if heartbeat:
            threading.Thread(target=self._beat, daemon=True, name="robot-heartbeat").start()

    # ---------- HTTP ----------
    def _req(self, method, path, body=None, timeout=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read() if raw else json.load(r)
        except urllib.error.HTTPError as e:  # REJECTED results come back as 4xx with a JSON body
            return json.loads(e.read() or b"{}")

    def _beat(self):
        while self._running:
            try:
                self._req("POST", "/robot/heartbeat", {"client": self.name}, timeout=1.0)
                self.link_ok = True
            except Exception:
                self.link_ok = False
            time.sleep(0.4)

    # ---------- API ----------
    def capabilities(self, refresh=False):
        if self._caps is None or refresh:
            self._caps = self._req("GET", "/robot/capabilities")
        return self._caps

    def is_physical(self, op):
        return any(p["op"] == op and p["physical_effect"] for p in self.capabilities()["primitives"])

    def execute(self, op, args=None, approved=False, operation_id=None, ttl_s=30):
        """Run one primitive and block until its result. operation_id makes retries idempotent:
        resending the same id returns the stored result instead of moving again."""
        args = args or {}
        operation_id = operation_id or f"{self.name}-{uuid.uuid4().hex[:16]}"
        body = {"op": op, "args": args, "operation_id": operation_id}
        if approved and self.is_physical(op):
            body["authorization"] = _sign(self.secret, operation_id, op, args, ttl_s)
        return self._req("POST", "/robot/execute", body)

    def stop(self, latch=True, reason="brain e-stop"):
        return self._req("POST", "/robot/stop", {"latch": latch, "reason": reason}, timeout=2)

    def reset(self, operator):
        return self._req("POST", "/robot/reset", {"operator": operator})

    def state(self):
        return self._req("GET", "/robot/state", timeout=3)

    def frame_jpeg(self, annotate=True, quality=70):
        return self._req("GET", f"/robot/frame?annotate={'true' if annotate else 'false'}&quality={int(quality)}",
                         timeout=3, raw=True)

    def journal(self, limit=50):
        return self._req("GET", f"/robot/journal?limit={limit}")

    def close(self):
        """Clean shutdown: stop heartbeats and tell the robot the brain left on purpose."""
        self._running = False
        try:
            self._req("POST", "/robot/link/release", {}, timeout=2)
        except Exception:
            pass
