"""Append-only operation journal (JSON lines).

Every physical op writes `started` before touching hardware and `finished` after. On restart, an
op with `started` but no `finished` had an uncertain side effect: it is recorded as UNKNOWN /
E_INTERRUPTED and is never re-executed (the same operation_id returns that UNKNOWN result).
"""
import json
import os
import threading
import time


class Journal:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.results: dict[str, dict] = {}
        self.recovered: list[str] = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        started: dict[str, dict] = {}
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn final line after power loss
                oid = rec.get("operation_id")
                if rec.get("phase") == "started":
                    started[oid] = rec
                elif rec.get("phase") == "finished":
                    self.results[oid] = rec["result"]
                    started.pop(oid, None)
        for oid, rec in started.items():
            result = {
                "operation_id": oid, "op": rec.get("op"), "status": "UNKNOWN", "code": "E_INTERRUPTED",
                "message": "service stopped while this operation was in progress; side effect uncertain, not replayed",
                "postcondition": {"verified": False}, "data": {}, "timing": {"started_at": rec.get("ts")},
            }
            self.finished(result)
            self.recovered.append(oid)

    def _write(self, rec: dict):
        with self._lock, open(self.path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def started(self, operation_id: str, op: str, args: dict):
        self._write({"ts": time.time(), "phase": "started", "operation_id": operation_id, "op": op, "args": args})

    def finished(self, result: dict):
        self.results[result["operation_id"]] = result
        self._write({"ts": time.time(), "phase": "finished", "operation_id": result["operation_id"], "result": result})

    def tail(self, n: int = 50) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            lines = f.readlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out
