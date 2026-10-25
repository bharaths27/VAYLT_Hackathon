"""Future tasks: "when X happens, do Y" (Carther ticket EC11).

Saying the same thing twice does not make two tasks. When the trigger fires,
the task becomes a proposal that still needs a person's approval.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import now
from .ids import new_id
from .teach import rule_body

# "When X, do Y" and the same shape with once / after / as soon as / whenever (EC15 t03.x).
_WHEN = re.compile(
    r"^\s*(?:when|whenever|once|after|as soon as)\s+(?P<trigger>.+?),\s*(?:then\s+)?(?P<request>.+)$", re.IGNORECASE
)


def normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


# A question about the past or present ("After the experiment finished, what happened to sample A?") is recall,
# never a commitment (EC15 t10.2 / practice check M2): it must not be scheduled.
_QUESTION_START = re.compile(
    r"^\s*(what|where|which|who|whom|whose|why|how|when|did|does|do|was|were|is|are|has|have|had|can|could)\b",
    re.IGNORECASE,
)


def is_question(text: str) -> bool:
    return text.strip().endswith("?")


def _split_when(text: str) -> tuple[str, str] | None:
    """(trigger, request) of "When X, Y" and its variants, before any check of what Y is."""
    m = None if is_question(text) else _WHEN.match(text)
    return (m.group("trigger"), m.group("request").strip()) if m else None


def parse_when(text: str) -> tuple[str, str] | None:
    """(normalized trigger, request) of a future task, or None. Only a robot action can wait for an event: a
    question is recall, not a commitment, and a rule ("When X, remember that Y") is taught now or not at all, so
    neither is scheduled. A rule is recognised by teach.rule_body, the same check that teaches one."""
    split = _split_when(text)
    if split is None:
        return None
    trigger, request = split
    if _QUESTION_START.match(request) or rule_body(request) is not None:
        return None
    return normalize(trigger), request


def deferred_rule(text: str) -> str | None:
    """The request of "When X, <a rule>" (for example "remember that Sample B must stay at the bench"), or None.
    parse_when refuses to schedule it; Engine.command answers it the way the console does."""
    split = _split_when(text)
    return split[1] if split and rule_body(split[1]) is not None else None


class Prospective:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, trigger: str, request: str, source_id: str | None = None) -> tuple[str, bool]:
        """Returns (task_id, created). created is False when the same task is already waiting."""
        trig = normalize(trigger)
        goal = normalize(request)
        row = self.conn.execute(
            "SELECT id FROM prospective_task WHERE trigger = ? AND goal = ? AND status = 'waiting'", (trig, goal)
        ).fetchone()
        if row:
            return row["id"], False
        task_id = new_id("fut")
        self.conn.execute(
            "INSERT INTO prospective_task(id, trigger, goal, request, source_id, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, trig, goal, request, source_id, "waiting", now()),
        )
        self.conn.commit()
        return task_id, True

    def fire(self, trigger: str) -> list[dict[str, Any]]:
        trig = normalize(trigger)
        rows = self.conn.execute(
            "SELECT * FROM prospective_task WHERE trigger = ? AND status = 'waiting' ORDER BY created_at", (trig,)
        ).fetchall()
        for r in rows:
            self.conn.execute("UPDATE prospective_task SET status = 'fired', fired_at = ? WHERE id = ?", (now(), r["id"]))
        self.conn.commit()
        return [dict(r) for r in rows]

    def link_proposal(self, task_id: str, proposal_id: str) -> None:
        self.conn.execute("UPDATE prospective_task SET proposal_id = ? WHERE id = ?", (proposal_id, task_id))
        self.conn.commit()

    def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM prospective_task ORDER BY created_at")]
