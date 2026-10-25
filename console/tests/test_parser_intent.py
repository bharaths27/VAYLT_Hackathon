"""Spoken-command safety tables, from a real end-to-end measurement with whisper.cpp output.

  STOP     whisper writes "Stop." (capital, period). The exact-match stop check missed it,
           so a spoken stop never stopped the robot. Stop must always win and never be planned.
  NEGATED  "Don't open the claw" was planned as an OPEN.
  FUTURE   "Open the claw after calibration finishes" was planned as an immediate open.

Expected kinds were written down before the fix; every table had failing rows on the old parser.
"""
import asyncio
import logging
import threading
import time

import httpx
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

# Review round 6: a primitive planned whatever else was said ("Open the claw following calibration." planned an
# immediate open). A command plans only when the whole utterance is that command, give or take filler such as
# "please" or "can you"; anything left over asks and never goes to the model fallback. The rows after the first
# block use words that no list knows, so only the whole-utterance rule can catch them.
LEFTOVER = [
    "Open the claw following calibration.", "Open the claw once calibration ends.",
    "Open the claw as soon as calibration ends.", "Open the claw when calibration ends.",
    "Open the claw whenever calibration ends.", "Open the claw upon calibration.",
    "Open the claw pending calibration.", "Open the claw provided calibration passed.",
    "Open the claw if calibration passed.", "Open the claw unless calibration failed.",
    "Open the claw until calibration ends.", "Open the claw before calibration.",
    "Open the claw by the time calibration ends.", "Open the claw the moment calibration ends.",
    "Drive forward 10 cm following calibration.", "Turn left following calibration.",
    "Find the vial following calibration.", "Bring the red cup to zone B following calibration.",
    "Prep the bench for calibration following the meeting.", "Juggle following calibration.",
    # No grammar and no rule trigger here, so only the deferral words keep these from the model fallback.
    "Juggle following the meeting.", "Dance pending approval.", "Wave provided it is safe.",
    # words no list knows
    "Open the claw subsequent to calibration.", "Open the claw contingent on calibration.", "Open the claw halfway.",
    "Close the gripper gently.", "Turn left 90 degrees twice.", "Drive forward 10 cm three times.",
    "Bring the red cup to zone B as needed.", "What do you see behind the bench?",
]

# Review round 6: only "In N seconds/minutes, ..." is scheduled; "After N seconds, ..." asks to be said that way.
AFTER = ["After 5 seconds, close the claw.", "After 2 minutes, bring the red cup to zone B.",
         "After five minutes, open the claw."]

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
    ("In 5 seconds, juggle.", "clarify"),  # no grammar: the LLM would plan it for NOW (round 10: asks)
    # Review round 10: only a robot action can be scheduled. A rule was taught at once ("In 5 minutes, remember
    # that ..."), and a question or a look was scheduled; they ask now ("say the rule now, or schedule an action").
    ("In 5 minutes, remember that the vial must remain in zone B.", "clarify"),
    ("In 5 minutes, rule: the vial stays in zone B.", "clarify"),
    ("In 5 minutes, note that the bench is wet.", "clarify"),
    ("In 5 minutes, from now on keep the vial in zone B.", "clarify"),
    ("In 5 minutes, what do you see?", "clarify"),
    ("In 5 minutes, look around.", "clarify"),
    ("In 5 minutes, where is the vial?", "clarify"),
    ("In 5 minutes, find the vial.", "clarify"),
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
    ("In 2 minutes, bring the red cup to zone B.", "prospective", "search_tag"),
    ("In five minutes, open the claw.", "prospective", "gripper_open"),
    ("In 5 minutes, drive forward 10 cm.", "prospective", "drive_bounded"),
    ("In 1 minute, turn left 90.", "prospective", "turn_bounded"),
    # Round 6: this used to plan a fetch of the VIAL ("the cup" is not in memory, "vial" is); now it asks.
    ("Bring the cup next to the vial to zone B.", "clarify", None),
    # Filler around a command is fine.
    ("Can you open the claw?", "plan", "gripper_open"),
    ("Please close the gripper now.", "plan", "gripper_close"),
    ("Hey robot, turn left 90 degrees.", "plan", "turn_bounded"),
    ("Could you drive forward 10 cm for me?", "plan", "drive_bounded"),
    ("Where is the vial?", "plan", "detect_tag"),
    ("Look for the vial.", "plan", "detect_tag"),  # used to plan "observe"
    ("Prep the bench for calibration.", "plan", "search_tag"),
    ("Bring the vial to the bench for calibration.", "plan", "search_tag"),
    ("Bring the red cup to the sample shelf.", "plan", "search_tag"),
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


@pytest.mark.parametrize("text", LEFTOVER)
def test_a_leftover_qualifier_asks_instead_of_planning(text):
    r = parse(text, BUNDLE)
    assert (r.kind, r.plan) == ("clarify", None), (text, r.kind, r.trace)
    assert not r.data.get("llm_eligible"), "a leftover qualifier must not go to the model fallback"


@pytest.mark.parametrize("text", LEFTOVER)
def test_a_leftover_qualifier_never_reaches_the_model(monkeypatch, text):
    calls = []

    async def propose(t, bundle):
        calls.append(t)
        return None, "model declined (test)"

    monkeypatch.setattr(main.config, "LLM_URL", "http://model.test")
    monkeypatch.setattr(main.llm, "propose", propose)
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        out = c.post("/commands", json={"text": text, "source": "voice"}).json()
    assert out["kind"] == "clarify" and "operation" not in out and calls == [], (text, out.get("kind"), calls)


@pytest.mark.parametrize("text", AFTER)
def test_after_a_delay_asks_to_rephrase(text):
    r = parse(text, BUNDLE)
    assert (r.kind, r.plan) == ("clarify", None) and "'In " in r.reason, (text, r.kind, r.reason)
    assert not r.data.get("llm_eligible")


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
    assert "rule_text" not in r.data, "a rule inside a delay must not be taught"


@pytest.mark.parametrize("text", ["In 5 minutes, remember that the vial must remain in zone B.",
                                  "In 5 minutes, what do you see?"])
def test_a_deferred_rule_or_question_is_neither_taught_nor_scheduled(text):
    # Review round 10: the route taught "In 5 minutes, remember that ..." at once, because the parser returned
    # the inner rule as itself. Nothing may be stored: no rule, no new source, no scheduled task.
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        before = (len(main.memory.sources), len(main.memory.constraints), len(main.memory.prospective))
        out = c.post("/commands", json={"text": text, "source": "voice"}).json()
        after = (len(main.memory.sources), len(main.memory.constraints), len(main.memory.prospective))
    assert out["kind"] == "clarify" and "rule" not in out and "prospective" not in out, out
    assert before == after, (before, after)
    assert "rule now" in out["reason"] and "In 5 minutes, open the claw" in out["reason"], out["reason"]


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
        return {"adapter": "mock", "stopped": True}

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
            # 502, not 200: the HUD then shows "STOP request failed: use the robot's physical button!"
            assert r.status_code == 502 and main.engine.stopped and told == ["stop"], (r.status_code, told)
            assert r.json()["status"] == "stop_request_failed" and "ValueError" in r.json()["error"], r.json()
            c.post("/stop/clear")


OFF = "brain bridge off"
ROBOT_OK = {"stopped": True, "latched": True}  # the robot server's reply to /robot/stop
MOCK_OK = {"status": "stopped", "simulated": True}  # the console mock robot's reply
BRAIN_OK = {"adapter": "mock", "stopped": True}  # the brain answers /stop with its robot's state
# Review rounds 7 and 9: a reply confirms a stop only when it says so explicitly, in its own target's documented
# shape. The robot server must say stopped and latched, both exactly true; the status-only shape counts only from
# the console's mock robot; the brain must answer with its robot's state. Anything else fails closed.
STOP_REPLIES = [  # (the console robot adapter's reply contract, robot reply, brain reply or OFF, confirmed)
    ("robot_server", ROBOT_OK, OFF, True),
    ("robot_server", {"stopped": True}, OFF, False),  # stopped, but no latch
    ("robot_server", {"latched": True}, OFF, False),
    ("robot_server", {"stopped": True, "latched": False}, OFF, False),
    ("robot_server", {"stopped": True, "latched": "yes"}, OFF, False),
    ("robot_server", {"stopped": "yes", "latched": True}, OFF, False),
    ("robot_server", {"stopped": 1, "latched": True}, OFF, False),
    ("robot_server", {"status": "stopped"}, OFF, False),  # the mock's shape, from a real robot
    ("robot_server", MOCK_OK, OFF, False),
    ("robot_server", {**ROBOT_OK, "error": "driver fault"}, OFF, False),
    ("robot_server", None, OFF, False), ("robot_server", {}, OFF, False), ("robot_server", [], OFF, False),
    ("robot_server", "stopped", OFF, False), ("robot_server", True, OFF, False),
    ("robot_server", {"status": "error"}, OFF, False), ("robot_server", {"status": "ok"}, OFF, False),
    ("mock", MOCK_OK, OFF, True),
    ("mock", {"status": "stopped"}, OFF, True),
    ("mock", {"status": "STOPPED"}, OFF, False),
    ("mock", {"status": "stopped", "stopped": False}, OFF, False),
    ("mock", {"status": "stopped", "error": "boom"}, OFF, False),
    ("mock", ROBOT_OK, OFF, False),  # not the mock's shape
    ("mock", {}, OFF, False), ("mock", None, OFF, False),
    ("robot_server", ROBOT_OK, BRAIN_OK, True),
    ("robot_server", ROBOT_OK, {"adapter": "brian8200", "connected": True, "stopped": True}, True),
    ("robot_server", ROBOT_OK, {"adapter": "masterpi", "at_zone": "home", "holding": None, "stopped": True}, True),
    ("robot_server", ROBOT_OK, {"stopped": True}, False),  # no adapter: not the brain's robot state
    ("robot_server", ROBOT_OK, {"status": "stopped"}, False),  # the mock robot's shape, from the brain
    ("robot_server", ROBOT_OK, {"adapter": "mock", "status": "stopped"}, False),
    ("robot_server", ROBOT_OK, {"adapter": 7, "stopped": True}, False),
    ("robot_server", ROBOT_OK, {"adapter": "masterpi", "stopped": "yes"}, False),
    ("robot_server", ROBOT_OK, {"adapter": "brian8200", "connected": "yes", "stopped": True}, False),
    # When the brain cannot reach its robot, its state says "stopped" anyway, next to the failure.
    ("robot_server", ROBOT_OK, {"adapter": "brian8200", "connected": False, "error": "timed out", "stopped": True}, False),
    ("robot_server", ROBOT_OK, {"adapter": "masterpi", "connected": False, "stopped": True}, False),
    ("robot_server", ROBOT_OK, {"error": "brain unreachable"}, False),
    ("robot_server", ROBOT_OK, {}, False), ("robot_server", ROBOT_OK, None, False),
    ("robot_server", ROBOT_OK, {"adapter": "mock", "stopped": False}, False),
    ("robot_server", [], BRAIN_OK, False),
]


@pytest.mark.parametrize("contract,robot_reply,brain_reply,confirmed", STOP_REPLIES)
def test_only_an_explicit_reply_confirms_a_stop(monkeypatch, contract, robot_reply, brain_reply, confirmed):
    async def robot_stop():
        return robot_reply

    async def brain_stop():
        return brain_reply

    async def brain_clear():
        return {}

    with TestClient(main.app) as c:
        c.post("/stop/clear")
        with monkeypatch.context() as m:
            m.setattr(main.engine.robot, "stop", robot_stop)
            m.setattr(main.engine.robot, "STOP_REPLY", contract, raising=False)  # as if this adapter answered
            if brain_reply != OFF:
                m.setattr(main.brain, "enabled", lambda: True)
                m.setattr(main.brain, "stop", brain_stop)
                m.setattr(main.brain, "clear_stop", brain_clear)
            voice = c.post("/commands", json={"text": "Stop.", "source": "voice"})
            button = c.post("/stop")
            latched = main.engine.stopped
            c.post("/stop/clear")
    assert voice.status_code == 200 and voice.json()["stop_confirmed"] is confirmed and latched, voice.json()
    assert button.status_code == (200 if confirmed else 502), button.status_code


@pytest.mark.parametrize("failure", ["robot stop raises", "robot stop request failed", "robot says not stopped",
                                     "brain stop failed"])
def test_an_unconfirmed_stop_says_so_and_stays_latched(monkeypatch, failure):
    # Review round 5: the voice route answered "Stopping." even when the robot never confirmed the stop.
    async def robot_stop():
        if failure == "robot stop raises":
            raise ValueError("robot reply was not JSON")
        if failure == "robot stop request failed":
            return {"status": "stop_request_failed", "error": "connect timeout"}
        if failure == "robot says not stopped":
            return {"status": "not stopped"}
        return MOCK_OK  # the console robot here is the mock: only the brain fails

    async def brain_stop():
        return {"error": "brain unreachable"}

    async def brain_clear():
        return {}

    with TestClient(main.app) as c:
        c.post("/stop/clear")
        with monkeypatch.context() as m:
            m.setattr(main.engine.robot, "stop", robot_stop)
            if failure == "brain stop failed":
                m.setattr(main.brain, "enabled", lambda: True)
                m.setattr(main.brain, "stop", brain_stop)
                m.setattr(main.brain, "clear_stop", brain_clear)
            out = c.post("/commands", json={"text": "Stop.", "source": "voice"}).json()
            latched = main.engine.stopped
            said = [e["text"] for e in main.bus.log if e["type"] == "say"][-1]
            button = c.post("/stop")
            c.post("/stop/clear")
    assert out["kind"] == "stop" and out["stop_confirmed"] is False and latched, out
    assert "could not confirm" in out["reason"] and "Cut the motor power" in out["quip"], out
    assert said == out["quip"] and out["robot"], (said, out)
    assert button.status_code == 502, button.status_code


@pytest.mark.parametrize("status,body,confirmed", [(200, {"stopped": True, "latched": True}, True),
                                                   (500, {"detail": "driver crashed"}, False),
                                                   (200, {"stopped": False, "latched": False}, False)])
def test_the_http_robot_never_passes_an_error_reply_off_as_a_stop(status, body, confirmed):
    from server.adapters.robot import HttpRobot
    from server.core import stop_unconfirmed

    robot = HttpRobot("http://robot.test")
    robot.fast = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
    res = asyncio.run(robot.stop())
    assert (not stop_unconfirmed(res, robot)) is confirmed, res


def test_a_robot_adapter_without_a_reply_contract_gets_the_strictest_one():
    from server.core import stop_unconfirmed

    unknown = object()  # an adapter that does not say what its stop reply looks like
    assert stop_unconfirmed({"status": "stopped"}, unknown) and stop_unconfirmed({"stopped": True}, unknown)
    assert not stop_unconfirmed(ROBOT_OK, unknown)


def test_a_confirmed_stop_says_stopping():
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        out = c.post("/commands", json={"text": "Stop.", "source": "voice"}).json()
        assert out["stop_confirmed"] is True and out["reason"] == "" and out["quip"] == "Stopping.", out
        assert out["robot"]["status"] == "stopped" and c.post("/stop").status_code == 200
        c.post("/stop/clear")


class StalledDisk:
    """A ledger file whose every open hangs, like a stalled SD card, until released. With reads_stall, looking
    the file up (exists()) hangs too, and `reading` is set once a read is stuck."""

    def __init__(self, release: threading.Event, reads_stall: bool = False, stall_s: float = 3.0,
                 read_stall_s: float = 5.0):
        self.release, self.reads_stall, self.stall_s, self.read_stall_s = release, reads_stall, stall_s, read_stall_s
        self.reading, self.writing = threading.Event(), threading.Event()

    def open(self, *args, **kwargs):  # only writes open the file here: reads stop at exists()
        self.writing.set()
        self.release.wait(self.stall_s)
        raise OSError("the disk stalled")

    def exists(self):
        if self.reads_stall:
            self.reading.set()
            self.release.wait(self.read_stall_s)
        return False


def test_a_stuck_ledger_read_queues_no_more_reads(monkeypatch):
    # Review round 9: with the one reader thread stuck on a stalled disk, every /evidence lookup queued another
    # read behind it (the time budget gave up on the caller but left the queued work), so sustained lookups could
    # exhaust memory. Only one read is ever in flight now; while it is stuck, a lookup searches the records that
    # are not on disk yet and otherwise answers 503 at once, queuing nothing.
    release = threading.Event()
    disk = StalledDisk(release, reads_stall=True, read_stall_s=30)
    submitted = []
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        real_submit = main.ledger._reader.submit

        def counting_submit(*args, **kwargs):
            submitted.append(args[0])
            return real_submit(*args, **kwargs)

        try:
            with monkeypatch.context() as m:
                m.setattr(main.ledger, "path", disk)
                m.setattr(main.ledger, "read_timeout_s", 0.2, raising=False)
                m.setattr(main.ledger._reader, "submit", counting_submit)
                stuck = threading.Thread(target=lambda: c.get("/evidence/ev_stuck"))
                stuck.start()
                assert disk.reading.wait(5), "the first lookup never reached the disk"
                statuses, slowest = [], 0.0
                for i in range(50):
                    t0 = time.monotonic()
                    statuses.append(c.get(f"/evidence/ev_{i}").status_code)
                    slowest = max(slowest, time.monotonic() - t0)
                t0 = time.monotonic()
                stop = c.post("/stop")
                stop_s = time.monotonic() - t0
                queued = main.ledger._reader._work_queue.qsize()
                stuck.join(5)
        finally:
            release.set()
        c.post("/stop/clear")
    assert len(submitted) == 1 and queued == 0, (len(submitted), queued)  # one read in flight, nothing queued
    assert set(statuses) == {503} and slowest < 1.0, (set(statuses), slowest)  # a controlled error, at once
    assert stop.status_code == 200 and stop_s < 1.0, (stop.status_code, stop_s)


def _backlog(ledger) -> dict:
    """The ledger's records that are not on disk yet (the attribute was _unwritten before round 8)."""
    return ledger._pending if hasattr(ledger, "_pending") else ledger._unwritten


def test_a_long_ledger_stall_keeps_a_bounded_backlog_and_counts_the_drops(monkeypatch, caplog):
    # Review round 8: during a disk stall the ledger kept every record in memory, so sustained traffic could
    # exhaust memory and take the console down, software stop included. The backlog now has a cap: when it is
    # full, the oldest record that is not being written is dropped and counted.
    cap, burst = 50, 2000
    release = threading.Event()
    disk = StalledDisk(release, stall_s=60)
    caplog.set_level(logging.WARNING, logger="console.core")
    appended: list[str] = []
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        assert main.ledger.flush(10)
        dropped0 = getattr(main.ledger, "dropped", 0)
        real_append = main.ledger.append

        def counting_append(event, **rec):
            appended.append(real_append(event, **rec))
            return appended[-1]

        try:
            with monkeypatch.context() as m:
                m.setattr(main.ledger, "path", disk)
                m.setattr(main.ledger, "max_pending", cap, raising=False)
                m.setattr(main.ledger, "append", counting_append)
                first = main.ledger.append("first")
                assert disk.writing.wait(5), "the writer never reached the disk"  # it now hangs on `first`
                slowest = 0.0
                for _ in range(20):  # requests keep coming during the stall and are never held up
                    t0 = time.monotonic()
                    assert c.post("/commands", json={"text": "What do you see?", "source": "typed"}).status_code == 200
                    slowest = max(slowest, time.monotonic() - t0)
                for i in range(burst):
                    main.ledger.append("probe", i=i)
                newest = appended[-1]
                t0 = time.monotonic()
                stop = c.post("/stop")
                stop_s = time.monotonic() - t0
                backlog, n = dict(_backlog(main.ledger)), len(appended)
                dropped = getattr(main.ledger, "dropped", 0) - dropped0
                health = c.get("/health").json()
        finally:
            release.set()
        assert main.ledger.flush(10)
        c.post("/stop/clear")
    assert len(backlog) == cap, len(backlog)  # bounded at the cap, however long the stall lasts
    assert dropped == n - cap, (dropped, n)  # every record over the cap is counted
    assert first in backlog and newest in backlog  # the oldest go first; the one being written stays
    assert stop.status_code == 200 and stop_s < 1.0 and slowest < 1.0, (stop.status_code, stop_s, slowest)
    assert health.get("ledger_dropped") == dropped0 + dropped, health
    logs = [r.getMessage() for r in caplog.records if r.name == "console.core" and "dropp" in r.getMessage()]
    assert len(logs) == 2 and f"dropped {dropped} " in logs[-1], logs  # once as drops start, once with the count


def test_a_stalled_ledger_read_never_delays_a_stop(monkeypatch):
    # Review round 7: Ledger.find() read the file on the event loop, so an evidence lookup during a disk stall
    # blocked a concurrent stop. The read now happens on the ledger's reader thread, within a time budget.
    release = threading.Event()
    disk = StalledDisk(release, reads_stall=True)
    lookup = {}
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        with monkeypatch.context() as m:
            m.setattr(main.ledger, "path", disk)
            m.setattr(main.ledger, "read_timeout_s", 1.0, raising=False)

            def look():
                t0 = time.monotonic()
                lookup["status"] = c.get("/evidence/ev_not_there").status_code
                lookup["s"] = time.monotonic() - t0

            reader = threading.Thread(target=look)
            reader.start()
            assert disk.reading.wait(5), "the lookup never reached the disk"
            t0 = time.monotonic()
            r = c.post("/commands", json={"text": "Stop.", "source": "voice"})
            stop_s = time.monotonic() - t0
            latched = main.engine.stopped
            reader.join(10)
        release.set()
        assert main.ledger.flush(10)
        c.post("/stop/clear")
    assert r.status_code == 200 and r.json()["kind"] == "stop" and latched and stop_s < 1.0, stop_s
    assert lookup["status"] == 503 and lookup["s"] < 3.0, lookup  # gave up after its time budget: not a 404


def test_a_stalled_ledger_disk_never_delays_a_stop(monkeypatch):
    # Review round 5: ledger writes ran on the event loop, so a stalled disk blocked the stop response (the time
    # budget cannot interrupt a blocked loop) and every other request. Writes now happen on the ledger's thread.
    release = threading.Event()
    calls = []
    with TestClient(main.app) as c:
        c.post("/stop/clear")
        real_stop = main.engine.robot.stop

        async def robot_stop():
            calls.append("robot.stop")
            return await real_stop()

        try:
            with monkeypatch.context() as m:
                m.setattr(main.ledger, "path", StalledDisk(release))
                m.setattr(main.engine.robot, "stop", robot_stop)
                t0 = time.monotonic()
                r = c.post("/commands", json={"text": "Stop.", "source": "voice"})
                stop_s = time.monotonic() - t0
                t1 = time.monotonic()
                other = c.post("/commands", json={"text": "What do you see?", "source": "typed"})
                other_s = time.monotonic() - t1
                eid = main.ledger.append("probe")
                found = asyncio.run(main.ledger.find(eid))
                latched = main.engine.stopped
        finally:
            release.set()
        assert r.status_code == 200 and r.json()["kind"] == "stop" and stop_s < 2.0, stop_s
        assert calls == ["robot.stop"] and latched
        assert other.status_code == 200 and other_s < 2.0, other_s  # the event loop was never blocked
        assert found and found["kind"] == "probe"  # a record that is not on disk yet can still be found
        assert main.ledger.flush(10)
        c.post("/stop/clear")


def test_spoken_stop_does_not_wait_on_the_brain(monkeypatch):
    # With the brain bridge on, every command used to go to the brain first. A stop must not
    # depend on the brain answering (or understanding it); engine.stop() still tells the brain.
    told = []

    async def must_not_plan(text):
        raise AssertionError("a stop was sent to the brain as a command")

    async def brain_stop():
        told.append("stop")
        return BRAIN_OK

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
