"""Operations, governance gate, executor, event bus and evidence ledger."""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import logging
import queue
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import WebSocket

from . import config
from .plan import PRIMITIVES, Plan

log = logging.getLogger("console.core")
TELEMETRY_TIMEOUT_S = 0.5


async def best_effort(*calls: Callable[[], Any], timeout_s: float = TELEMETRY_TIMEOUT_S) -> None:
    """Run telemetry calls (bus emits, ledger writes) in order, after a safety action, never before it.
    Each failure is logged and skipped, and all of them together get at most timeout_s, so none can raise
    into or stall the caller. (A call that blocks the event loop could not be cut short; the ledger's writes
    happen on its own thread for that reason.)"""
    async def run() -> None:
        for call in calls:
            try:
                r = call()
                if inspect.isawaitable(r):
                    await r
            except Exception as e:  # noqa: BLE001
                log.warning("telemetry failed: %s: %s", type(e).__name__, e)

    try:
        await asyncio.wait_for(run(), timeout_s)
    except asyncio.TimeoutError:
        log.warning("telemetry took over %.1f s; the rest was skipped", timeout_s)


def _stop_reply(r: Any) -> dict:
    """A stop call's reply as a dict. One that raised becomes the failure record the HTTP robot adapter already
    returns; one that is not a dict at all is kept under "reply" (it confirms nothing)."""
    if isinstance(r, BaseException):
        return {"status": "stop_request_failed", "error": f"{type(r).__name__}: {r}"}
    if not isinstance(r, dict):
        return {"status": "stop_reply_unrecognised", "reply": r}
    return r


def _not_stopped(r: Any) -> str | None:
    """Why this reply does not confirm a stop, or None when it does. Only an explicit "yes" in the target's own
    shape confirms: the robot server's {"stopped": true, "latched": true}, the console mock robot's
    {"status": "stopped"}, or the brain's /stop reply, its robot's state with {"stopped": true} (which also
    carries "connected": false and an error when the brain could not reach its robot). Anything missing,
    malformed or unknown fails closed."""
    if not isinstance(r, dict):
        return f"the reply was {json.dumps(r, default=str)[:80]}"
    if r.get("error") or r.get("status") == "stop_request_failed":
        return str(r.get("error") or "the stop request failed")
    if r.get("connected") is False:
        return "it is not connected to the robot"
    if "latched" in r and r["latched"] is not True:
        return "the stop did not latch"
    if r.get("stopped") is True or ("stopped" not in r and r.get("status") == "stopped"):
        return None
    return f"the reply did not confirm a stop: {json.dumps(r, default=str)[:80]}"


def stop_unconfirmed(res: Any) -> list[str]:
    """What an Engine.stop() result does not confirm: "robot: ..." and/or "brain: ..." for each stop target whose
    reply is not an explicit confirmation (see _not_stopped). An empty list means every target confirmed."""
    robot = {k: v for k, v in res.items() if k != "brain"} if isinstance(res, dict) else res
    parts = [f"robot: {w}" for w in [_not_stopped(robot)] if w]
    if isinstance(res, dict) and "brain" in res and (w := _not_stopped(res["brain"])):
        parts.append(f"brain: {w}")
    return parts


# --------------------------------------------------------------------------- bus
class Bus:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.log: list[dict] = []

    async def emit(self, type_: str, **data: Any) -> dict:
        ev = {"type": type_, "ts": time.time(), **data}
        if type_ not in ("state",):
            self.log.append(ev)
            self.log = self.log[-300:]
        dead = []
        msg = json.dumps(ev, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
        return ev


# ------------------------------------------------------------------------ ledger
class Ledger:
    """Append only JSONL. Evidence format from the team packet:
    ticket | commit | input fixture | procedure | observed result | limitation | next owner

    Every write happens on the ledger's own thread, in order: append() serializes the record and queues it,
    so a slow or stalled disk can never block the event loop (and with it a stop, or any other request).
    Reads (find) happen on a reader thread for the same reason, and also see records that are not on disk yet.
    A write that fails is logged; the caller is not told."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._unwritten: dict[str, str] = {}  # id -> line, until it is on disk
        self._lock = threading.Lock()  # guards _unwritten only; never held during disk I/O
        self._reader = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ledger-reader")
        self.read_timeout_s = 2.0
        threading.Thread(target=self._writer, name="ledger-writer", daemon=True).start()

    def append(self, event: str, **rec: Any) -> str:
        eid = "ev_" + uuid.uuid4().hex[:10]
        line = json.dumps({"id": eid, "kind": event, "ts": time.time(), **rec}, default=str) + "\n"
        with self._lock:
            self._unwritten[eid] = line
        self._queue.put((eid, line))
        return eid

    def _writer(self) -> None:
        while True:
            eid, line = self._queue.get()
            try:
                self._write(line)
            except Exception as e:  # noqa: BLE001
                log.error("ledger write failed, record %s lost: %s: %s", eid, type(e).__name__, e)
            with self._lock:
                self._unwritten.pop(eid, None)

    def _write(self, line: str) -> None:
        with self.path.open("a") as f:
            f.write(line)

    def flush(self, timeout_s: float = 5.0) -> bool:
        """Wait in the calling thread until every queued record is written (or has failed). For shutdown and tests."""
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                if not self._unwritten:
                    return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)

    async def find(self, eid: str) -> dict | None:
        """The first (oldest) record with this id or operation id. The file is read on the ledger's reader thread,
        so a slow or stalled disk can delay only this lookup, by at most read_timeout_s (after which only the
        records not on disk yet are searched), and never the event loop or a stop."""
        with self._lock:  # snapshot first: a record written after this is then in the file
            pending = list(self._unwritten.values())
        try:
            reading = asyncio.get_running_loop().run_in_executor(self._reader, self._scan, eid)
            j = await asyncio.wait_for(reading, self.read_timeout_s)
        except asyncio.TimeoutError:
            log.warning("ledger read took over %.1f s; searched only records not on disk yet", self.read_timeout_s)
            j = None
        except OSError as e:
            log.error("ledger read failed: %s", e)
            j = None
        if j:
            return j
        for line in pending:
            if j := _ledger_match(line, eid):
                return j
        return None

    def _scan(self, eid: str) -> dict | None:
        if not self.path.exists():
            return None
        with self.path.open() as f:
            for line in f:  # oldest first
                if j := _ledger_match(line, eid):
                    return j
        return None


def _ledger_match(line: str, eid: str) -> dict | None:
    if eid not in line:
        return None
    try:
        j = json.loads(line)
    except ValueError:  # a line the writer has not finished
        return None
    return j if j.get("id") == eid or j.get("operation_id") == eid else None


# ------------------------------------------------------------------- operations
class Operation:
    def __init__(self, plan: Plan, source: str, heard: str, bundle: dict, metrics: dict, trace: list[str]) -> None:
        self.id = "op_" + uuid.uuid4().hex[:10]
        self.plan = plan
        self.plan.expires_at = time.time() + config.APPROVAL_TTL_S
        self.digest = plan.digest()
        self.source = source
        self.heard = heard
        self.bundle_id = bundle.get("bundle_id")
        self.facts = bundle.get("facts", [])[:5]
        self.missing = bundle.get("missing_evidence", [])
        self.status = "PENDING_APPROVAL"
        self.steps = [{"i": i, **s.model_dump(), "status": "queued", "result": None,
                       "label": PRIMITIVES[s.op]["label"], "effect": PRIMITIVES[s.op]["effect"]}
                      for i, s in enumerate(plan.steps)]
        self.metrics = metrics
        self.trace = trace
        self.created = time.time()
        self.decided_by: str | None = None
        self.verification: dict | None = None
        self.task: asyncio.Task | None = None

    def public(self) -> dict:
        return {"id": self.id, "status": self.status, "goal": self.plan.goal, "heard": self.heard, "source": self.source,
                "digest": self.digest, "planner": self.plan.planner, "skill_id": self.plan.skill_id,
                "evidence_ids": self.plan.evidence_ids, "facts": self.facts, "missing": self.missing,
                "steps": self.steps, "physical": self.plan.physical(), "expires_at": self.plan.expires_at,
                "metrics": self.metrics, "trace": self.trace, "created": self.created,
                "verification": self.verification, "bundle_id": self.bundle_id}


class Engine:
    def __init__(self, robot, memory, bus: Bus, ledger: Ledger) -> None:
        self.robot = robot
        self.memory = memory
        self.bus = bus
        self.ledger = ledger
        self.ops: dict[str, Operation] = {}
        self.stopped = False
        self.runs: list[dict] = []  # completed runs for the first/second run scoreboard
        self.frames_dir = config.DATA_DIR / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)

    async def stage(self, op: Operation) -> Operation:
        self.ops[op.id] = op
        self.ledger.append("plan_staged", operation_id=op.id, goal=op.plan.goal, digest=op.digest,
                           steps=[s.model_dump() for s in op.plan.steps], evidence_ids=op.plan.evidence_ids,
                           planner=op.plan.planner, metrics=op.metrics)
        await self.bus.emit("operation", op=op.public())
        if not op.plan.physical():
            await self.approve(op.id, op.digest, by="auto:no_physical_effect")
        return op

    async def approve(self, oid: str, digest: str, by: str = "operator") -> tuple[bool, str]:
        op = self.ops.get(oid)
        if not op:
            return False, "unknown operation"
        if op.status != "PENDING_APPROVAL":
            return False, f"operation is {op.status}"
        if time.time() > op.plan.expires_at:
            op.status = "EXPIRED"
            await self.bus.emit("operation", op=op.public())
            return False, "approval window expired; re-issue the command"
        if digest != op.digest:
            return False, "plan changed since preview (digest mismatch)"
        if self.stopped:
            return False, "E-STOP latched. Clear stop first."
        st = await self.robot.state()
        if op.plan.physical() and st.get("estop"):
            return False, "robot reports E-STOP"
        if op.plan.physical() and not st.get("connected", True):
            return False, "robot unreachable"
        op.status = "APPROVED"
        op.decided_by = by
        self.ledger.append("approved", operation_id=oid, digest=digest, by=by)
        await self.bus.emit("operation", op=op.public())
        op.task = asyncio.create_task(self._run(op))
        return True, "approved"

    async def reject(self, oid: str, by="operator") -> bool:
        op = self.ops.get(oid)
        if not op or op.status != "PENDING_APPROVAL":
            return False
        op.status = "REJECTED"
        op.decided_by = by
        self.ledger.append("rejected", operation_id=oid, by=by)
        await self.bus.emit("operation", op=op.public())
        return True

    async def _snap(self, name: str) -> str | None:
        try:
            jpg = await self.robot.frame()
            p = self.frames_dir / f"{name}.jpg"
            p.write_bytes(jpg)
            return f"frame:{name}"
        except Exception:  # noqa: BLE001
            return None

    async def _run(self, op: Operation) -> None:
        if getattr(op, "remote", False):
            from . import brain
            await brain.run(self, op)
            return
        op.status = "EXECUTING"
        t0 = time.time()
        await self.bus.emit("operation", op=op.public())
        prim_calls = 0
        final = "VERIFIED"
        for s in op.steps:
            if self.stopped:
                s["status"] = "skipped"
                final = "STOPPED"
                continue
            if final != "VERIFIED" and final != "NEEDS_VERIFICATION":
                s["status"] = "skipped"
                continue
            s["status"] = "running"
            await self.bus.emit("operation", op=op.public())
            st = await self.robot.state()
            if s["effect"] == "motion" and s["args"].get("distance_cm", 1) > 0 and st.get("veto"):
                _cm = st.get("lidar_min_cm")
                s["status"], s["result"] = "vetoed", {
                    "status": "vetoed", "by": "lidar", "lidar_min_cm": _cm,
                    "reason": f"obstacle {round(_cm)} cm ahead (LiDAR veto)" if _cm is not None else "obstacle ahead (LiDAR veto)",
                }
                final = "HALTED"
                await self.bus.emit("veto", step=s["i"], operation_id=op.id, lidar_min_cm=st.get("lidar_min_cm"))
                continue
            prim_calls += 1
            res = await self.robot.execute(s["op"], s["args"], f"{op.id}.{s['i']}")
            res["evidence_frame"] = await self._snap(f"{op.id}_{s['i']}")
            s["result"] = res
            status = res.get("status", "unknown")
            s["status"] = status
            self.ledger.append("primitive", operation_id=op.id, step=s["i"], op=s["op"], args=s["args"], result=res)
            if status == "succeeded":
                # A primitive "succeeded" only counts as verified if the robot names a sensor.
                if s["effect"] != "none" and not (res.get("postcondition") or {}).get("verified_by"):
                    if s["i"] == len(op.steps) - 1:
                        final = "NEEDS_VERIFICATION"
            elif status == "unknown":
                final = "NEEDS_VERIFICATION"  # never retried automatically
                break
            elif status in ("vetoed",):
                final = "HALTED"
            elif status in ("stopped",):
                final = "STOPPED"
            else:
                final = "FAILED"
            await self.bus.emit("operation", op=op.public())
        for s in op.steps:
            if s["status"] == "queued":
                s["status"] = "skipped"
        op.status = final
        op.metrics["primitive_calls"] = prim_calls
        op.metrics["exec_ms"] = int((time.time() - t0) * 1000)
        op.metrics["total_ms"] = op.metrics.get("plan_ms", 0) + op.metrics["exec_ms"]
        self.ledger.append("outcome", operation_id=op.id, outcome=final, metrics=op.metrics)
        await self.bus.emit("operation", op=op.public())
        if final == "VERIFIED":
            await self._finish(op, "verified", "sensor")
        elif final == "NEEDS_VERIFICATION":
            await self.bus.emit("say", text="I can't confirm that with my sensors. Please verify on screen.")

    async def verify(self, oid: str, ok: bool, note: str = "") -> bool:
        op = self.ops.get(oid)
        if not op or op.status not in ("NEEDS_VERIFICATION", "FAILED", "HALTED"):
            return False
        op.status = "VERIFIED" if ok else "FAILED"
        op.verification = {"by": "operator", "ok": ok, "note": note, "ts": time.time()}
        self.ledger.append("operator_verification", operation_id=oid, ok=ok, note=note)
        await self.bus.emit("operation", op=op.public())
        if ok:
            await self._finish(op, "verified", "operator")
        return True

    async def _finish(self, op: Operation, outcome: str, by: str) -> None:
        # A run is only comparable on the scoreboard if every step actually
        # succeeded (no vetoed/failed/stopped/skipped/unknown step). A step with
        # no physical effect that the robot marks "succeeded" still counts.
        all_ok = all(s["status"] == "succeeded" for s in op.steps)
        run = {"operation_id": op.id, "goal": op.plan.goal, "planner": op.plan.planner, "skill_id": op.plan.skill_id,
               "signature": "|".join(f"{s['op']}" for s in op.steps), **op.metrics, "verified_by": by,
               "all_steps_succeeded": all_ok, "ts": time.time()}
        self.runs.append(run)
        await self.bus.emit("run", run=run, runs=self.runs[-10:])
        if not op.plan.physical():
            return
        try:
            ep = await self.memory.record_episode({
                "operation_id": op.id, "goal": op.plan.goal, "outcome": outcome, "verified_by": by,
                "steps": [{"op": s["op"], "args": s["args"], "status": s["status"]} for s in op.steps],
                "evidence_ids": op.plan.evidence_ids, "metrics": op.metrics, "skill_id": op.plan.skill_id})
            await self.bus.emit("episode", episode_id=ep.get("id"), operation_id=op.id)
            await self.bus.emit("skills", skills=await self.memory.list_skills())
        except Exception as e:  # noqa: BLE001
            await self.bus.emit("log", level="warn", msg=f"episode not recorded: {e}")

    async def stop(self, by="operator") -> dict:
        """E-STOP. The latch, the cancellations and the stop calls to the robot and the brain all happen before
        any telemetry, which comes last and is best effort: a stalled HUD client or a failed ledger write can
        neither delay nor skip a stop."""
        self.stopped = True  # the executor checks this before every step, and approve() refuses while it is set
        for op in self.ops.values():
            if op.status == "PENDING_APPROVAL":
                op.status = "CANCELLED"
        from . import brain
        # Both at once, each failure on its own: a slow or failing robot call must not hold up the brain's stop.
        robot_res, *brain_res = await asyncio.gather(self.robot.stop(), *([brain.stop()] if brain.enabled() else []),
                                                     return_exceptions=True)
        res = _stop_reply(robot_res)
        if brain_res:
            res = {**res, "brain": _stop_reply(brain_res[0])}
        unconfirmed = stop_unconfirmed(res)
        await best_effort(lambda: self.ledger.append("estop", by=by, robot=res, unconfirmed=unconfirmed),
                          lambda: self.bus.emit("stop", by=by, robot=res, confirmed=not unconfirmed),
                          *([lambda: self.bus.emit("log", level="error", msg="E-STOP NOT CONFIRMED ("
                                                   + "; ".join(unconfirmed) + "). Cut the motor power.")]
                            if unconfirmed else []),
                          *[lambda op=op: self.bus.emit("operation", op=op.public()) for op in list(self.ops.values())])
        return res

    async def clear_stop(self) -> dict:
        res = await self.robot.clear_stop()
        from . import brain
        if brain.enabled():
            res = {**(res or {}), "brain": await brain.clear_stop()}
        self.stopped = False
        self.ledger.append("estop_clear", robot=res)
        await self.bus.emit("stop_clear", robot=res)
        return res


# ------------------------------------------------------------------ net + hash
def upstream_online(timeout=0.7) -> bool:
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def hash_models(d: Path) -> list[dict]:
    out = []
    if not d.exists():
        return out
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix in (".bin", ".gguf", ".onnx", ".pt", ".tflite"):
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out.append({"file": p.name, "bytes": p.stat().st_size, "sha256": h.hexdigest()})
    return out
