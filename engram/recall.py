"""Memory OS recall: answer a question from the right kind of memory.

Each kind of memory has its own lifecycle, so each is searched with its own
rule for what still counts:

- spatial      locations decay; a stale one is "last observed", never a fact
- semantic     only current (not superseded) facts, then cited document chunks
- episodic     immutable history, newest first, outcome exactly as recorded
- procedural   only APPROVED skills count as "I know how"
- prospective  waiting tasks, and fired tasks not yet done; done/cancelled drop out
- negative     constraints learned from failure
- heard        what people said or typed, immutable, with what it became

Whatever cannot be answered is listed under "missing" (meta-memory) instead of
being guessed. This only reads memory; it never plans or moves anything.
"""

from __future__ import annotations

import re
from typing import Any

from .heard import heard
from .memory import Memory, ObjectState

# Question shape -> memory kind. Every match is searched; no match -> semantic.
ROUTES = [
    ("heard", r"\bwhat did (i|we|you|he|she|they|\w+) (say|tell|ask)\b|\b(said|told|heard)\b"),
    ("spatial", r"\bwhere\b|\blast seen\b|\blocation\b"),
    ("episodic", r"\bwhat happened\b|\bdid (you|it|we)\b|\blast time\b|\bwhy did\b|\bhistory\b"),
    ("procedural", r"\bhow (do|to|can|would)\b|\bskills?\b|\bsteps\b"),
    ("prospective", r"\blater\b|\bscheduled\b|\bwaiting\b|\bremind\b|\bpending\b|\bupcoming\b"),
    ("negative", r"\bavoid\b|\bfail(ed|s|ure)?\b|\bcan'?t\b|\bproblems?\b"),
]


KINDS = [kind for kind, _ in ROUTES] + ["semantic"]

# Words that say what kind of question it is, not what it is about.
_STOP = set("""what which where when why how who whom whose does did done have has had said told tell
heard happened happen about with from that this there then them they their your yours mine should would
could will shall remember history last time later scheduled waiting pending upcoming remind avoid failed
fail fails failure problem problems skill skills steps know thing things before after again ever
please robot""".split())


def route(question: str) -> list[str]:
    q = question.lower()
    return [kind for kind, pattern in ROUTES if re.search(pattern, q)] or ["semantic"]


# "Could you fetch the beaker?" is a request, not a memory question.
_POLITE_REQUEST = re.compile(r"^\s*(can|could|would|will)\s+you\b|^\s*please\b", re.IGNORECASE)


def is_memory_question(text: str) -> bool:
    """A question that asks what memory holds (where / what happened / what was said / what is
    pending...). Plain questions with no memory shape, and polite requests, are left to the planner."""
    t = text.strip()
    return t.endswith("?") and not _POLITE_REQUEST.match(t) and route(t) != ["semantic"]


# A question opener. "When" is left out (it begins real future instructions: "When the centrifuge
# stops, carry the beaker"), and so are "do"/"have" (they begin commands: "Do not ...", "Have it ...").
_ASKING_START = re.compile(r"^\s*(what|where|which|who|whom|whose|why|how|is|are|am|was|were|did|does|"
                           r"can|could|should|would|will|shall|may|might|must)\b", re.IGNORECASE)


def asks_question(text: str) -> bool:
    """Any question at all, except a polite request ("Could you fetch the beaker?"). A question is
    answered from memory and never becomes a plan: on 10-25 "Can the 500 mL beaker go in the drying
    oven?" was planned as fetching the beaker. Whisper ends spoken questions with "?"; the opener
    catches a typed question without one."""
    t = text.strip()
    return (t.endswith("?") or bool(_ASKING_START.match(t))) and not _POLITE_REQUEST.match(t)


def topic_terms(question: str, objs: list[ObjectState]) -> list[str] | None:
    """What the question is about: known object names, else its content words.
    None means a general question ("what happened?") that matches everything."""
    if objs:
        return [o.name.lower() for o in objs]
    words = [w for w in re.findall(r"[a-z][a-z_]+", question.lower()) if len(w) > 3 and w not in _STOP]
    return words or None


def about(text: str, terms: list[str] | None) -> bool:
    return terms is None or any(t in text.lower() for t in terms)


class Recall:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.conn = memory.conn

    def recall(self, question: str, at: float | None = None, kinds: list[str] | None = None) -> dict[str, Any]:
        """kinds overrides the routing, e.g. a planner that needs facts, constraints and
        pending tasks together asks for ["semantic", "negative", "prospective"]."""
        kinds = list(kinds) if kinds else route(question)
        unknown = [k for k in kinds if k not in KINDS]
        if unknown:
            raise ValueError(f"unknown memory kind(s): {unknown}")
        objs = [self.memory.object_state(o.object_id, at) for o in self.memory.resolve_object(question)]
        answers: list[dict[str, Any]] = []
        for kind in kinds:
            answers += [{"kind": kind, **a} for a in getattr(self, kind)(question, objs)]
        missing = [f"current location of {o.name}" for o in objs if o.stale]
        if not answers:
            missing.append(f"nothing in {'/'.join(kinds)} memory about this")
        reply = answers[0]["text"] if answers else "I have no record of that."
        if missing and answers:
            reply += " Unknown: " + "; ".join(missing) + "."
        return {"question": question, "kinds": kinds, "answers": answers, "missing": missing, "reply": reply}

    def spatial(self, question: str, objs: list[ObjectState]) -> list[dict[str, Any]]:
        return [{"text": o.describe(), "object_id": o.object_id, "freshness": o.freshness} for o in objs]

    def semantic(self, question: str, objs: list[ObjectState]) -> list[dict[str, Any]]:
        ids = {o.object_id for o in objs}
        facts = [{"text": f"{f['subject']} {f['predicate']} {f['object']}", "fact_id": f["id"],
                  "source_id": f["source_id"]}
                 for f in self.memory.current_facts() if f["subject"] in ids]
        docs = [{"text": h.text, "source_id": h.source_id, "chunk_id": h.chunk_id, "span": [h.start_char, h.end_char]}
                for h in self.memory.search(question, limit=3)]
        return facts + docs

    def episodic(self, question: str, objs: list[ObjectState]) -> list[dict[str, Any]]:
        terms = topic_terms(question, objs)
        rows = self.conn.execute("SELECT id, request, outcome, failure, started_at FROM episode ORDER BY started_at DESC")
        return [{"text": f"{r['request']} -> {r['outcome']}" + (f" ({r['failure']})" if r["failure"] else ""),
                 "episode_id": r["id"], "at": r["started_at"]}
                for r in rows if about(r["request"], terms)][:5]

    def procedural(self, question: str, objs: list[ObjectState]) -> list[dict[str, Any]]:
        # Skill names use object ids ("fetch sample_b to bench"), so match on ids, not display names.
        terms = [o.object_id for o in objs] or topic_terms(question, [])
        rows = self.conn.execute(
            "SELECT id, name, version FROM skill_version WHERE status = 'APPROVED' ORDER BY approved_at DESC")
        return [{"text": f"skill '{r['name']}' v{r['version']}", "skill_id": r["id"]}
                for r in rows if about(r["name"], terms)]

    def prospective(self, question: str, objs: list[ObjectState]) -> list[dict[str, Any]]:
        # Fired is not done: a fired task stays visible until it is completed or cancelled.
        terms = topic_terms(question, objs)
        rows = self.conn.execute(
            "SELECT id, trigger, goal, request, status FROM prospective_task "
            "WHERE status IN ('waiting', 'fired') ORDER BY created_at")
        return [{"text": f"when {r['trigger']}: {r['goal']}" + (" (fired, not done yet)" if r["status"] == "fired" else ""),
                 "task_id": r["id"], "status": r["status"]}
                for r in rows if about(r["request"], terms)]

    def heard(self, question: str, objs: list[ObjectState]) -> list[dict[str, Any]]:
        terms = topic_terms(question, objs)
        return [{"text": f"{h['who']} said \"{h['text']}\" -> {h['became']}", "observation_id": h["observation_id"],
                 "at": h["at"]}
                for h in heard(self.conn, limit=None) if about(h["text"], terms)][:10]

    def negative(self, question: str, objs: list[ObjectState]) -> list[dict[str, Any]]:
        return [{"text": c} for c in self.memory.constraints_for([o.object_id for o in objs])]
