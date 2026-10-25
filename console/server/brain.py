"""Bridge to Bharath's ENGRAM brain API (EC08). Off unless ENGRAM_BRAIN_URL is set.

When on, the console stops planning by itself: every command goes to the brain's
POST /commands, the brain's proposal is shown as the pending plan (its own
operation_id + digest), and Hold-to-Approve calls the brain's
POST /operations/{id}/approve with that exact digest. The brain's executor owns
robot signing and execution; the console never calls the robot for these plans.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from . import config

URL = config.BRAIN_URL.rstrip("/") if config.BRAIN_URL != "off" else ""
WHO = "verna-ipad"
_c = httpx.AsyncClient(timeout=20.0) if URL else None

# Brain step ops -> what the plan card shows. Unknown ops still render (generic label).
LABELS = {
    "locate_tag": ("Find the object's tag", "none"),
    "pick_tagged_object": ("Pick up the object", "gripper"),
    "place_at_zone": ("Put it in the zone", "motion"),
    "go_to_zone": ("Drive to the zone", "motion"),
    "speak": ("Say it out loud", "none"),
    "wait": ("Wait", "none"),
    "stop": ("Stop all motion", "none"),
}
STEP_STATUS = {"done": "succeeded", "ok": "succeeded", "succeeded": "succeeded", "failed": "failed",
               "skipped": "skipped", "stopped": "stopped", "unknown": "unknown", "vetoed": "vetoed"}


def enabled() -> bool:
    return bool(URL)


async def _post(path: str, body: dict | None = None, timeout: float | None = None) -> dict:
    extra = {"timeout": timeout} if timeout else {}  # quick calls (stop, health) keep the client's 20 s
    r = await _c.post(URL + path, json=body or {}, **extra)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        raise RuntimeError(f"brain {path} -> {r.status_code}: {detail}")
    return r.json()


async def _get(path: str, timeout: float | None = None) -> Any:
    r = await _c.get(URL + path, **({"timeout": timeout} if timeout else {}))
    r.raise_for_status()
    return r.json()


async def health() -> dict:
    try:
        return await _get("/health")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


async def command(text: str) -> dict:
    return await _post("/commands", {"text": text, "who": WHO}, timeout=config.BRAIN_PLAN_TIMEOUT_S)


async def pending() -> list[dict]:
    try:
        return await _get("/operations/pending")
    except Exception:  # noqa: BLE001
        return []


async def running() -> list[dict]:
    """The plans the brain is executing now, whoever started them. Raises if the brain cannot say within
    config.BRAIN_QUICK_TIMEOUT_S, whatever the transport (a stop may be waiting on the answer): the caller then
    assumes one is running."""
    return await asyncio.wait_for(_get("/operations/running", timeout=config.BRAIN_QUICK_TIMEOUT_S),
                                  config.BRAIN_QUICK_TIMEOUT_S)


async def approve(oid: str, digest: str) -> dict:
    # The brain answers only when the whole plan has run on the robot.
    return await _post(f"/operations/{oid}/approve", {"digest": digest, "approved_by": WHO},
                       timeout=config.BRAIN_RUN_TIMEOUT_S)


async def stop() -> dict:
    try:
        return await _post("/stop")
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def clear_stop() -> dict:
    try:
        return await _post("/robot/clear_stop")
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _items(mem: dict, key: str) -> list[dict]:
    """The brain sends some memory items as plain strings (constraints always, after a failed step): a string is
    that item's text. Calling .get on one crashed every plan that carried a constraint."""
    return [x if isinstance(x, dict) else {"text": str(x)} for x in (mem.get(key) or [])]


def to_bundle(mem: dict | None) -> dict:
    """Brain MemoryBundle -> the console's memory panel shape."""
    mem = mem or {}
    facts = [{"id": f.get("chunk_id") or f.get("id") or f"fact{i}", "text": f.get("statement") or f.get("text") or str(f),
              "source_id": f.get("chunk_id") or f.get("source_id") or ""} for i, f in enumerate(_items(mem, "facts"))]
    for e in _items(mem, "evidence"):
        facts.append({"id": e.get("chunk_id") or e.get("id") or "", "text": e.get("text") or e.get("quote") or str(e),
                      "source_id": e.get("chunk_id") or e.get("source_id") or ""})
    binds = [{"id": o.get("id"), "name": o.get("name") or o.get("text"), "aliases": [], "tag_id": o.get("tag"),
              "source_id": f"memory:{o.get('id')}", "last_seen": o.get("statement")} for o in _items(mem, "objects")]
    cons = [{"id": c.get("id", f"c{i}"), "text": c.get("statement") or c.get("text") or str(c),
             "source_id": c.get("chunk_id") or c.get("source_id") or ""} for i, c in enumerate(_items(mem, "constraints"))]
    return {"bundle_id": "brain", "goal": mem.get("goal"), "facts": facts[:6], "bindings": binds, "zones": [],
            "constraints": cons, "skills": mem.get("applicable_skills", []),
            "missing_evidence": mem.get("missing", []), "source_ids": [f["source_id"] for f in facts if f["source_id"]]}


class _Step:
    def __init__(self, d: dict) -> None:
        self._d = {"op": d.get("op"), "args": d.get("args", {})}

    def model_dump(self) -> dict:
        return dict(self._d)


class _Plan:
    def __init__(self, goal: str, physical: bool, evidence_ids: list[str], source: str, steps: list[dict]) -> None:
        self.goal, self._physical, self.evidence_ids = goal, physical, evidence_ids
        self.steps = [_Step(x) for x in steps]
        self.planner = f"engram-api:{source}"
        self.skill_id = None
        self.expires_at = time.time() + config.APPROVAL_TTL_S

    def physical(self) -> bool:
        return self._physical


class RemoteOperation:
    """Looks like core.Operation to the engine and the UI, but runs on the brain."""
    remote = True

    def __init__(self, p: dict, heard: str, source: str, metrics: dict) -> None:
        plan = p.get("plan") or {}
        ev = [str(e.get("chunk_id") or e.get("id") or e) if isinstance(e, dict) else str(e)
              for e in (plan.get("evidence") or [])]
        ev += [str(r.get("chunk_id") or r.get("id") or r) if isinstance(r, dict) else str(r)
               for r in (plan.get("rules_used") or [])]
        physical = bool((p.get("check") or {}).get("physical", True))
        self.id = p["operation_id"]
        self.plan = _Plan(plan.get("goal") or heard, physical, ev, p.get("source", "?"), plan.get("steps", []))
        self.digest = p["digest"]
        self.source, self.heard = source, heard
        b = to_bundle(p.get("memory"))
        self.bundle_id, self.facts, self.missing = "brain", b["facts"][:5], b["missing_evidence"]
        self.status = "PENDING_APPROVAL"
        self.steps = []
        for i, s in enumerate(plan.get("steps", [])):
            label, effect = LABELS.get(s.get("op"), (s.get("op", "step").replace("_", " ").capitalize(), "motion"))
            self.steps.append({"i": i, "op": s.get("op"), "args": s.get("args", {}), "status": "queued",
                               "result": None, "label": label, "effect": effect})
        self.metrics = {**metrics, "planner_calls": p.get("planner_calls", 0), "brain_planning_ms": p.get("planning_ms")}
        self.trace = [f"brain: {p.get('source', '?')} planner, checker ok={ (p.get('check') or {}).get('ok') }"]
        self.created = time.time()
        self.decided_by = None
        self.verification = None
        self.task = None

    def public(self) -> dict:
        return {"id": self.id, "status": self.status, "goal": self.plan.goal, "heard": self.heard, "source": self.source,
                "digest": self.digest, "planner": self.plan.planner, "skill_id": None,
                "evidence_ids": self.plan.evidence_ids, "facts": self.facts, "missing": self.missing,
                "steps": self.steps, "physical": self.plan.physical(), "expires_at": self.plan.expires_at,
                "metrics": self.metrics, "trace": self.trace, "created": self.created,
                "verification": self.verification, "bundle_id": self.bundle_id, "brain": URL}


async def run(engine, op: RemoteOperation) -> None:
    """Approve on the brain with the exact digest shown, then mirror its reported outcome."""
    op.status = "EXECUTING"
    for s in op.steps:
        s["status"] = "running" if s["i"] == 0 else "queued"
    await engine.bus.emit("operation", op=op.public())
    t0 = time.time()
    try:
        res = await approve(op.id, op.digest)
    except Exception as e:  # noqa: BLE001
        for s in op.steps:
            s["status"] = "unknown" if s["status"] == "running" else "skipped"
        op.status = "NEEDS_VERIFICATION"
        engine.ledger.append("brain_error", operation_id=op.id, error=str(e))
        await engine.bus.emit("operation", op=op.public())
        await engine.bus.emit("say", text="I could not confirm what the robot did. Please check it.")
        return
    by_i = res.get("steps") or []
    for i, s in enumerate(op.steps):
        r = by_i[i] if i < len(by_i) else None
        if r:
            s["status"] = STEP_STATUS.get(str(r.get("status")).lower(), "unknown")
            s["result"] = {"status": s["status"], "detail": r.get("detail"), "by": "engram-api"}
        else:
            s["status"] = "skipped"
    outcome = str(res.get("outcome", "unknown")).lower()
    if outcome == "aborted" and res.get("stopped") is True:  # stopped (a stop, or Carter's rule): it did not finish
        op.status = "STOPPED"
    else:
        op.status = {"success": "VERIFIED", "failed": "FAILED", "failure": "FAILED", "stopped": "STOPPED",
                     "refused": "FAILED"}.get(outcome, "NEEDS_VERIFICATION")
    if res.get("failure"):
        op.trace.append(f"brain failure: {res.get('failure')}")
    op.metrics["exec_ms"] = int((time.time() - t0) * 1000)
    op.metrics["total_ms"] = op.metrics.get("plan_ms", 0) + op.metrics["exec_ms"]
    op.decided_by = WHO
    engine.ledger.append("outcome", operation_id=op.id, outcome=op.status, brain=res)
    await engine.bus.emit("operation", op=op.public())
    if op.status == "VERIFIED":
        run_rec = {"operation_id": op.id, "goal": op.plan.goal, "planner": op.plan.planner, "skill_id": None,
                   "signature": "|".join(s["op"] for s in op.steps), **op.metrics, "verified_by": "engram-api",
                   "all_steps_succeeded": all(s["status"] == "succeeded" for s in op.steps), "ts": time.time()}
        engine.runs.append(run_rec)
        await engine.bus.emit("run", run=run_rec, runs=engine.runs[-10:])
    elif op.status == "NEEDS_VERIFICATION":
        await engine.bus.emit("say", text="I can't confirm that. Please verify on screen.")
