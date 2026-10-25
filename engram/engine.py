"""Ties memory, planner, checker, approvals, executor and skills together.

The API and the demo script both talk only to this class.

Flow for one request:
    request -> memory bundle -> approved skill or planner -> checker
            -> proposal with digest -> person approves that digest -> executor
"""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .checker import check_plan
from .db import now
from .executor import Executor
from .hashes import hash_models
from .ids import digest, new_id
from .memory import Memory, MemoryBundle
from .planner import PlanResult, RulePlanner, find_zone, refusal, targets_in
from .prospective import Prospective, normalize, parse_when
from .robot.base import RobotAdapter
from .skills import Skills
from .teach import extract_rules

APPROVAL_TTL_S = 120.0


class EngineError(Exception):
    pass


class Engine:
    def __init__(self, conn: sqlite3.Connection, robot: RobotAdapter, zones: set[str],
                 planner: Any = None, models_dir: str | Path | None = None,
                 freshness_s: float | None = None):
        self.conn = conn
        self.robot = robot
        self.zones = set(zones)
        self.memory = Memory(conn) if freshness_s is None else Memory(conn, freshness_s)
        self.planner = planner or RulePlanner(self.zones)
        self.skills = Skills(conn)
        self.prospective = Prospective(conn)
        self.executor = Executor(conn, robot, self.memory)
        self.models_dir = models_dir
        self.model_hashes_at_start = hash_models(models_dir)
        # Canonical request text (normalized) -> fixed steps. See register_fixed_request().
        self.fixed_requests: dict[str, list[dict[str, Any]]] = {}

    # ---- setup -----------------------------------------------------------

    def set_robot(self, robot: RobotAdapter) -> None:
        self.robot = robot
        self.executor.robot = robot

    def register_fixed_request(self, request: str, steps: list[dict[str, Any]]) -> None:
        """Plan this exact request (case and punctuation aside) as these steps, without a planner.

        For short canonical requests the rule planner cannot plan because they name no object,
        such as a voice preset's "Open the gripper". Only planning is skipped: propose() still
        refuses negated or future wording, the checker must accept the steps, and a person must
        approve the plan's digest before anything runs. Future tasks that fire use the same path.
        """
        self.fixed_requests[normalize(request)] = copy.deepcopy(steps)

    def load_arena(self, arena: dict[str, Any]) -> None:
        """Bind tagged objects from the arena file. Locations come only from observations."""
        for o in arena["objects"]:
            self.memory.bind_object(o["id"], o["name"], o["tag"], o.get("graspable", True), o.get("width_mm"))

    def overhead_scan(self) -> int:
        """What the overhead camera would report. With the mock robot we ask it for visible tags."""
        out = self.robot.execute("observe", {})
        for tag_id, zone in out.observations:
            self.memory.observe_tag(tag_id, zone)
        return len(out.observations)

    def teach(self, title: str, text: str, kind: str = "document", origin: str | None = None) -> dict[str, Any]:
        source_id = self.memory.add_source(title, text, kind, origin)
        rules = extract_rules(self.memory, source_id)
        facts = [f for f in self.memory.current_facts() if f["id"] in rules]
        return {"source_id": source_id, "rules": facts}

    # ---- commands --------------------------------------------------------

    def command(self, text: str, who: str = "user") -> dict[str, Any]:
        """One entry point for typed or spoken commands."""
        stripped = text.strip()
        low = stripped.lower()
        if low.startswith("remember"):
            body = stripped.split(" ", 1)[1] if " " in stripped else ""
            body = body.lstrip(":, ").strip()
            if body.lower().startswith("that "):
                body = body[5:]
            return {"kind": "taught", **self.teach(f"Spoken note from {who}", body, kind="spoken", origin=who)}
        when = parse_when(stripped)
        if when:
            trigger, request = when
            src = self.memory.add_source(f"Future task from {who}", stripped, kind="spoken", origin=who)
            task_id, created = self.prospective.add(trigger, request, src)
            return {"kind": "scheduled", "task_id": task_id, "created": created, "trigger": trigger, "request": request}
        return {"kind": "proposal", **self.propose(stripped)}

    def propose(self, request: str, bundle: MemoryBundle | None = None) -> dict[str, Any]:
        """bundle: the memory bundle for this request, if the caller has already retrieved it and memory has not
        changed since (voice presets prefetch it while the user is still speaking). Without one, or with one
        retrieved for another request, it is retrieved here."""
        episode_id = new_id("ep")
        why = refusal(request)
        if why:
            self._episode(episode_id, request, None, {}, "refused", why, PlanResult(None))
            self.executor.journal(new_id("op"), episode_id, "propose", {"request": request}, "rejected", why)
            return {"operation_id": episode_id, "status": "refused", "reason": why}

        if bundle is None or bundle.goal != request:
            bundle = self.memory.retrieve_memory_bundle(request, self.skills.approved_names())
        targets = targets_in(request, self.memory)
        zone = find_zone(request, self.zones)
        fixed = self.fixed_requests.get(normalize(request))
        skill = self.skills.find(targets, zone) if targets and fixed is None else None
        if fixed is not None:
            plan = {"goal": request, "steps": copy.deepcopy(fixed), "evidence": [], "rules_used": []}
            res = PlanResult(plan, planner_calls=0, source="fixed", targets=targets, zone=None)
        elif skill:
            t0 = time.perf_counter()
            plan = {"goal": request, "steps": skill["definition"]["steps"],
                    "evidence": skill["definition"]["evidence"], "rules_used": skill["definition"]["rules_used"],
                    "skill_id": skill["id"]}
            res = PlanResult(plan, planner_calls=0, planning_ms=(time.perf_counter() - t0) * 1000,
                             source="skill", targets=targets, zone=zone)
        else:
            res = self.planner.plan(request, bundle, self.memory)

        if res.plan is None:
            self._episode(episode_id, request, None, {"bundle": bundle.to_dict()}, "refused", res.refused, res)
            return {"operation_id": episode_id, "status": "refused", "reason": res.refused,
                    "memory": bundle.to_dict(), "planner_calls": res.planner_calls}

        check = check_plan(res.plan, self.memory, self.zones)
        plan_digest = digest(res.plan)
        meta = {"targets": res.targets, "zone": res.zone, "source": res.source, "planner_note": res.note,
                "digest": plan_digest, "check": check.to_dict(), "bundle": bundle.to_dict()}
        status = "proposed" if check.ok else "rejected"
        self._episode(episode_id, request, res.plan, meta, status, None if check.ok else "; ".join(check.errors), res,
                      skill_id=skill["id"] if skill else None)
        return {
            "operation_id": episode_id, "status": status, "plan": res.plan, "digest": plan_digest,
            "check": check.to_dict(), "memory": bundle.to_dict(), "source": res.source,
            "planner_note": res.note, "planner_calls": res.planner_calls, "planning_ms": round(res.planning_ms, 3),
        }

    def propose_fixed(self, goal: str, steps: list[dict[str, Any]], source: str) -> dict[str, Any]:
        """Propose a ready-made plan (for example "return to rest" from a gesture).

        No planner is involved, but everything else is the same as propose(): the checker
        must accept it, it gets a digest, and a person must approve that digest before it runs.
        """
        episode_id = new_id("ep")
        plan = {"goal": goal, "steps": steps, "evidence": [], "rules_used": []}
        res = PlanResult(plan, planner_calls=0, source=source)
        check = check_plan(plan, self.memory, self.zones)
        plan_digest = digest(plan)
        status = "proposed" if check.ok else "rejected"
        meta = {"targets": [], "zone": None, "source": source, "digest": plan_digest, "check": check.to_dict()}
        self._episode(episode_id, goal, plan, meta, status, None if check.ok else "; ".join(check.errors), res)
        return {"operation_id": episode_id, "status": status, "plan": plan, "digest": plan_digest,
                "check": check.to_dict(), "source": source, "planner_calls": 0, "planning_ms": 0.0}

    def propose_rest(self, zone: str = "home") -> dict[str, Any]:
        """Return to the resting position: drive to the start zone. Needs approval like any plan.

        When the robot adapter gets an arm rest pose, add it to capabilities.py and to these steps.
        """
        return self.propose_fixed(f"return to rest at {zone}", [{"op": "go_to_zone", "args": {"zone": zone}}],
                                  source="gesture")

    def _episode(self, episode_id: str, request: str, plan: dict[str, Any] | None, meta: dict[str, Any],
                 outcome: str, failure: str | None, res: PlanResult, skill_id: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO episode(id, goal, request, plan_json, evidence_json, outcome, failure, planner_calls,
                                   planning_ms, started_at, skill_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (episode_id, request, request, json.dumps(plan) if plan else None, json.dumps(meta), outcome, failure,
             res.planner_calls, res.planning_ms, now(), skill_id),
        )
        self.conn.commit()

    def approve_and_run(self, operation_id: str, approved_digest: str, approved_by: str) -> dict[str, Any]:
        ep = self.conn.execute("SELECT * FROM episode WHERE id = ?", (operation_id,)).fetchone()
        if not ep:
            raise EngineError(f"no operation {operation_id}")
        if ep["outcome"] != "proposed":
            raise EngineError(f"operation is {ep['outcome']}, not waiting for approval")
        if not approved_by:
            raise EngineError("a person must approve")
        plan = json.loads(ep["plan_json"])
        if approved_digest != digest(plan):
            raise EngineError("digest does not match the proposed plan")
        t = now()
        self.conn.execute(
            "INSERT INTO approval(id, target_kind, target_id, digest, approved_by, approved_at, expires_at, used_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (new_id("apv"), "plan", operation_id, approved_digest, approved_by, t, t + APPROVAL_TTL_S, t),
        )
        # The world may have changed since the proposal. Check again right before moving.
        check = check_plan(plan, self.memory, self.zones)
        if not check.ok:
            self._finish(operation_id, "rejected", "; ".join(check.errors))
            return {"operation_id": operation_id, "outcome": "rejected", "check": check.to_dict()}
        self.conn.execute("UPDATE episode SET outcome = 'running' WHERE id = ?", (operation_id,))
        self.conn.commit()
        result = self.executor.run(operation_id, plan)
        self._finish(operation_id, result.outcome, result.failure)
        return {"operation_id": operation_id, "outcome": result.outcome, "failure": result.failure,
                "steps": result.steps}

    def _finish(self, episode_id: str, outcome: str, failure: str | None) -> None:
        self.conn.execute("UPDATE episode SET outcome = ?, failure = ?, finished_at = ? WHERE id = ?",
                          (outcome, failure, now(), episode_id))
        self.conn.commit()

    # ---- other actions ---------------------------------------------------

    def stop(self) -> dict[str, Any]:
        self.robot.stop()
        self.executor.journal(new_id("op"), None, "stop", {}, "done", "stop pressed")
        return self.robot.state()

    def fire_trigger(self, trigger: str) -> list[dict[str, Any]]:
        out = []
        for task in self.prospective.fire(trigger):
            proposal = self.propose(task["request"])
            self.prospective.link_proposal(task["id"], proposal["operation_id"])
            out.append({"task": task, "proposal": proposal})
        return out

    def dream(self) -> list[str]:
        return self.skills.compile_all()

    def test_skill(self, skill_id: str) -> dict[str, Any]:
        return self.skills.test(skill_id, lambda p: check_plan(p, self.memory, self.zones)).to_dict()

    def pending(self, max_age_s: float = 300.0) -> list[dict[str, Any]]:
        """Plans waiting for a person's approval, newest first.

        Lets the iPad show plans it did not ask for itself: a gesture's "return to rest",
        or a future task whose trigger fired. Only recent ones, so old unapproved
        proposals (for example from a test run) do not pile up on screen.
        """
        rows = self.conn.execute(
            "SELECT id, request, plan_json, evidence_json, started_at FROM episode "
            "WHERE outcome = 'proposed' AND started_at >= ? ORDER BY started_at DESC",
            (now() - max_age_s,),
        ).fetchall()
        out = []
        for r in rows:
            meta = json.loads(r["evidence_json"] or "{}")
            out.append({"operation_id": r["id"], "request": r["request"], "plan": json.loads(r["plan_json"]),
                        "digest": meta.get("digest"), "source": meta.get("source"), "planner_note": meta.get("planner_note", ""), "proposed_at": r["started_at"]})
        return out

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM operation_event ORDER BY seq DESC LIMIT ?", (limit,))]

    def metrics(self) -> dict[str, Any]:
        runs = [dict(r) for r in self.conn.execute(
            "SELECT id, request, outcome, planner_calls, planning_ms, skill_id FROM episode ORDER BY started_at")]
        hashes_now = hash_models(self.models_dir)
        statuses = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM skill_version GROUP BY status")}
        return {
            "runs": runs,
            "skills": statuses,
            "model_hashes_at_start": self.model_hashes_at_start,
            "model_hashes_now": hashes_now,
            "model_unchanged": hashes_now == self.model_hashes_at_start,
            "journal_rows": self.conn.execute("SELECT COUNT(*) FROM operation_event").fetchone()[0],
        }
