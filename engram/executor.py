"""Run a checked, approved plan one step at a time (Carther ticket EC08).

Every step writes a pending row to the journal before it starts and a result
row after. If the result is "unknown" we stop the robot and never retry,
because repeating a motion we can't confirm could knock something over.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .db import now
from .ids import new_id
from .memory import Memory
from .robot.base import RobotAdapter


@dataclass
class ExecResult:
    outcome: str                    # success | failed | aborted
    failure: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    stopped: bool = False           # aborted because the robot was stopped (a stop, or a command that needed
                                    # clarifying): not an unknown outcome a person must check


class Executor:
    def __init__(self, conn: sqlite3.Connection, robot: RobotAdapter, memory: Memory):
        self.conn = conn
        self.robot = robot
        self.memory = memory
        # Engine.stop() counts here (a stop word, the stop button, a command that needed clarifying), so a step
        # that a stop interrupted is told apart from a step that failed, or whose outcome is unknown, on its own.
        self.stop_requests = 0

    def journal(self, operation_id: str, episode_id: str | None, op: str, args: dict[str, Any],
                status: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO operation_event(operation_id, episode_id, op, args_json, status, detail, at) VALUES (?,?,?,?,?,?,?)",
            (operation_id, episode_id, op, json.dumps(args, sort_keys=True), status, detail, now()),
        )
        self.conn.commit()

    def run(self, episode_id: str, plan: dict[str, Any]) -> ExecResult:
        result = ExecResult(outcome="success")
        for step in plan["steps"]:
            op, args = step["op"], step.get("args", {})
            op_id = new_id("op")
            stops = self.stop_requests  # before the check: a stop from here on, however it lands, ends the run
            if self.robot.state().get("stopped") and op != "stop":
                self.journal(op_id, episode_id, op, args, "rejected", "robot is stopped")
                result.outcome, result.failure, result.stopped = "aborted", "stop was pressed", True
                break
            self.journal(op_id, episode_id, op, args, "pending")
            out = self.robot.execute(op, args)
            stopped = self.stop_requests != stops  # read at once, whatever the step returned (done included)
            for tag_id, zone in out.observations:
                self.memory.observe_tag(tag_id, zone, source="robot")
            self.journal(op_id, episode_id, op, args, out.status, out.detail)
            result.steps.append({"op": op, "args": args, "status": out.status, "detail": out.detail})
            if stopped:  # stopped before or while it ran: the stop ended the run, not the step
                result.outcome, result.failure, result.stopped = "aborted", "stop was pressed", True
                break
            if out.status == "done":
                continue
            subject = args.get("object_id") or args.get("zone") or op
            if out.status == "unknown":
                self.robot.stop()
                result.outcome = "aborted"
                result.failure = f"{op} outcome unknown: {out.detail}. Robot stopped. Not retried; a person must check."
            else:
                result.outcome = "failed"
                result.failure = f"{op} failed: {out.detail}"
            self.memory.add_constraint(subject, result.failure, [episode_id, op_id])
            break
        return result
