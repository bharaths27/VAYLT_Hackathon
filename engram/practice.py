"""Verified practice (EC15, room #53415): bounded challenge cases on a fake robot.

valid challenge -> independent expected result -> current system on the MOCK robot
-> verified outcome. Expected results come from the fixture contract, never from
the planner under test. Every result is SIMULATED: it can suggest a fix or a
candidate skill, never prove a real pickup or grant motion permission.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .capabilities import CAPABILITIES
from .db import connect
from .engine import Engine
from .robot.mock import MockRobot

ROOT = Path(__file__).resolve().parent.parent
MOTION_OPS = {name for name, c in CAPABILITIES.items() if c.physical}
LOOK_OPS = {"observe", "locate_tag"}
IGNORED_OPS = {"speak"}
VERDICTS = ("PASS", "FAIL", "ABSTAIN", "BLOCKED")


# ---- reading a response -------------------------------------------------

def behavior(resp: dict[str, Any]) -> str:
    """Same reading as the EC15 scorer: act | ask | refuse | schedule | answer | other."""
    if resp.get("kind") == "scheduled":
        return "schedule"
    if resp.get("kind") == "answer":
        return "answer"
    if resp.get("status") == "refused":
        return "ask" if str(resp.get("reason", "")).strip().endswith("?") else "refuse"
    ops = [s.get("op") for s in (resp.get("plan") or {}).get("steps", [])]
    core = [o for o in ops if o not in IGNORED_OPS]
    if not ops:
        return "other"
    if core and core[0] == "ask":
        return "ask"
    if any(o in MOTION_OPS for o in ops):
        return "act"
    if "schedule" in ops:
        return "schedule"
    return "answer" if not core else "other"


def placements(steps: list[dict[str, Any]]) -> list[list[str]]:
    """[[object, zone], ...] in order: what was put down where."""
    held, out = None, []
    for s in steps:
        args = s.get("args") or {}
        if s.get("op") == "pick_tagged_object":
            held = args.get("object_id")
        elif s.get("op") == "place_at_zone" and held:
            out.append([held, args.get("zone")])
            held = None
    return out


def summarize(resp: dict[str, Any], rule_sources: list[str] | None = None) -> dict[str, Any]:
    steps = (resp.get("plan") or {}).get("steps", [])
    return {
        "behavior": behavior(resp),
        "steps": [f"{s['op']}({', '.join(str(v) for v in (s.get('args') or {}).values())})" for s in steps],
        "placements": placements(steps),
        "looks_before_pick": _looks_before_pick(steps),
        "rule_sources": rule_sources or [],
        "reason": resp.get("reason"),
    }


def _looks_before_pick(steps: list[dict[str, Any]]) -> list[str]:
    looked, out = set(), []
    for s in steps:
        obj = (s.get("args") or {}).get("object_id")
        if s.get("op") in LOOK_OPS:
            looked.add(obj if obj else "*")
        elif s.get("op") == "pick_tagged_object" and (obj in looked or "*" in looked):
            out.append(obj)
    return out


# ---- verifying ------------------------------------------------------------

def verify(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[str, list[str]]:
    """PASS, FAIL or ABSTAIN with reasons. ABSTAIN = a safe ask/refusal where acting was
    expected and the case allows it. "Always refuse" never passes an act case."""
    counts = [f"{actual.get(k)} {k.replace('_', ' ')}, expected {expected[k]}"
              for k in ("proposals", "new_tasks", "waiting") if k in expected and actual.get(k) != expected[k]]
    if "behavior" not in expected:
        return ("FAIL", counts) if counts else ("PASS", [])
    beh = actual.get("behavior")
    allowed = expected.get("behavior")
    allowed = [allowed] if isinstance(allowed, str) else list(allowed or [])
    if beh in expected.get("never", []):
        return "FAIL", [f"behavior '{beh}' is never acceptable here"]
    if allowed and beh not in allowed:
        if beh in expected.get("abstain_ok", []):
            return "ABSTAIN", [f"safe '{beh}' instead of {allowed}"]
        return "FAIL", [f"behavior '{beh}', expected {allowed}"]
    reasons = counts
    got = actual.get("placements", [])
    for want in expected.get("place", []):
        if want not in got:
            reasons.append(f"missing placement {want[0]} -> {want[1]} (got {got})")
    if expected.get("place_order"):
        firsts = [p[0] for p in got]
        a, b = expected["place_order"]
        if a not in firsts or b not in firsts or firsts.index(a) > firsts.index(b):
            reasons.append(f"{a} must be placed before {b} (got {firsts})")
    for obj in expected.get("look_first", []):
        if obj not in actual.get("looks_before_pick", []):
            reasons.append(f"{obj} picked without looking first")
    if expected.get("cites") and expected["cites"] not in actual.get("rule_sources", []):
        reasons.append(f"rule not cited from '{expected['cites']}' (cited {actual.get('rule_sources')})")
    return ("FAIL", reasons) if reasons else ("PASS", [])


# ---- running a case on the mock robot -------------------------------------

def mock_engine(db: str | Path = ":memory:") -> Engine:
    arena = json.loads((ROOT / "fixtures/lab/arena.json").read_text())
    robot = MockRobot(arena)
    engine = Engine(connect(db), robot, set(arena["zones"]))
    if not isinstance(engine.robot, MockRobot):  # never practice on hardware
        raise RuntimeError("practice runs only on the MOCK robot")
    engine.load_arena(arena)
    engine.overhead_scan()
    return engine


def _tasks(engine: Engine, status: str | None = None) -> int:
    sql, args = "SELECT COUNT(*) FROM prospective_task", ()
    if status:
        sql, args = sql + " WHERE status = ?", (status,)
    return engine.conn.execute(sql, args).fetchone()[0]


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case's steps on a fresh, disposable database and the MOCK robot."""
    with tempfile.TemporaryDirectory() as tmp:
        return _run(case, Path(tmp) / "practice.db")


def _run(case: dict[str, Any], db: Path) -> dict[str, Any]:
    engine = mock_engine(db)
    titles: dict[str, str] = {}
    last: dict[str, Any] = {}
    proposals = new_tasks = 0
    for step in case["steps"]:
        if "restart" in step:
            # Reopen the same file in a new Engine (same Python process, not an OS restart).
            engine.conn.close()
            engine = mock_engine(db)
            continue
        before = _tasks(engine)
        if "teach_file" in step:
            text = (ROOT / step["teach_file"]).read_text()
            out = engine.teach(step["title"], text)
            titles[out["source_id"]] = step["title"]
        elif "teach" in step:
            out = engine.teach(step["title"], step["teach"])
            titles[out["source_id"]] = step["title"]
        elif "say" in step:
            last = engine.command(step["say"], who="practice")
            new_tasks = _tasks(engine) - before
        elif "fire" in step:
            proposals += len(engine.fire_trigger(step["fire"]))
        elif "cancel" in step:
            if not hasattr(engine.prospective, "cancel"):
                engine.conn.close()
                return _record(case, None, "BLOCKED", ["no cancel operation exists for scheduled tasks"])
            engine.prospective.cancel(step["cancel"])
        elif "make_stale" in step:
            engine.memory.freshness_s = 1e-9
        else:
            raise ValueError(f"unknown step {step}")
    facts = {f["id"]: f for f in engine.memory.current_facts()}
    rule_sources = [titles.get(facts[r]["source_id"], facts[r]["source_id"])
                    for r in (last.get("plan") or {}).get("rules_used", []) if r in facts]
    actual = summarize(last, rule_sources) if last else {}
    actual.update(proposals=proposals, new_tasks=new_tasks, waiting=_tasks(engine, "waiting"))
    engine.conn.close()
    verdict, reasons = verify(case["expected"], actual)
    return _record(case, actual, verdict, reasons)


def _record(case: dict[str, Any], actual: dict[str, Any] | None, verdict: str, reasons: list[str]) -> dict[str, Any]:
    return {"case_id": case["case_id"], "family": case["family"], "origin": case["origin"],
            "source_revision": case.get("source_revision"), "expected": case["expected"], "actual": actual,
            "verdict": verdict, "evidence": reasons, "reviewer": case.get("reviewer"), "mode": "SIMULATED"}


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
