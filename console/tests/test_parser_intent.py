"""Spoken-command safety tables, from a real end-to-end measurement with whisper.cpp output.

  STOP     whisper writes "Stop." (capital, period). The exact-match stop check missed it,
           so a spoken stop never stopped the robot. Stop must always win and never be planned.
  NEGATED  "Don't open the claw" was planned as an OPEN.
  FUTURE   "Open the claw after calibration finishes" was planned as an immediate open.

Expected kinds were written down before the fix; every table had failing rows on the old parser.
"""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from server import main
from server.adapters.memory import MockMemory
from server.parser import parse

BUNDLE = asyncio.run(MockMemory().query("seed"))
CURLY = chr(0x2019)  # the right single quote some transcribers write in "don't"

STOP = [
    "Stop.", "Stop!", "stop, stop", "STOP", "Stop now.", "Please stop.", "Halt.", "Emergency stop!",
    " Stop. ", "Stop, stop, stop.", "STOP!!!", "Stop...", "Stop it.", "E-stop!", "Whoa!", "Freeze.", "Abort!",
    "Emergency!",
    "Stop. Open the claw.", "Open the claw. Stop!", "Bring the red cup to zone B. Stop!",
    "Stop opening the claw.", "In 5 seconds, stop.",
    # Deliberate false positives (see STOP_WORDS in parser.py): a needless stop costs one Clear Stop,
    # a missed stop can hurt someone.
    "Don't stop the timer.", "Stop sign.", "Remember to stop at zone A.",
    # "Wait", "Pause", "Hold on", "Hold it" and "Hang on" stop too (they used to refuse, and the robot kept going).
    "Wait!", "Wait.", "Wait, don't open the claw.", "Wait, open the claw.", "Pause.", "Pause!", "Hold on.",
    "Hold on, open the claw.", "Hold it!", "Hang on.", "Freeze!", "Open the claw. Wait!",
    "Remember to wait for the student.",  # deliberate: a rule with a stop word is taught through /rules
]

# Phrases, not words: holding something is not a stop.
NOT_STOP = ["Hold the vial.", "Hold the claw closed.", "Is the gripper holding the vial?",
            "Put the vial in the waiting area."]

NEGATED = [
    "Don't open the claw", "Don't open the claw.", "Do not open the claw.", "Never open the claw.",
    f"Don{CURLY}t open the claw.", "dont open the claw", "You shouldn't open the claw.",
    "Not now, open the claw.", "No, open the claw.", "No, don't close the gripper.",
    "Cancel, open the claw.", "Cancel that.",
    "Never mind, close the claw.", "Do not bring the red cup to zone B.", "Don't drive forward 10 cm.",
    "Don't turn left.", "In 5 seconds, don't open the claw.",
]

FUTURE = [
    "Open the claw after calibration finishes", "Open the claw after calibration finishes.",
    "After calibration finishes, open the claw.", "When calibration finishes, open the claw.",
    "Once the calibration is done, close the claw.", "As soon as calibration finishes, drive forward 10 cm.",
    "Open the claw when the vial arrives.", "Close the claw once calibration is done.",
    "Bring the vial to zone B after the beep.", "Whenever the bench is clear, open the claw.",
    "Open the claw later.", "Open the claw in 5 seconds.", "Drive forward 10 cm in two minutes.",
    "In 5 seconds, open the claw after calibration.",
    # Review round 2: durations and times the console cannot schedule ("in 2 days" planned an open NOW).
    "Open the claw in 2 days.", "Open the claw in 3 hours.", "Open the claw in an hour.",
    "Open the claw in half an hour.", "Open the claw in two and a half hours.", "Open the claw in 2 weeks.",
    "Open the claw in a month.", "Open the claw in 1 year.", "Open the claw 10 minutes from now.",
    "Open the claw tomorrow.", "Open the claw tonight.", "Open the claw later today.", "Open the claw today.",
    "Open the claw next week.", "Open the claw next Monday.", "Open the claw on Friday.",
    "Open the claw this afternoon.", "Open the claw in the morning.", "Open the claw next time.",
    "Open the claw at 5 pm.", "Open the claw at 5pm.", "Open the claw at 5 p.m.", "Open the claw at 17:30.",
    "Open the claw at five o'clock.", "Open the claw at noon.", "Drive forward 10 cm by 5 pm.",
    "In 2 days, open the claw.", "In 3 hours, close the claw.", "In an hour, open the claw.",
    "Tomorrow, open the claw.", "Next week, bring the vial to zone B.", "At 5 pm, open the claw.",
    # Review round 3: plural/singular units, vague quantities, and deferral words.
    "Open the claw in a few moments.", "Open the claw in a couple of minutes.", "Open the claw in a moment.",
    "Open the claw in a sec.", "Open the claw in 2 hrs.", "Open the claw in several days.",
    "Open the claw in 30 secs.", "Open the claw afterwards.", "Open the claw after that.",
    "Open the claw soon.", "Open the claw shortly.", "Open the claw eventually.",
    "Open the claw before calibration.", "Open the claw until the vial arrives.",
    # "then" makes a sequence: the console plans one action per command, so the rest would be dropped
    # or run out of order ("close the claw then open it" planned only the close).
    "Then open the claw.", "Close the claw, then open it.",
    "Open the claw this week.", "Open the claw every day.", "Open the claw at that point.",
    "Open the claw at the end of calibration.",
    # Conditions and durations are the same class: the command would run now, not when or for as long as said.
    "If the vial is on the bench, open the claw.", "Open the claw while the arm moves.",
    "Open the claw during calibration.", "Open the claw every 5 minutes.", "Open the claw for 10 seconds.",
    "Drive forward for 2 minutes.", "Open the claw for a minute.", "Open the claw around 5.",
    "Open the claw within the hour.", "Open the claw at half past five.",
]

# Fail-closed: after in/at/by/on/within/next, a clause that is not recognizably a place asks too.
FAIL_CLOSED = [
    "Open the claw in a jiffy.", "Open the claw at once.", "Open the claw at 5.", "Open the claw at five.",
    "Open the claw by then.", "Open the claw on my signal.", "Open the claw on cue.", "Open the claw in due course.",
    "Open the claw in time.", "Open the claw next.", "Put the vial in a box.",
    # Review round 4: a distance counts only as "by N cm" in a drive (or "by N degrees" in a turn), and a
    # recognized phrase must end its clause.
    "Open the claw within 5 cm of the vial.", "Close the gripper at 10 cm.", "Open the claw by 10 cm.",
    "Turn left by 10 cm.", "Drive forward within 10 cm.", "Drive forward by 10 cm of the vial.",
    "Open the claw in place soonish.", "Open the claw in front of the deadline.",
]

# Review round 4: the grammar plans one primitive, or one fetch of one object to one zone. A second action or
# a second object was silently dropped ("close the claw and open the gripper" staged only the open).
MULTIPLE = [
    "Close the claw and open the gripper.", "Open the claw, close it.", "Open the claw & close it.",
    "Drive forward 10 cm plus turn left.", "Open the claw, also close it.", "Close the claw. Open the gripper.",
    "Open the claw! Close it!", "Turn left, drive forward 10 cm.", "Drive forward 10 cm and turn left.",
    "Bring the red cup and the vial to zone B.", "Bring tag 7 and tag 8 to zone A.",
    "Bring the vial to zone A or zone B.", "Open the claw or close it.",
    "Bring the vial, the cup to zone B.", "Bring tag 7, tag 8 to zone A.",
]

# The leading "In N seconds/minutes, <task>" form schedules only a plan. Anything else the inner task
# parses to comes back as itself, never as a prospective without a plan, and never to the LLM fallback.
PROSPECTIVE_INNER = [
    ("In 5 seconds, open the claw after calibration.", "clarify"),
    ("In 5 seconds, open the claw tomorrow.", "clarify"),
    ("In 5 minutes, in 5 seconds, open the claw.", "clarify"),  # would schedule 5 s and drop the 5 min
    ("In 5 seconds, don't open the claw.", "abstain"),
    ("In 5 seconds, cancel that.", "abstain"),
    ("In 5 seconds, bring it here.", "clarify"),
    ("In 5 seconds, juggle.", "abstain"),  # no grammar: the LLM would plan it for NOW
    ("In 5 seconds, stop.", "stop"),
    ("In 2 minutes, open the claw. Stop!", "stop"),
    ("In 5 seconds, open the claw, close it.", "clarify"),  # scheduled only the open
]

STILL_WORKS = [
    ("Close the claw.", "plan", "gripper_close"),
    ("Open the claw.", "plan", "gripper_open"),
    ("open the claw", "plan", "gripper_open"),
    ("Drive forward 10 cm.", "plan", "drive_bounded"),
    ("Turn left 90.", "plan", "turn_bounded"),
    ("Turn left once.", "plan", "turn_bounded"),
    ("Bring the red cup to zone B.", "plan", "search_tag"),
    ("What do you see?", "plan", "observe"),
    ("In 5 seconds, open the claw.", "prospective", "gripper_open"),
    ("After 5 seconds, close the claw.", "prospective", "gripper_close"),
    ("In 2 minutes, bring the red cup to zone B.", "prospective", "search_tag"),
    ("In five minutes, open the claw.", "prospective", "gripper_open"),
    ("Bring the cup next to the vial to zone B.", "plan", "search_tag"),
    # Places after in/at/on/next are consumed by the grammar, not mistaken for time.
    ("Put the red cup in zone A.", "plan", "search_tag"),  # "the cup" alone is not a known object here
    ("Put the vial on the calibration bench.", "plan", "search_tag"),
    ("Bring the vial to zone B in the bin.", "plan", "search_tag"),
    ("Find the vial on the sample shelf.", "plan", "detect_tag"),
    ("Look at the vial.", "plan", "observe"),
    ("Look at tag 5.", "plan", "observe"),
    ("What's in front of you?", "plan", "observe"),
    ("Drive forward 4 in.", "plan", "drive_bounded"),  # "in" is the inch unit here
    ("Turn left 90 degrees in place.", "plan", "turn_bounded"),
    ("Drive forward by 10 cm.", "plan", "drive_bounded"),  # a distance after "by" is not a time
    ("Turn around.", "plan", "turn_bounded"),
    ("Turn around now.", "plan", "turn_bounded"),  # "turn around" is the turn primitive, not "around <time>"
    ("Put the cup in zone A.", "clarify", None),  # asks which cup ("cup" alone is not in memory), not a deferral
    ("Put the vial in the bin.", "clarify", None),  # "the bin" ends its clause; it asks for a zone, not a time
    ("Drive back by 5 cm.", "plan", "drive_bounded"),
    ("Turn left by 90 degrees.", "plan", "turn_bounded"),
    # One action with a comma is still one action.
    ("Robot, open the claw.", "plan", "gripper_open"),
    ("Okay, close the claw.", "plan", "gripper_close"),
    ("Close the claw, please.", "plan", "gripper_close"),
    ("Turn left, 90 degrees.", "plan", "turn_bounded"),
    ("Drive forward 10.5 cm.", "plan", "drive_bounded"),  # a decimal point is not a sentence end
    ("Turn left.", "plan", "turn_bounded"),  # whisper's period broke the turn grammar's end anchor
    ("Turn right.", "plan", "turn_bounded"),
    ("Remember that the vial must never leave zone B.", "rule", None),
    ("From now on, do not drive into zone A.", "rule", None),
]


@pytest.mark.parametrize("text", STOP)
def test_spoken_stop_always_wins(text):
    r = parse(text, BUNDLE)
    assert (r.kind, r.plan) == ("stop", None), (text, r.kind, r.trace)


@pytest.mark.parametrize("text", NOT_STOP)
def test_holding_something_is_not_a_stop(text):
    assert parse(text, BUNDLE).kind != "stop", text


@pytest.mark.parametrize("text", NEGATED)
def test_negated_command_is_refused_not_planned(text):
    r = parse(text, BUNDLE)
    assert r.kind in ("abstain", "clarify") and r.plan is None, (text, r.kind, r.trace)
    assert not r.data.get("llm_eligible"), "the LLM fallback could plan the negated action"


@pytest.mark.parametrize("text", FUTURE + FAIL_CLOSED)
def test_future_condition_is_not_run_now(text):
    r = parse(text, BUNDLE)
    assert (r.kind, r.plan) == ("clarify", None), (text, r.kind, r.trace)


@pytest.mark.parametrize("text", MULTIPLE)
def test_more_than_one_action_or_object_asks(text):
    r = parse(text, BUNDLE)
    assert (r.kind, r.plan) == ("clarify", None), (text, r.kind, r.trace)
    assert "One thing at a time" in r.reason, r.reason


def test_a_name_with_and_in_it_is_one_object():
    shaker = {"name": "salt and pepper shaker", "aliases": ["shaker"], "tag_id": 11, "source_id": "test"}
    r = parse("Bring the salt and pepper shaker to zone A.", {**BUNDLE, "bindings": BUNDLE["bindings"] + [shaker]})
    assert r.kind == "plan" and r.plan.steps[0].args == {"tag_id": 11}, (r.kind, r.reason, r.trace)


@pytest.mark.parametrize("text,kind", PROSPECTIVE_INNER)
def test_prospective_schedules_only_a_plan(text, kind):
    r = parse(text, BUNDLE)
    assert (r.kind, r.plan) == (kind, None), (text, r.kind, r.trace)
    assert not r.data.get("llm_eligible"), "the LLM fallback would plan the task now and drop the delay"


@pytest.mark.parametrize("text,kind,first_op", STILL_WORKS)
def test_ordinary_commands_still_parse(text, kind, first_op):
    r = parse(text, BUNDLE)
    assert r.kind == kind, (text, r.kind, r.reason, r.trace)
    assert not any(s.startswith("deferral") for s in r.trace), r.trace
    if first_op:
        assert r.plan.steps[0].op == first_op


def test_spoken_stop_latches_the_console():
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        op = c.post("/commands", json={"text": "Drive forward 10 cm.", "source": "voice"}).json()["operation"]
        assert c.post("/commands", json={"text": "Stop.", "source": "voice"}).json()["kind"] == "stop"
        assert main.engine.stopped
        assert c.post(f"/operations/{op['id']}/approve", json={"digest": op["digest"]}).status_code == 409
        c.post("/stop/clear")


STOP_PATH_TELEMETRY = {"emit heard", "emit parsed", "emit say", "emit stop", "emit operation",
                       "ledger command", "ledger parse", "ledger estop"}


@pytest.mark.parametrize("route", ["voice command", "stop button"])
@pytest.mark.parametrize("failure", ["emit raises", "ledger raises", "both raise", "emit hangs"])
def test_stop_acts_before_telemetry_and_survives_its_failure(monkeypatch, route, failure):
    # Review round 4: the "heard" emit and the "command" ledger write ran before the stop check, so a stalled
    # HUD client or a failed ledger write meant the robot never stopped. Telemetry now comes after the stop.
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        op = c.post("/commands", json={"text": "Drive forward 10 cm.", "source": "voice"}).json()["operation"]
        order: list[str] = []
        real_stop = main.engine.robot.stop

        async def robot_stop():
            order.append("robot.stop")
            return await real_stop()

        async def emit(type_, **data):
            order.append(f"emit {type_}")
            if failure == "emit hangs":
                await asyncio.sleep(4)
            if failure in ("emit raises", "both raise"):
                raise RuntimeError("a HUD subscriber broke")
            return {}

        def append(event, **rec):
            order.append(f"ledger {event}")
            if failure in ("ledger raises", "both raise"):
                raise OSError("disk full")
            return "ev_test"

        with monkeypatch.context() as m:
            m.setattr(main.engine.robot, "stop", robot_stop)
            m.setattr(main.bus, "emit", emit)
            m.setattr(main.ledger, "append", append)
            t0 = time.monotonic()
            if route == "voice command":
                r = c.post("/commands", json={"text": "Stop.", "source": "voice"})
            else:
                r = c.post("/stop")
            elapsed = time.monotonic() - t0
        assert r.status_code == 200 and elapsed < 3.0, (r.status_code, elapsed)
        assert route != "voice command" or r.json()["kind"] == "stop"
        assert main.engine.stopped and order.count("robot.stop") == 1, order
        telemetry = [i for i, e in enumerate(order) if e in STOP_PATH_TELEMETRY]
        assert order.index("robot.stop") < min(telemetry, default=len(order)), order
        assert main.engine.ops[op["id"]].status == "CANCELLED"
        c.post("/stop/clear")


def test_a_failing_robot_stop_still_stops_the_brain(monkeypatch):
    # The robot and the brain are stopped at once, each failure on its own: before, a robot call that raised
    # (for example on a reply that is not JSON) skipped the brain's stop and the cancellations.
    told = []

    async def robot_stop():
        raise ValueError("robot reply was not JSON")

    async def brain_stop():
        told.append("stop")
        return {"stopped": True}

    async def brain_clear():
        return {}

    with TestClient(main.app) as c:
        c.post("/stop/clear")
        with monkeypatch.context() as m:
            m.setattr(main.brain, "enabled", lambda: True)
            m.setattr(main.brain, "stop", brain_stop)
            m.setattr(main.brain, "clear_stop", brain_clear)
            m.setattr(main.engine.robot, "stop", robot_stop)
            r = c.post("/stop")
            assert r.status_code == 200 and main.engine.stopped and told == ["stop"], (r.status_code, told)
            assert r.json()["status"] == "stop_request_failed" and "ValueError" in r.json()["error"], r.json()
            c.post("/stop/clear")


def test_spoken_stop_does_not_wait_on_the_brain(monkeypatch):
    # With the brain bridge on, every command used to go to the brain first. A stop must not
    # depend on the brain answering (or understanding it); engine.stop() still tells the brain.
    told = []

    async def must_not_plan(text):
        raise AssertionError("a stop was sent to the brain as a command")

    async def brain_stop():
        told.append("stop")
        return {"stopped": True}

    async def brain_clear():
        return {}

    with TestClient(main.app) as c:
        c.post("/stop/clear")
        monkeypatch.setattr(main.brain, "enabled", lambda: True)
        monkeypatch.setattr(main.brain, "command", must_not_plan)
        monkeypatch.setattr(main.brain, "stop", brain_stop)
        monkeypatch.setattr(main.brain, "clear_stop", brain_clear)
        out = c.post("/commands", json={"text": "Stop.", "source": "voice"}).json()
        assert out["kind"] == "stop", out
        assert main.engine.stopped and told == ["stop"]
        c.post("/stop/clear")
