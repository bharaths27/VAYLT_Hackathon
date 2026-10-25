"""Carter's rule in the brain: "ambiguous or misheard command: we stop the task and ask user to please clarify."

A command that needs clarifying stops the plan that is running with the latching stop a stop word uses, and
says so ("I stopped. <reason> Please clarify."). With nothing running nothing is stopped ("<reason> Please
clarify."). Understood refusals and stop words keep their behaviour. needs_clarification on every proposal
result is what lets the console tell the two kinds of refusal apart without reading the words.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from conftest import REQUEST, ROOT
from engram.api import create_app
from engram.db import connect
from engram.engine import Engine, clarify_reply
from engram.robot.mock import MockRobot

UNCLEAR = "Bring the thing over here"          # no object it knows: "I don't know which object you mean."
UNDERSTOOD = "Throw the beaker"                # a prohibited action: understood, and refused


class HeldRobot(MockRobot):
    """The plan's first motion step does its work, then waits until the test lets it finish, so the plan is
    running (episode outcome 'running') for as long as the test needs."""

    def __init__(self, arena):
        super().__init__(arena)
        self.entered, self.release = threading.Event(), threading.Event()

    def execute(self, op, args):
        res = super().execute(op, args)
        if op != "observe" and not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(10), "the test never let the step finish"
        return res


@pytest.fixture
def lab(arena):
    eng = Engine(connect(":memory:"), HeldRobot(arena), set(arena["zones"]))
    eng.load_arena(arena)
    eng.overhead_scan()
    eng.teach("SOP 004", (ROOT / "fixtures/lab/SOP_004.md").read_text())
    return eng


def start_plan(engine):
    """Propose REQUEST, approve it on another thread, and return once its first step is under way."""
    p = engine.command(REQUEST, "test")
    assert p["status"] == "proposed", p
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("run", engine.approve_and_run(
        p["operation_id"], p["digest"], "tester")))
    t.start()
    assert engine.robot.entered.wait(5)
    assert [r["operation_id"] for r in engine.running()] == [p["operation_id"]]
    return p["operation_id"], t, out


def outcome(engine, operation_id):
    return engine.conn.execute("SELECT outcome FROM episode WHERE id = ?", (operation_id,)).fetchone()[0]


def test_an_unclear_command_while_a_plan_runs_stops_it_and_asks(lab):
    oid, t, out = start_plan(lab)
    r = lab.command(UNCLEAR, "test")
    lab.robot.release.set()
    t.join(10)
    assert r["needs_clarification"] is True and r["stopped"] is True
    assert r["reply"] == "I stopped. I don't know which object you mean. Please clarify."
    assert lab.robot.state()["stopped"] is True                 # latched: clear_stop is needed to move again
    assert (out["run"]["outcome"], out["run"]["failure"]) == ("aborted", "stop was pressed")
    assert outcome(lab, oid) == "aborted" and lab.running() == []
    assert len(out["run"]["steps"]) == 1                        # nothing after the step that was under way


def test_an_unclear_command_with_nothing_running_stops_nothing(lab):
    r = lab.command(UNCLEAR, "test")
    assert r["needs_clarification"] is True and r["stopped"] is False and "robot" not in r
    assert r["reply"] == "I don't know which object you mean. Please clarify."
    assert lab.robot.state()["stopped"] is False                # no latch for a cough
    assert not [e for e in lab.events() if e["op"] == "stop"]


def test_a_misheard_empty_command_while_a_plan_runs_stops_it_and_asks(lab):
    oid, t, out = start_plan(lab)
    r = lab.command("   ", "voice")
    lab.robot.release.set()
    t.join(10)
    assert r["status"] == "refused" and r["needs_clarification"] is True and r["stopped"] is True
    assert r["reply"] == "I stopped. I didn't catch anything. Please clarify."
    assert outcome(lab, oid) == "aborted"


def test_a_clear_refusal_while_a_plan_runs_does_not_stop_it(lab):
    oid, t, out = start_plan(lab)
    r = lab.command(UNDERSTOOD, "test")
    lab.robot.release.set()
    t.join(10)
    assert r["status"] == "refused" and r["needs_clarification"] is False
    assert "stopped" not in r and "reply" not in r              # its reply is unchanged: the reason itself
    assert out["run"]["outcome"] == "success" and outcome(lab, oid) == "success"
    assert lab.robot.state()["stopped"] is False


def test_a_stop_word_is_unchanged(lab):
    oid, t, out = start_plan(lab)
    r = lab.command("Stop.", "test")
    lab.robot.release.set()
    t.join(10)
    assert r["kind"] == "stopped" and r["stopped"] is True and "needs_clarification" not in r
    assert outcome(lab, oid) == "aborted"


@pytest.mark.parametrize("text,unclear", [
    (UNCLEAR, True),                                            # no object it knows
    ("Open the claw later", True),                              # a time that did not parse as a future task
    ("When calibration ends, remember that the bench is wet", False),  # understood: a rule cannot wait for an event
    ("", True),                                                 # nothing heard
    (UNDERSTOOD, False),                                        # an action the robot may not do
    ("Don't move Sample B", False),                             # "don't": understood
    (REQUEST, False),                                           # planned
], ids=["unknown object", "unparsed time", "deferred rule", "empty", "prohibited", "negated", "planned"])
def test_needs_clarification_tells_unclear_from_understood(lab, text, unclear):
    r = lab.command(text, "test")
    assert r["kind"] == "proposal" and r["needs_clarification"] is unclear, r


def test_the_reply_always_ends_please_clarify():
    assert clarify_reply("I don't know which object you mean", False) == \
        "I don't know which object you mean. Please clarify."
    assert clarify_reply("Which first?", True) == "I stopped. Which first? Please clarify."


def test_the_brain_api_lists_running_plans_and_takes_an_empty_command(lab):
    client = TestClient(create_app(lab))
    assert client.get("/operations/running").json() == []
    oid, t, out = start_plan(lab)
    try:
        assert [r["operation_id"] for r in client.get("/operations/running").json()] == [oid]
        r = client.post("/commands", json={"text": "", "who": "voice"}).json()
    finally:
        lab.robot.release.set()
        t.join(10)
    assert r["needs_clarification"] is True and r["stopped"] is True
    assert r["reply"] == "I stopped. I didn't catch anything. Please clarify."


def test_a_rule_that_cannot_wait_for_an_event_while_a_plan_runs_is_refused_and_stops_nothing(lab):
    oid, t, out = start_plan(lab)
    r = lab.command("When calibration ends, remember that the bench is wet", "test")
    lab.robot.release.set()
    t.join(10)
    assert r["status"] == "refused" and r["needs_clarification"] is False and "stopped" not in r
    assert r["reason"].startswith("I can only schedule a robot action")
    assert out["run"]["outcome"] == "success" and lab.robot.state()["stopped"] is False
