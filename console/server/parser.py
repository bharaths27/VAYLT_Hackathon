"""Deterministic intent parser: text + MemoryBundle -> typed Plan or abstention.

Order of preference (V03):
  1. safety screen (unsafe -> abstain, never planned)
  2. APPROVED skill reuse (zero planner calls)
  3. deterministic grammar
  4. optional local LLM, only if enabled and its output passes validate_plan
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

from .plan import Plan, PlanError, Step, validate_plan

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45,
             "fifty": 50, "sixty": 60, "ninety": 90, "a hundred": 100, "one hundred": 100, "hundred eighty": 180,
             "a half": 0.5, "half a": 0.5}

UNSAFE = [
    (r"\b(full|max(imum)?|top)\s+speed\b|\bas fast as\b|\bspeed up\b|\bfaster\b", "speed beyond the bounded primitives is not allowed"),
    (r"\b(throw|toss|fling|ram|smash|crash|hit|attack|chase)\b", "that would be an unsafe physical effect"),
    (r"\boff the (table|edge|bench)\b|\bover the edge\b", "driving off an edge is outside the safety envelope"),
    (r"\b(ignore|disable|bypass|override|turn off)\b.*\b(safety|lidar|stop|limits?|watchdog|approval)\b", "safety systems cannot be disabled by command"),
    (r"\b(run|execute|eval)\b.*\b(code|python|script|shell|command)\b|\bsudo\b|\brm -rf\b", "arbitrary code cannot control the robot"),
    (r"\b(at|toward|towards|into)\s+(a |the )?(person|people|someone|me|him|her|them|judge)\b", "the robot does not drive at people"),
    (r"\b(acid|bleach|knife|blade|flame|fire|hot plate)\b", "hazardous objects are out of scope for this robot"),
]

PRONOUNS = r"\b(it|that|this|them|those|the thing|the stuff|something)\b"

# STOP always wins: a stop word ANYWHERE in the utterance stops the robot, whatever the case, punctuation or
# company ("Stop.", "STOP!", "stop, stop", "Please stop.", "Stop. Open the claw."). whisper.cpp writes
# "Stop." and the old exact match missed it. This deliberately also stops on "don't stop the timer",
# "stop sign" or "remember to stop at zone A": on a robot a needless stop costs one Clear Stop, a missed
# stop can hurt someone, so we err toward stopping. ("e-stop" is covered by "stop". Keep in step with
# STOP_WORDS in engram/engine.py.)
STOP_WORDS = re.compile(r"\b(stop|estop|halt|freeze|abort|emergency|whoa)\b")
# Negated or called-off commands ("don't open the claw", "no, ...", "cancel", "wait, don't"). Planning the
# rest of the sentence would do the opposite of what was said, so these refuse. Checked after the rule
# prefix, so "remember that the vial must never ..." is still taught as a rule (rules never move anything).
NEGATION = re.compile(r"\b(do not|don'?t|\w+n't|cannot|never|not|no|cancel|never ?mind|wait|hold on)\b")
# Deferral backstop. A planned command runs NOW, so a command meant for later, on a condition, or chained to
# another step must not plan. Fail-closed, in two parts (see _deferral):
#  1. DEFERRAL, anywhere in the command: after(wards), when, once ..., until, before, during, while, if, then,
#     next, every, later, soon, shortly, eventually, tomorrow, ...; any time unit, singular or plural ("moments",
#     "a sec", "2 hrs", "a couple of minutes", "several days"; "second", "minute" and "min" only after a number
#     or quantity, so "the second vial" still parses); or a clock time ("5 pm", "17:30", "noon"). "then" and a
#     bare "next" count: the console plans one action per command, so "close the claw, then open it" planned
#     only the close.
#  2. PLACE_OR_TIME: after in, at, on, by, within, past or around, the words that follow must be a place or
#     target the grammar knows: a zone or tagged object from memory, "zone A", "tag 5", a scene word ("the bin",
#     "in front", "in place") or a distance ("by 10 cm"). Anything else ("in a jiffy", "at 5", "on my signal",
#     "at the end of calibration") asks. The "around" in "turn around" is the turn primitive, not a preposition.
# Only the leading "In N seconds/minutes, ..." (PROSPECTIVE, checked first) can be scheduled here.
_NUMWORD = r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety"
DEFERRAL = re.compile(
    r"\b(after(?:wards?)?|thereafter|when|whenever|once(?=\s+\w)|as soon as|until|till|before|during|while|meanwhile"
    r"|if|unless|upon|then|next(?!\s+to\b)|every|always|repeatedly|later|soon|shortly|presently|momentarily"
    r"|eventually|sometime|someday|today|tonight|tomorrow|overnight|from now"
    r"|moments?|secs?|seconds|mins|minutes|hrs?|hours?|days?|weeks?|weekends?|fortnights?|months?|years?"
    r"|mornings?|afternoons?|evenings?|nights?|noon|midnight|(?:mon|tues|wednes|thurs|fri|satur|sun)days?"
    rf"|(?:\d+(?:\.\d+)?\s*|(?:an?|{_NUMWORD}|few|couple(?: of)?|several|some|half|another|this|that|each|per)\s+)"
    r"(?:second|minute|min)"
    r"|\d{1,2}(?::\d{2})?\s*[ap]\.?m|[ap]\.m|pm|o'?clock|\d{1,2}:\d{2})\b")
PLACE_OR_TIME = re.compile(r"\b(in|at|on|by|within|past|(?<!turn )around)\b\s*")
_DETERMINERS = re.compile(r"(?:(?:the|a|an|this|that|these|those|my|your|our|its|his|her|their)\s+)*")
_SCENE = (r"(?:bin|tray|table|bench|shelf|shelves|floor|ground|place|front|back|middle|cent(?:er|re)|corner|side"
          r"|hand|gripper|claw|view|sight|frame|there|here|zone|area|spot|home|tag|marker|aruco)(?:e?s)?")
_DISTANCE = (rf"(?:\d+(?:\.\d+)?|{_NUMWORD})\s*(?:cm|mm|m|in|inch(?:es)?|(?:centi|milli)?met(?:er|re)s?|ft|feet|foot"
             r"|deg(?:rees?)?)")

QUIPS = {
    "plan": ["Plan staged. Your signature, please.", "Drafted. Nothing moves until you say so.",
             "Ready when you are. I've shown my work.", "Plan's on the board. I await your blessing."],
    "skill": ["I've done this before. Using the approved routine.", "Muscle memory engaged. Approved skill reused."],
    "abstain": ["I'd rather not. Reason's on screen.", "Declining that one, for everyone's sake.",
                "That's a no from me. Safety first, sass second."],
    "clarify": ["Which one, exactly?", "I need one more detail before I touch anything."],
    "rule": ["Noted, cited, and filed. The old rule stays on record.", "New rule learned. No retraining required."],
    "prospective": ["I'll remember. Future me has been notified.", "Scheduled. I'll ask before acting."],
    "stop": ["Stopping."],
    "info": ["Here's what I've got."],
}


def quip(kind: str) -> str:
    return random.choice(QUIPS.get(kind, QUIPS["info"]))


@dataclass
class ParseResult:
    kind: str                      # plan | abstain | clarify | rule | prospective | stop | info
    text: str
    plan: Plan | None = None
    reason: str = ""
    quip: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)  # why the parser decided what it did (shown on HUD)


def _num(s: str) -> float | None:
    s = s.strip().lower()
    m = re.search(r"-?\d+(\.\d+)?", s)
    if m:
        return float(m.group())
    for w in sorted(NUM_WORDS, key=len, reverse=True):
        if re.search(rf"\b{w}\b", s):
            return float(NUM_WORDS[w])
    return None


def _norm(text: str) -> str:
    t = text.lower().replace(chr(0x2019), "'").strip()  # a curly apostrophe (U+2019) still reads as one
    t = re.sub(r"[^\w\s:.'-]", " ", t)
    return re.sub(r"\s+", " ", t)


def resolve_object(t: str, bundle: dict) -> dict | None:
    m = re.search(r"\b(?:tag|marker|aruco)\s*#?\s*(\d+)\b", t)
    if m:
        tid = int(m.group(1))
        for b in bundle.get("bindings", []):
            if b.get("tag_id") == tid:
                return b
        return {"name": f"tag {tid}", "tag_id": tid, "source_id": "operator_utterance"}
    best = None
    for b in bundle.get("bindings", []):
        for name in [b.get("name", "")] + list(b.get("aliases", [])):
            if name and re.search(rf"\b{re.escape(name.lower())}\b", t):
                if best is None or len(name) > best[0]:
                    best = (len(name), b)
    return best[1] if best else None


def resolve_zone(t: str, bundle: dict) -> dict | None:
    m = re.search(r"\bzone\s*([a-z])\b", t)
    if m:
        z = m.group(1).upper()
        for zz in bundle.get("zones", []):
            if zz.get("zone") == z:
                return zz
        return {"name": f"zone {z}", "zone": z, "source_id": "operator_utterance"}
    best = None
    for zz in bundle.get("zones", []):
        for name in [zz.get("name", "")] + list(zz.get("aliases", [])):
            if name and re.search(rf"\b{re.escape(name.lower())}\b", t):
                if best is None or len(name) > best[0]:
                    best = (len(name), zz)
    return best[1] if best else None


def matching_constraint(t: str, bundle: dict) -> dict | None:
    for c in bundle.get("constraints", []):  # newest first: a newer rule supersedes
        for trig in c.get("applies_to", []):
            if trig and re.search(rf"\b{re.escape(trig.lower())}\b", t):
                return c
    return None


def fetch_steps(tag: int, zone: str) -> list[Step]:
    return [
        Step(op="search_tag", args={"tag_id": tag}, expect=f"tag {tag} found (turns in place if not in view)"),
        Step(op="approach_tag", args={"tag_id": tag, "stop_cm": 15}, expect="stopped ~15 cm from tag, LiDAR clear"),
        Step(op="pick_tagged", args={"tag_id": tag}, expect=f"gripper holding tag {tag} (sensor)"),
        Step(op="place_at", args={"zone": zone}, expect=f"finds zone {zone} marker, places tag {tag} there (camera)"),
    ]


def skill_plan(t: str, goal: str, bundle: dict, tag: int, zone: str, evidence: list[str]) -> Plan | None:
    for s in bundle.get("skills", []):
        if s.get("status") != "APPROVED":
            continue
        trig = s.get("triggers", [])
        if trig and not any(re.search(rf"\b{re.escape(w)}\b", t) for w in trig):
            continue
        steps = []
        for st in s.get("steps", []):
            args = {}
            for k, v in st.get("args", {}).items():
                if v == "{tag_id}":
                    v = tag
                elif v == "{zone}":
                    v = zone
                args[k] = v
            steps.append(Step(op=st["op"], args=args, expect=st.get("expect", "")))
        try:
            return validate_plan(Plan(goal=goal, steps=steps, evidence_ids=evidence + s.get("evidence_ids", [])[:2],
                                      skill_id=s["id"], planner="skill"))
        except PlanError:
            continue
    return None


FETCH_VERB = r"\b(bring|fetch|get|grab|move|put|take|deliver|carry|place|set)\b"
PROSPECTIVE = re.compile(r"^(?:in|after)\s+(\d+|\w+)\s*(seconds?|secs?|minutes?|mins?)\s*,?\s*(.+)$")


def is_stop(text: str) -> bool:
    return STOP_WORDS.search(_norm(text)) is not None


def _deferral(t: str, bundle: dict) -> tuple[str, str] | None:
    """(phrase, reason) for the first part of the normalized command that defers, conditions or chains it,
    or None. See DEFERRAL and PLACE_OR_TIME. Names from memory only ever excuse a place after a preposition:
    they are never removed before the DEFERRAL check, so a name cannot hide "tomorrow" or "after"."""
    m = DEFERRAL.search(t)
    if m:
        return m.group(1), f"'{m.group(1)}' sounds like later, a condition or another step"
    names = {_norm(str(n)).strip(" .") for item in bundle.get("zones", []) + bundle.get("bindings", [])
             for n in [item.get("name", "")] + list(item.get("aliases", []))}
    known = "".join("|" + re.escape(n) for n in sorted(names - {""}, key=len, reverse=True))
    place = re.compile(rf"(?:{_SCENE}|{_DISTANCE}{known})\b")
    for p in PLACE_OR_TIME.finditer(t):
        rest = t[p.end():]
        rest = rest[_DETERMINERS.match(rest).end():]
        if rest[:1].isalnum() and not place.match(rest):  # nothing after it ("4 in.") is fine
            phrase = " ".join([p.group(1)] + t[p.end():].split()[:2]).rstrip(".,:'-")
            return phrase, f"I can't tell if '{phrase}' is a place or a time"
    return None


def parse(text: str, bundle: dict) -> ParseResult:
    """Never raises for a request the primitives cannot express: a plan that fails validation
    (for example a tag id outside the allowed range) comes back as a clarification."""
    try:
        return _parse(text, bundle)
    except PlanError as e:
        return ParseResult("clarify", text, reason=f"I can't plan that safely: {e}", quip=quip("clarify"),
                           trace=[f"plan rejected by validation: {e}"])


def _parse(text: str, bundle: dict) -> ParseResult:
    t = _norm(text)
    trace: list[str] = [f"normalized: '{t}'"]
    if not t:
        return ParseResult("clarify", text, reason="empty command", quip=quip("clarify"), trace=trace)

    # 0. STOP always wins, no approval needed (see STOP_WORDS)
    m = STOP_WORDS.search(t)
    if m:
        return ParseResult("stop", text, quip=quip("stop"), trace=trace + [f"stop word '{m.group(1)}'"])

    # 1. safety screen
    for pat, why in UNSAFE:
        if re.search(pat, t):
            trace.append(f"unsafe pattern matched: {pat}")
            return ParseResult("abstain", text, reason=why, quip=quip("abstain"), trace=trace)

    # 2. teaching a rule
    m = re.match(r"^(?:new rule|rule|remember( that)?|from now on|note( that)?|learn( that)?)\s*[:,-]?\s*(.+)$", t)
    if m:
        body = text.split(":", 1)[1].strip() if ":" in text else m.group(m.lastindex)
        return ParseResult("rule", text, data={"rule_text": body}, quip=quip("rule"), trace=trace + ["rule prefix"])

    # 2b. a negated command is never planned, and never handed to the LLM fallback (see NEGATION)
    m = NEGATION.search(t)
    if m:
        return ParseResult("abstain", text, reason=f"You said '{m.group(1)}', so nothing will move. "
                           "To make it a rule, say 'Remember that ...'", quip=quip("abstain"),
                           trace=trace + [f"negation '{m.group(1)}'"])

    # 3. prospective ("in 2 minutes, bring the vial to zone B")
    m = PROSPECTIVE.match(t)
    if m:
        n = _num(m.group(1))
        if n is None:
            return ParseResult("clarify", text, reason="couldn't read the delay", quip=quip("clarify"), trace=trace)
        secs = n * (60 if m.group(2).startswith("min") else 1)
        inner = m.group(3)
        sub = parse(inner, bundle)
        if sub.kind == "prospective":  # "In 5 minutes, in 5 seconds, ..." would schedule 5 s and drop the 5 min
            return ParseResult("clarify", text, reason="Give one delay, for example 'In 5 minutes, open the claw'",
                               quip=quip("clarify"), trace=trace + ["nested delay"])
        if sub.kind != "plan" or sub.plan is None:
            # Only a plan is scheduled. Anything else (stop, refusal, question) comes back as itself, and never
            # goes to the LLM fallback, which would plan the task for NOW and drop the delay.
            sub.data.pop("llm_eligible", None)
            sub.trace = trace + ["prospective inner command failed"] + sub.trace
            return sub
        return ParseResult("prospective", text, plan=sub.plan, quip=quip("prospective"),
                           data={"delay_s": secs, "inner": inner}, trace=trace + [f"prospective in {secs:.0f}s"] + sub.trace)

    # 3b. a command meant for later, on a condition, or chained to another step must not run now (see DEFERRAL)
    d = _deferral(t, bundle)
    if d:
        phrase, why = d
        return ParseResult("clarify", text, reason=f"{why}, so I won't do anything now. Say one thing to do now, "
                           "or 'In 5 minutes, ...' to schedule it.", quip=quip("clarify"),
                           trace=trace + [f"deferral: '{phrase}'"])

    goal = text.strip()

    # 4. simple primitives
    if re.search(r"\b(what do you see|look around|observe|scan|what's in front|whats in front|look)\b", t) and not re.search(FETCH_VERB, t):
        return _ok(goal, [Step(op="observe", expect="fresh frame + detections")], [], trace + ["observe"], physical=False)

    if re.search(r"\b(open)\b.*\b(claw|gripper|hand)\b", t):
        return _ok(goal, [Step(op="gripper_open", expect="claw open")], [], trace + ["gripper open"])
    if re.search(r"\b(close|shut)\b.*\b(claw|gripper|hand)\b", t):
        return _ok(goal, [Step(op="gripper_close", expect="claw closed (grasp state unknown)")], [], trace + ["gripper close"])

    m = re.search(r"\bturn\s+(around)\b|\bturn\s+(left|right)(?:\s+(.+?))?(?:\s*(?:degrees?|deg))?$", t)
    if m:
        if m.group(1):
            deg = 180.0
        else:
            n = _num(m.group(3) or "") if m.group(3) else 90.0
            if n is None:
                n = 90.0
            deg = n if m.group(2) == "right" else -n
        if abs(deg) > 180:
            return ParseResult("abstain", text, reason=f"turns are bounded to 180 degrees per step (asked {abs(deg):.0f})",
                               quip=quip("abstain"), trace=trace)
        return _ok(goal, [Step(op="turn_bounded", args={"degrees": deg}, expect=f"heading changed {deg:+.0f} deg")], [], trace + ["turn"])

    m = re.search(r"\b(?:drive|go|move|roll|back up|reverse)\b\s*(forward|forwards|ahead|straight|back|backward|backwards|up)?\s*(.*)$", t)
    if m and not re.search(r"\bto\b", t) and not resolve_object(t, bundle):
        direction = m.group(1) or ("back" if re.search(r"back up|reverse", t) else "forward")
        rest = m.group(2) or ""
        n = _num(rest)
        unit = "cm"
        if n is None:
            n = 20.0
        if re.search(r"\b(inch|inches|in)\b", rest):
            n, unit = n * 2.54, "in"
        elif re.search(r"\b(meter|meters|metre|m)\b", rest) and not re.search(r"\bcm|centi", rest):
            n, unit = n * 100, "m"
        if direction.startswith("back") or re.search(r"back up|reverse", t):
            n = -n
        if abs(n) > 50:
            return ParseResult("abstain", text, reason=f"drive is bounded to 50 cm per step (asked {abs(n):.0f} cm). Split it up or approve multiple steps.",
                               quip=quip("abstain"), trace=trace + [f"unit {unit}"])
        return _ok(goal, [Step(op="drive_bounded", args={"distance_cm": round(n, 1)}, expect=f"moved {n:+.0f} cm, LiDAR clear")], [], trace + ["drive"])

    m = re.search(r"\b(find|locate|where is|where's|look for|search for)\b", t)
    if m and not re.search(FETCH_VERB, t):
        obj = resolve_object(t, bundle)
        if not obj:
            return _clarify(text, "I don't know which tagged object you mean", bundle, trace)
        return _ok(goal, [Step(op="detect_tag", args={"tag_id": obj["tag_id"]}, expect=f"tag {obj['tag_id']} visible")],
                   [obj.get("source_id", "")], trace + [f"object -> tag {obj['tag_id']} via {obj.get('source_id')}"], physical=False)

    # 5. composite fetch, possibly decided by a rule in memory
    rule = matching_constraint(t, bundle)
    has_verb = re.search(FETCH_VERB, t) is not None
    if has_verb or rule:
        obj = resolve_object(t, bundle)
        zone = resolve_zone(t, bundle)
        evidence: list[str] = []
        if rule:
            trace.append(f"rule {rule['id']} applies ({rule['source_id']})")
            req = rule.get("requires", {})
            if obj and obj.get("tag_id") != req.get("tag_id"):
                trace.append(f"operator named tag {obj.get('tag_id')} but rule requires tag {req.get('tag_id')}")
                return ParseResult("clarify", text, reason=f"Rule {rule['id']} says tag {req.get('tag_id')} is needed here, but you named "
                                   f"{obj.get('name')}. Confirm which one.", quip=quip("clarify"), trace=trace,
                                   data={"evidence_ids": [rule["source_id"]]})
            if not obj:
                obj = next((b for b in bundle.get("bindings", []) if b.get("tag_id") == req.get("tag_id")),
                           {"name": f"tag {req.get('tag_id')}", "tag_id": req.get("tag_id"), "source_id": rule["source_id"]})
            if not zone:
                zone = next((z for z in bundle.get("zones", []) if z.get("zone") == req.get("zone")),
                            {"name": f"zone {req.get('zone')}", "zone": req.get("zone"), "source_id": rule["source_id"]})
            evidence.append(rule["source_id"])
        if not obj:
            if re.search(PRONOUNS, t):
                return _clarify(text, "'it' is ambiguous. Name the object or its tag number", bundle, trace)
            return _clarify(text, "I couldn't match that to a tagged object in memory", bundle, trace)
        if not zone:
            return ParseResult("clarify", text, reason=f"Where should {obj['name']} go? Say a zone.", quip=quip("clarify"),
                               trace=trace, data={"options": [z.get("name") for z in bundle.get("zones", [])]})
        evidence += [e for e in (obj.get("source_id"), zone.get("source_id")) if e and e not in evidence]
        trace.append(f"object '{obj['name']}' -> tag {obj['tag_id']}; destination '{zone['name']}' -> zone {zone['zone']}")
        sp = skill_plan(t, goal, bundle, int(obj["tag_id"]), str(zone["zone"]), evidence)
        if sp:
            r = ParseResult("plan", text, plan=sp, quip=quip("skill"), trace=trace + [f"reused APPROVED skill {sp.skill_id}"])
            return r
        return _ok(goal, fetch_steps(int(obj["tag_id"]), str(zone["zone"])), evidence, trace + ["grammar: fetch"])

    return ParseResult("abstain", text, reason="I don't have a safe primitive for that yet", quip=quip("abstain"),
                       trace=trace + ["no grammar rule matched"], data={"llm_eligible": True})


def _ok(goal, steps, evidence, trace, physical=True) -> ParseResult:
    plan = validate_plan(Plan(goal=goal, steps=steps, evidence_ids=[e for e in evidence if e]))
    return ParseResult("plan", goal, plan=plan, quip=quip("plan"), trace=trace)


def _clarify(text, why, bundle, trace) -> ParseResult:
    return ParseResult("clarify", text, reason=why, quip=quip("clarify"), trace=trace,
                       data={"options": [f"{b.get('name')} (tag {b.get('tag_id')})" for b in bundle.get("bindings", [])]})
