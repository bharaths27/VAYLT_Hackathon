"""Carter's rule in the brain: "ambiguous or misheard command: we stop the task and ask user to please clarify."

A command that needs clarifying stops the plan that is running with the latching stop a stop word uses, and
says so ("I stopped. <reason> Please clarify."). With nothing running nothing is stopped ("<reason> Please
clarify."). Understood refusals and stop words keep their behaviour. needs_clarification on every proposal
result is what lets the console tell the two kinds of refusal apart without reading the words.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from conftest import REQUEST, ROOT
from engram.api import create_app
from engram.db import connect
from engram.engine import Engine, clarify_reply
from engram.planner import LlamaCppPlanner, PlanResult
from engram.robot.base import ActionResult
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


# ---- a run the brain stopped says so, so the console shows STOPPED, not "needs verification" ---------------


def test_a_run_stopped_by_a_stop_says_stopped(lab):
    oid, t, out = start_plan(lab)
    lab.command(UNCLEAR, "test")                                # Carter's rule stops it
    lab.robot.release.set()
    t.join(10)
    assert (out["run"]["outcome"], out["run"]["stopped"]) == ("aborted", True)


def test_a_run_that_finished_or_needs_a_person_is_not_stopped(engine):
    p = engine.command(REQUEST, "test")
    done = engine.approve_and_run(p["operation_id"], p["digest"], "tester")
    assert (done["outcome"], done["stopped"]) == ("success", False)
    engine.robot.reset()
    engine.robot.inject_fault("locate_tag", "unknown", "camera lost the tag")
    p = engine.command(REQUEST, "test")
    unknown = engine.approve_and_run(p["operation_id"], p["digest"], "tester")
    assert (unknown["outcome"], unknown["stopped"]) == ("aborted", False)   # a person must check: not a stop


# ---- a stop that interrupts a step (the only one, or the last) ends the run as stopped -----------------------

class StopsMidStep(MockRobot):
    """The step `hold_on` waits until the test lets it finish; if the robot was stopped meanwhile it comes back the
    way a real robot's does: ABORTED, which the robot adapters report as "unknown" (or "failed")."""

    def __init__(self, arena, hold_on, status):
        super().__init__(arena)
        self.hold_on, self.status = hold_on, status
        self.entered, self.release = threading.Event(), threading.Event()

    def execute(self, op, args):
        if op == self.hold_on and not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(10), "the test never let the step finish"
            if self.stopped:
                return ActionResult(self.status, "ABORTED E_ESTOP: stopped during the motion")
        return super().execute(op, args)


def run_held(engine, proposal):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("run", engine.approve_and_run(
        proposal["operation_id"], proposal["digest"], "tester")))
    t.start()
    assert engine.robot.entered.wait(5) and engine.running()
    return t, out


def lessons(engine):
    return engine.conn.execute("SELECT COUNT(*) FROM negative_memory").fetchone()[0]


@pytest.mark.parametrize("status", ["unknown", "failed"])
@pytest.mark.parametrize("which", ["the only step", "the final step"])
def test_a_stop_during_a_step_ends_the_run_as_stopped(arena, which, status):
    hold_on = "go_to_zone" if which == "the only step" else "speak"
    eng = Engine(connect(":memory:"), StopsMidStep(arena, hold_on, status), set(arena["zones"]))
    eng.load_arena(arena)
    eng.overhead_scan()
    eng.teach("SOP 004", (ROOT / "fixtures/lab/SOP_004.md").read_text())
    p = eng.propose_rest("home") if which == "the only step" else eng.command(REQUEST, "test")
    before = lessons(eng)
    t, out = run_held(eng, p)
    r = eng.command(UNCLEAR, "test")                         # Carter's rule stops it during that step
    eng.robot.release.set()
    t.join(10)
    assert r["stopped"] is True
    assert (out["run"]["outcome"], out["run"]["stopped"], out["run"]["failure"]) == ("aborted", True, "stop was pressed")
    assert lessons(eng) == before                            # a stopped step is no lesson about the step


# ---- every command result states its classification; the console reads it, never the words or a missing flag --


@pytest.mark.parametrize("text,kind,cls", [
    ("Stop.", "stopped", "understood"),
    ("Remember that the bench is wet.", "taught", "understood"),
    ("What did I say about the goggles?", "answer", "understood"),
    ("When calibration ends, bring Sample B to the bench", "scheduled", "understood"),
    (REQUEST, "proposal", "understood"),
    (UNDERSTOOD, "proposal", "understood"),
    ("When calibration ends, remember that the bench is wet", "proposal", "understood"),
    (UNCLEAR, "proposal", "unclear"),
    ("Open the claw later", "proposal", "unclear"),
    ("", "proposal", "misheard"),
], ids=["stop", "taught", "answer", "scheduled", "planned", "prohibited", "a rule that waits", "unknown object",
        "unparsed time", "nothing heard"])
def test_every_command_result_states_its_classification(lab, text, kind, cls):
    r = lab.command(text, "test")
    assert (r["kind"], r["classification"]) == (kind, cls), r
    if kind == "proposal":
        assert r["needs_clarification"] is (cls != "understood")


# ---- a planner states how it took every request; the engine fails closed on anything but "understood" ---------


class Says:
    """A planner that returns the result the test gives it."""

    def __init__(self, res):
        self.res = res

    def plan(self, request, bundle, memory):
        return self.res


GO = {"goal": "go", "steps": [{"op": "go_to_zone", "args": {"zone": "bench"}}], "evidence": [], "rules_used": []}


def test_a_planner_result_cannot_be_made_without_its_classification():
    with pytest.raises(TypeError):
        PlanResult(None, refused="I don't know which object you mean.")   # a refusal that does not classify itself
    with pytest.raises(TypeError):
        PlanResult(GO)


@pytest.mark.parametrize("stated", [None, "", "maybe", "Understood"], ids=["none", "empty", "another word", "capital"])
def test_a_planner_refusal_that_states_no_known_classification_is_unclear_and_stops_the_plan(lab, stated):
    oid, t, out = start_plan(lab)
    lab.planner = Says(PlanResult(None, classification=stated, refused="The planner could not resolve it."))
    r = lab.command("Bring the gizmo to the bench", "test")
    lab.robot.release.set()
    t.join(10)
    assert (r["status"], r["classification"], r["needs_clarification"], r["stopped"]) == ("refused", "unclear", True, True)
    assert r["reply"] == "I stopped. The planner could not resolve it. Please clarify."
    assert outcome(lab, oid) == "aborted"


@pytest.mark.parametrize("stated", ["unclear", "misheard", None], ids=["unclear", "misheard", "none"])
def test_a_plan_the_planner_does_not_state_understood_is_refused_and_stops_the_plan(lab, stated):
    oid, t, out = start_plan(lab)
    lab.planner = Says(PlanResult(GO, classification=stated))
    r = lab.command("Go over there", "test")
    lab.robot.release.set()
    t.join(10)
    assert (r["status"], r["classification"], r["stopped"]) == ("refused", stated or "unclear", True)
    assert "digest" not in r and "plan" not in r and lab.pending() == []      # never proposed, so never approvable
    assert r["reply"] == "I stopped. I'm not sure what you meant. Please clarify."


def test_a_refusal_the_planner_states_understood_while_a_plan_runs_does_not_stop_it(lab):
    oid, t, out = start_plan(lab)
    lab.planner = Says(PlanResult(None, classification="understood", refused="That cannot be done as said."))
    r = lab.command("Bring the gizmo to the bench", "test")
    lab.robot.release.set()
    t.join(10)
    assert (r["status"], r["classification"], r["needs_clarification"]) == ("refused", "understood", False)
    assert "stopped" not in r and out["run"]["outcome"] == "success"


# ---- the model planner: a plan that does not resolve to what memory knows goes to the rules, which say why -----


class Model:
    """A local stand-in for the llama.cpp server (no model runs): every completion is the plan the test sets."""

    def __init__(self):
        fake, self.plan = self, None

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                data = json.dumps({"content": json.dumps(fake.plan)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/completion"


@pytest.fixture
def model():
    m = Model()
    yield m
    m.server.shutdown()
    m.server.server_close()


def bring(obj):
    return {"goal": "bring it", "evidence": [], "steps": [
        {"op": "locate_tag", "args": {"object_id": obj}}, {"op": "pick_tagged_object", "args": {"object_id": obj}},
        {"op": "place_at_zone", "args": {"zone": "bench"}}]}


@pytest.mark.parametrize("reply", [bring("gizmo"), {"goal": "bring it", "steps": [], "evidence": []}, None],
                         ids=["names an object memory does not have", "plans nothing", "no model"])
def test_the_model_planner_s_refusal_while_a_plan_runs_stops_it_and_asks(lab, model, reply):
    oid, t, out = start_plan(lab)
    model.plan = reply
    lab.planner = LlamaCppPlanner(lab.zones, model.url if reply else "http://127.0.0.1:9/completion", timeout_s=0.5)
    r = lab.command("Bring the gizmo to the bench", "test")
    lab.robot.release.set()
    t.join(10)
    assert (r["status"], r["classification"], r["stopped"]) == ("refused", "unclear", True), r
    assert r["reply"] == "I stopped. I don't know which object you mean. Please clarify."
    assert outcome(lab, oid) == "aborted" and lab.robot.state()["stopped"] is True


def test_a_model_plan_naming_an_object_memory_does_not_have_goes_to_the_rules(lab, model):
    model.plan = bring("beaker_1")                              # the model did not resolve "the beaker"; the rules do
    lab.planner = LlamaCppPlanner(lab.zones, model.url, timeout_s=5)
    r = lab.command("Bring the beaker to the bench", "test")
    assert (r["status"], r["source"], r["classification"]) == ("proposed", "fallback", "understood"), r
    assert "model plan names an object memory does not have: 'beaker_1'" in r["planner_note"]


def test_a_model_plan_that_resolves_is_the_model_s(lab, model):
    model.plan = bring("beaker")
    lab.planner = LlamaCppPlanner(lab.zones, model.url, timeout_s=5)
    r = lab.command("Bring the beaker to the bench", "test")
    assert (r["status"], r["source"], r["classification"]) == ("proposed", "model", "understood"), r
