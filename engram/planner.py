"""Request to typed JSON plan (Carther ticket EC07).

The rule planner is the default and needs no model, so the demo never depends
on a model loading. A local model behind llama.cpp's HTTP server can be used
instead; whatever it returns still goes through the checker, and on any error
we fall back to rules.

The planner only proposes. It never talks to the robot.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .capabilities import describe_all
from .memory import Memory, MemoryBundle

REFUSE_WORDS = (
    "delete", "erase", "wipe", "forget everything", "disable stop", "turn off stop",
    "throw", "pour", "hit", "break", "override",
    # Disposal: the robot has no disposal capability, so these must refuse instead of planning a move (EC15 t08.2/t08.3).
    "dump", "get rid of", "dispose", "discard", "trash", "garbage", "bin it",
)
# Future intent that did not parse as "When/Once/After/As soon as X, do Y" must never become an immediate move
# (EC15 t03.x): it is refused with a hint to phrase it as a future task, which the engine schedules instead.
FUTURE_HINTS = re.compile(
    r"\b(when|whenever|once|after|as soon as|until|later|tomorrow|tonight|in \d+ (?:seconds?|minutes?|hours?))\b"
)
# "Do not move Sample B" is an instruction NOT to act. Without this it planned a move of Sample B.
NEGATION = re.compile(r"\b(do not|don'?t|never|no need to)\b")
DEFAULT_ZONE = "bench"
log = logging.getLogger("engram.planner")


@dataclass
class PlanResult:
    plan: dict[str, Any] | None
    # How the planner took the request, stated by every result: "understood" (planned, or refused as something the
    # robot may not do), "unclear" (what was meant is not clear, so the person must say it another way) or
    # "misheard". No default: a result that does not say cannot be made, and the engine fails closed on anything
    # else (unclear), for a plan as for a refusal.
    classification: str
    refused: str | None = None
    planner_calls: int = 0
    planning_ms: float = 0.0
    source: str = "rules"          # rules | model | fallback (model failed, rules used) | skill
    note: str = ""                 # why the model was not used, or the start of its raw reply
    targets: list[str] = field(default_factory=list)
    zone: str | None = None


def classify_refusal(request: str, future_ok: bool = False) -> tuple[str, str] | None:
    """(why this request must not be planned, how it was taken), or None. Each refusal states its classification.

    "unclear": what was meant is not clear, so the person is asked to say it another way.
    "understood": it was understood and is refused, and saying it again would not help: an action
    the robot is not allowed to do, or "don't".

    future_ok=True is for a request that is being saved as a future task ("... after
    calibration finishes"): its deferral wording is expected, so only that check is skipped.
    Refused actions and negations still refuse.
    """
    low = request.lower()
    for w in REFUSE_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low):
            return f"I will not do that: '{w}' is not something this robot is allowed to do.", "understood"
    neg = NEGATION.search(low.replace("\u2019", "'"))
    if neg:
        return (f"You said '{neg.group(1)}', so I will not move anything. "
                "To make it a rule, say 'Remember that ...'."), "understood"
    hint = None if future_ok else FUTURE_HINTS.search(low)
    if hint:
        return (f"That sounds like something to do later ('{hint.group(1)}'), so I will not move now. "
                "Say it as 'When <event>, <task>' and I will schedule it."), "unclear"
    return None


def refusal(request: str, future_ok: bool = False) -> str | None:
    """Why this request must not be planned, or None (see classify_refusal)."""
    r = classify_refusal(request, future_ok)
    return r[0] if r else None


def find_zone(request: str, zones: set[str]) -> str:
    low = request.lower()
    for z in sorted(zones, key=len, reverse=True):
        if z.replace("_", " ").lower() in low:
            return z
    return DEFAULT_ZONE


def targets_in(request: str, memory: Memory) -> list[str]:
    return [o.object_id for o in memory.resolve_object(request)]


class RulePlanner:
    name = "rules"

    def __init__(self, zones: set[str]):
        self.zones = zones

    def plan(self, request: str, bundle: MemoryBundle, memory: Memory) -> PlanResult:
        t0 = time.perf_counter()
        cls = classify_refusal(request)
        if cls:
            return PlanResult(None, classification=cls[1], refused=cls[0])
        targets = targets_in(request, memory)
        if not targets:
            return PlanResult(None, classification="unclear", refused="I don't know which object you mean.",
                              planner_calls=1, planning_ms=(time.perf_counter() - t0) * 1000)
        zone = find_zone(request, self.zones)
        rules = [f for f in memory.current_facts() if f["predicate"] == "requires_before"]

        order: list[str] = []

        def add(obj: str, seen: tuple[str, ...] = ()) -> None:
            if obj in order or obj in seen:
                return
            for r in rules:
                if r["subject"] == obj:
                    add(r["object"], seen + (obj,))
            order.append(obj)

        for t in targets:
            add(t)

        steps: list[dict[str, Any]] = []
        for obj in order:
            steps.append({"op": "locate_tag", "args": {"object_id": obj}})
            steps.append({"op": "pick_tagged_object", "args": {"object_id": obj}})
            steps.append({"op": "place_at_zone", "args": {"zone": zone}})
        names = [memory.object_state(o).name for o in order]
        steps.append({"op": "speak", "args": {"text": f"Done. {', '.join(names)} at {zone.replace('_', ' ')}."}})
        plan = {
            "goal": bundle.goal,
            "steps": steps,
            "evidence": [e["chunk_id"] for e in bundle.evidence],
            "rules_used": [r["id"] for r in rules if r["subject"] in order],
        }
        return PlanResult(plan, classification="understood", planner_calls=1,
                          planning_ms=(time.perf_counter() - t0) * 1000, source="rules", targets=targets, zone=zone)


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {"type": "array", "maxItems": 12, "items": {
            "type": "object",
            "properties": {"op": {"type": "string"}, "args": {"type": "object"}},
            "required": ["op", "args"]}},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["goal", "steps", "evidence"],
}


def extract_json(text: str) -> dict | None:
    """The JSON object in a model reply. Qwen3 may think first; drop <think> blocks before parsing."""
    text = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else None


def unresolved(plan: dict[str, Any], memory: Memory) -> str:
    """Why a model's plan does not resolve to what memory knows, or "": it has no steps, or it names an object that
    memory does not have. The model did not work out what was meant, so the rule planner takes the request instead:
    it plans it, or refuses it with its own classification ("I don't know which object you mean." is unclear).
    Handed to the checker as it was, the plan would be turned down as understood, and a running plan would go on."""
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return "model plan has no steps"
    known = {o.object_id for o in memory.objects()}
    for step in steps:
        args = step.get("args") if isinstance(step, dict) else None
        obj = args.get("object_id") if isinstance(args, dict) else None
        if obj is not None and (not isinstance(obj, str) or obj not in known):
            return f"model plan names an object memory does not have: {obj!r}"
    return ""


class LlamaCppPlanner:
    """Ask a local llama.cpp server for the plan. Falls back to rules on any problem, a plan that does not resolve
    to what memory knows included (see unresolved)."""

    name = "model"

    def __init__(self, zones: set[str], url: str = "http://127.0.0.1:8080/completion", timeout_s: float = 20.0):
        self.zones = zones
        self.url = url
        self.timeout_s = timeout_s
        self.fallback = RulePlanner(zones)

    def _prompt(self, request: str, bundle: MemoryBundle) -> str:
        return (
            "You plan for a small lab robot. Reply with JSON only: "
            '{"goal": str, "steps": [{"op": str, "args": {}}], "evidence": [chunk ids]}.\n'
            f"Allowed actions: {json.dumps(describe_all())}\n"
            f"Zones: {sorted(self.zones)}\n"
            f"Memory: {json.dumps(bundle.to_dict())}\n"
            f"Request: {request} /no_think\nJSON:"
        )

    def plan(self, request: str, bundle: MemoryBundle, memory: Memory) -> PlanResult:
        cls = classify_refusal(request)
        if cls:
            return PlanResult(None, classification=cls[1], refused=cls[0])
        t0 = time.perf_counter()
        # json_schema makes llama.cpp stop as soon as the plan object closes; without it the model
        # kept writing prose until n_predict (measured on the Pi 5: 512 tokens, 100 s wasted).
        body = json.dumps({"prompt": self._prompt(request, bundle), "n_predict": 384, "temperature": 0,
                           "json_schema": PLAN_SCHEMA}).encode()
        text, why = "", ""
        try:
            req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                text = json.loads(r.read())["content"]
            log.info("model reply starts: %r", text[:200])
            plan = extract_json(text)
            if not isinstance(plan, dict):
                why = "model reply had no JSON plan"
            else:
                why = unresolved(plan, memory)
                if why:  # the model did not work out what was meant: the rules take the request
                    plan = None
        except Exception as e:
            plan, why = None, f"model call failed: {type(e).__name__}: {e}"
        ms = (time.perf_counter() - t0) * 1000
        if not isinstance(plan, dict):
            res = self.fallback.plan(request, bundle, memory)
            res.planner_calls += 1
            res.planning_ms += ms
            if res.plan is not None:
                res.source = "fallback"
            res.note = f"{why}; reply starts: {text[:200]!r}" if text else why
            return res
        return PlanResult(plan, classification="understood", planner_calls=1, planning_ms=ms, source="model",
                          note=f"reply starts: {text[:200]!r}", targets=targets_in(request, memory),
                          zone=find_zone(request, self.zones))
