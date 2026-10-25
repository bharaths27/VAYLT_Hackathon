"""Spoken-command safety tables, from a real end-to-end measurement with whisper.cpp output.

  STOP     whisper writes "Stop." (capital, period). The exact-match stop check missed it,
           so a spoken stop never stopped the robot. Stop must always win and never be planned.
  NEGATED  "Don't open the claw" was planned as an OPEN.
  FUTURE   "Open the claw after calibration finishes" was planned as an immediate open.

Expected kinds were written down before the fix; every table had failing rows on the old parser.
"""
import asyncio

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
]

NEGATED = [
    "Don't open the claw", "Don't open the claw.", "Do not open the claw.", "Never open the claw.",
    f"Don{CURLY}t open the claw.", "dont open the claw", "You shouldn't open the claw.",
    "Not now, open the claw.", "No, open the claw.", "No, don't close the gripper.",
    "Cancel, open the claw.", "Cancel that.", "Wait, don't open the claw.", "Wait, open the claw.",
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
    ("Remember that the vial must never leave zone B.", "rule", None),
    ("From now on, do not drive into zone A.", "rule", None),
]


@pytest.mark.parametrize("text", STOP)
def test_spoken_stop_always_wins(text):
    r = parse(text, BUNDLE)
    assert (r.kind, r.plan) == ("stop", None), (text, r.kind, r.trace)


@pytest.mark.parametrize("text", NEGATED)
def test_negated_command_is_refused_not_planned(text):
    r = parse(text, BUNDLE)
    assert r.kind in ("abstain", "clarify") and r.plan is None, (text, r.kind, r.trace)
    assert not r.data.get("llm_eligible"), "the LLM fallback could plan the negated action"


@pytest.mark.parametrize("text", FUTURE)
def test_future_condition_is_not_run_now(text):
    r = parse(text, BUNDLE)
    assert (r.kind, r.plan) == ("clarify", None), (text, r.kind, r.trace)


@pytest.mark.parametrize("text,kind,first_op", STILL_WORKS)
def test_ordinary_commands_still_parse(text, kind, first_op):
    r = parse(text, BUNDLE)
    assert r.kind == kind, (text, r.kind, r.reason, r.trace)
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
