"""Carter's rule (16:3xZ): "ambiguous or misheard command: we stop the task and ask user to please clarify."

Every way a command reaches the console: typed and voice text (/commands), /stt, and the brain bridge. A task
that is running is stopped with the latching stop a stop word uses ("I stopped. <reason> Please clarify.");
with nothing running nothing is stopped ("<reason> Please clarify."). Clear refusals and stop words keep their
behaviour.
"""
import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server import brain, main

AMBIGUOUS = "bring it here"                     # the parser asks: "'it' is ambiguous. ..."


def wait(c, oid, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        o = next(x for x in c.get("/operations").json() if x["id"] == oid)
        if o["status"] not in ("APPROVED", "EXECUTING", "PENDING_APPROVAL"):
            return o
        time.sleep(0.05)
    raise AssertionError("timeout")


class HeldRobot:
    """The robot's execute keeps a step under way until the robot is stopped (then it reports the step stopped,
    as the real robot does) or the test lets it finish."""

    def __init__(self, robot):
        self.robot, self.real = robot, robot.execute
        self.entered, self.finish = threading.Event(), threading.Event()

    async def __call__(self, op, args, operation_id):
        self.entered.set()
        while not self.finish.is_set():
            if self.robot.estop:
                return {"operation_id": operation_id, "status": "stopped", "reason": "E-STOP latched"}
            await asyncio.sleep(0.01)
        return await self.real(op, args, operation_id)


@pytest.fixture
def c():
    with TestClient(main.app) as client:
        client.post("/stop/clear")
        main.engine.ops.clear()
        yield client
        client.post("/stop/clear")


@pytest.fixture
def held(monkeypatch):
    h = HeldRobot(main.engine.robot)
    monkeypatch.setattr(main.engine.robot, "execute", h)
    yield h
    h.finish.set()


def start_op(c, held, text="open the claw"):
    """Plan, approve, and return once its step is under way: a task is running."""
    j = c.post("/commands", json={"text": text}).json()
    assert j["kind"] == "plan", j
    op = j["operation"]
    assert c.post(f"/operations/{op['id']}/approve", json={"digest": op["digest"]}).status_code == 200
    assert held.entered.wait(5)
    assert next(x for x in c.get("/operations").json() if x["id"] == op["id"])["status"] == "EXECUTING"
    return op


def test_a_running_task_and_an_ambiguous_command_stops_it_and_asks(c, held):
    op = start_op(c, held)
    j = c.post("/commands", json={"text": AMBIGUOUS}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True and j["stop_confirmed"] is True
    assert j["reply"].startswith("I stopped.") and j["reply"].endswith("Please clarify.")
    assert j["reply"] == "I stopped. 'it' is ambiguous. Name the object or its tag number. Please clarify."
    assert main.engine.stopped                                  # latched: Clear Stop is needed to move again
    assert wait(c, op["id"])["status"] == "STOPPED"
    assert c.get("/status").json()["stopped"] is True           # and nothing resumed it


def test_idle_and_an_ambiguous_command_asks_without_stopping(c):
    j = c.post("/commands", json={"text": AMBIGUOUS}).json()
    assert j["kind"] == "clarify" and j["stopped"] is False and "robot" not in j
    assert j["reply"] == "'it' is ambiguous. Name the object or its tag number. Please clarify."
    assert not main.engine.stopped                              # no latch for a cough


@pytest.mark.parametrize("text", ["juggle", "sing a song"])
def test_an_unparsed_command_is_unclear_too(c, held, text):
    op = start_op(c, held)
    j = c.post("/commands", json={"text": text}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True
    assert j["reply"] == "I stopped. I don't have a safe primitive for that yet. Please clarify."
    assert wait(c, op["id"])["status"] == "STOPPED"


@pytest.mark.parametrize("auto", ["1", "0"])
@pytest.mark.parametrize("transcript", ["", "[BLANK_AUDIO]"], ids=["empty", "noise only"])
def test_a_running_task_and_a_misheard_transcript_stops_it_and_asks(c, held, monkeypatch, auto, transcript):
    async def fake_transcribe(data, suffix, **kw):
        return transcript, "ok (whisper server)"            # transcribed, and nothing in it

    monkeypatch.setattr(main.voice, "transcribe", fake_transcribe)
    op = start_op(c, held)
    r = c.post("/stt", files={"audio": ("speech.m4a", bytes(2000), "audio/mp4")}, data={"auto": auto})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["misheard"] is True
    if auto == "1":
        j = r.json()["result"]
    else:                                                       # the iPad runs the transcript, as voice.js does
        assert "result" not in r.json()
        j = c.post("/commands", json={"text": r.json()["text"], "source": "voice"}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True
    assert j["reply"] == "I stopped. I didn't catch that. Please clarify."
    assert wait(c, op["id"])["status"] == "STOPPED"


def test_a_misheard_transcript_only_still_broadcasts_nothing(monkeypatch):
    """voice.js does not track /stt with auto=0 (see test_stt_transcript_only.py): it must stay silent."""
    events = []

    async def fake_transcribe(data, suffix, **kw):
        return "", "ok (whisper server)"

    async def spy_emit(type_, **data):
        events.append(type_)
        return {"type": type_, **data}

    monkeypatch.setattr(main.voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(main.bus, "emit", spy_emit)
    # As test_stt_transcript_only.py does: without entering the client, so no background loop emits.
    r = TestClient(main.app).post("/stt", files={"audio": ("speech.m4a", bytes(2000), "audio/mp4")}, data={"auto": "0"})
    assert r.json() == {"ok": True, "text": "", "misheard": True} and events == []


def test_idle_and_a_misheard_transcript_asks_without_stopping(c, monkeypatch):
    async def fake_transcribe(data, suffix, **kw):
        return "", "ok (whisper server)"

    monkeypatch.setattr(main.voice, "transcribe", fake_transcribe)
    j = c.post("/stt", files={"audio": ("speech.m4a", bytes(2000), "audio/mp4")}, data={"auto": "1"}).json()
    assert j["result"]["reply"] == "I didn't catch that. Please clarify." and j["result"]["stopped"] is False
    assert not main.engine.stopped


def test_a_failed_transcription_is_still_a_503_and_stops_nothing(c, held, monkeypatch):
    async def fake_transcribe(data, suffix, **kw):
        return None, "whisper failed"                       # not misheard: not heard at all

    monkeypatch.setattr(main.voice, "transcribe", fake_transcribe)
    op = start_op(c, held)
    r = c.post("/stt", files={"audio": ("speech.m4a", bytes(2000), "audio/mp4")}, data={"auto": "1"})
    assert r.status_code == 503 and not main.engine.stopped
    held.finish.set()
    assert wait(c, op["id"])["status"] != "STOPPED"


@pytest.mark.parametrize("text", ["drive at full speed", "don't open the claw", "drive forward 2 meters"],
                         ids=["unsafe", "negated", "over a limit"])
def test_a_running_task_and_a_clear_refusal_does_not_stop(c, held, text):
    op = start_op(c, held)
    j = c.post("/commands", json={"text": text}).json()
    assert j["kind"] == "abstain" and "stopped" not in j and "reply" not in j
    assert not main.engine.stopped
    held.finish.set()
    assert wait(c, op["id"])["status"] != "STOPPED"


def test_a_stop_word_is_unchanged(c, held):
    op = start_op(c, held)
    j = c.post("/commands", json={"text": "Stop."}).json()
    assert j["kind"] == "stop" and j["stop_confirmed"] is True and "reply" not in j
    assert main.engine.stopped and wait(c, op["id"])["status"] == "STOPPED"


def test_an_unconfirmed_stop_says_so(c, held, monkeypatch):
    start_op(c, held)

    async def failing_stop():
        raise RuntimeError("robot unreachable")

    monkeypatch.setattr(main.engine.robot, "stop", failing_stop)
    j = c.post("/commands", json={"text": AMBIGUOUS}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True and j["stop_confirmed"] is False
    assert j["reply"].startswith("I could not confirm the robot stopped. Cut the motor power.")
    assert j["reply"].endswith("Please clarify.") and main.engine.stopped    # the latch holds all the same


# ---- the brain bridge ------------------------------------------------------------------------------------


class FakeBrain:
    def __init__(self, reply, running=(), running_fails=False):
        self.reply, self.running, self.running_fails = reply, list(running), running_fails
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/commands":
            return httpx.Response(200, json=self.reply)
        if request.url.path == "/operations/running":
            return httpx.Response(500, json={}) if self.running_fails else httpx.Response(200, json=self.running)
        if request.url.path == "/stop":
            return httpx.Response(200, json={"adapter": "mock", "stopped": True, "connected": True})
        if request.url.path == "/robot/clear_stop":
            return httpx.Response(200, json={"adapter": "mock", "stopped": False})
        return httpx.Response(200, json=[])


@pytest.fixture
def on_brain(monkeypatch):
    def use(fake):
        monkeypatch.setattr(brain, "URL", "http://brain.test")
        monkeypatch.setattr(brain, "_c", httpx.AsyncClient(timeout=5.0, transport=httpx.MockTransport(fake)))
        return fake
    return use


UNKNOWN = {"kind": "proposal", "status": "refused", "reason": "I don't know which object you mean.",
           "needs_clarification": True, "stopped": False, "operation_id": "ep_1"}
PROHIBITED = {"kind": "proposal", "status": "refused", "needs_clarification": False, "operation_id": "ep_2",
              "reason": "I will not do that: 'throw' is not something this robot is allowed to do."}


def test_brain_unclear_and_it_stopped_its_own_plan(c, on_brain):
    fake = on_brain(FakeBrain({**UNKNOWN, "stopped": True}))
    j = c.post("/commands", json={"text": "bring the thing"}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True and main.engine.stopped
    assert j["reply"] == "I stopped. I don't know which object you mean. Please clarify."
    assert "/stop" in fake.calls                                # the console's latch stops the brain as well


def test_brain_unclear_with_nothing_running_asks_without_stopping(c, on_brain):
    fake = on_brain(FakeBrain(UNKNOWN))
    j = c.post("/commands", json={"text": "bring the thing"}).json()
    assert j["kind"] == "clarify" and j["stopped"] is False and not main.engine.stopped
    assert j["reply"] == "I don't know which object you mean. Please clarify."
    assert "/operations/running" in fake.calls and "/stop" not in fake.calls


@pytest.mark.parametrize("brain_state", ["a plan it is running", "cannot say"])
def test_brain_unclear_while_the_brain_runs_a_plan_stops(c, on_brain, brain_state):
    on_brain(FakeBrain(UNKNOWN, running=[{"operation_id": "ep_9"}], running_fails=brain_state == "cannot say"))
    j = c.post("/commands", json={"text": "bring the thing"}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True and main.engine.stopped


def test_brain_understood_refusal_is_unchanged(c, on_brain):
    fake = on_brain(FakeBrain(PROHIBITED, running=[{"operation_id": "ep_9"}]))
    j = c.post("/commands", json={"text": "throw the beaker"}).json()
    assert j["kind"] == "abstain" and j["reason"] == PROHIBITED["reason"] and "stopped" not in j
    assert not main.engine.stopped and "/stop" not in fake.calls


def test_brain_that_does_not_say_is_treated_as_unclear(c, on_brain):
    old = {k: v for k, v in UNKNOWN.items() if k not in ("needs_clarification", "stopped")}
    on_brain(FakeBrain(old, running=[{"operation_id": "ep_9"}]))
    j = c.post("/commands", json={"text": "bring the thing"}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True


def test_brain_path_misheard_voice_stops_the_brains_plan(c, on_brain):
    fake = on_brain(FakeBrain(UNKNOWN, running=[{"operation_id": "ep_9"}]))
    j = c.post("/commands", json={"text": "[BLANK_AUDIO]", "source": "voice"}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True and main.engine.stopped
    assert "/commands" not in fake.calls and "/stop" in fake.calls   # answered here, and the brain is stopped


@pytest.mark.parametrize("text,reply", [
    ("open the claw and close the gripper", "One thing at a time: which first? Please clarify."),
    ("open the claw after calibration", "'after' sounds like later, a condition or another step. Please clarify."),
    ("open the claw halfway", "I don't understand 'halfway'. Please clarify."),
    ("In 5 seconds, juggle.", "I don't have a safe primitive for that yet. Please clarify."),
], ids=["more than one thing", "a deferral", "a word it does not use", "an unparsed task in a delay"])
def test_the_spoken_reason_is_short_and_the_screen_keeps_the_whole_one(c, text, reply):
    j = c.post("/commands", json={"text": text}).json()
    assert j["kind"] == "clarify" and j["reply"] == reply and len(j["reason"]) > len(reply) - len(" Please clarify.")


# ---- only whisper's own markers are noise: words in brackets are speech -----------------------------------


def test_a_parenthesized_refusal_while_a_task_runs_stays_a_refusal(c, held):
    op = start_op(c, held)
    j = c.post("/commands", json={"text": "(don't open the claw)", "source": "voice"}).json()
    assert j["kind"] == "abstain" and "don't" in j["reason"] and "stopped" not in j
    assert not main.engine.stopped
    held.finish.set()
    assert wait(c, op["id"])["status"] != "STOPPED"


@pytest.mark.parametrize("text,noise", [
    ("[BLANK_AUDIO]", True), ("[MUSIC]", True), ("(music)", True), ("[NOISE]", True), ("(silence)", True),
    ("[inaudible]", True), ("(soft music playing)", True), ("(wind blowing) [BLANK_AUDIO]", True), (" ... ", True),
    ("(don't open the claw)", False), ("(no, I mean close it)", False), ("[open the claw]", False),
    ("Open the claw. [MUSIC]", False), ("(music) open the claw", False),
], ids=lambda v: str(v))
def test_no_speech_drops_only_recognized_markers(text, noise):
    assert main.voice.no_speech(text) is noise


# ---- understood but impossible as said ("I can only schedule a robot action"): a refusal, never a stop -----


@pytest.fixture
def said(monkeypatch):
    out, real = [], main.bus.emit

    async def emit(type_, **data):
        if type_ == "say":
            out.append(data.get("text"))
        return await real(type_, **data)

    monkeypatch.setattr(main.bus, "emit", emit)
    return out


@pytest.mark.parametrize("text", ["In 5 minutes, look around.", "In 5 minutes, remember that the bench is wet.",
                                  "In 5 minutes, in 2 minutes, open the claw."],
                         ids=["not a robot action", "a rule", "a delay of its own"])
def test_an_understood_request_that_cannot_be_scheduled_is_refused_and_stops_nothing(c, held, said, text):
    op = start_op(c, held)
    j = c.post("/commands", json={"text": text}).json()
    assert j["kind"] == "clarify" and "stopped" not in j and "reply" not in j
    assert said[-1] == "I can only schedule a robot action." and not main.engine.stopped
    held.finish.set()
    assert wait(c, op["id"])["status"] != "STOPPED"


@pytest.mark.parametrize("text", ["juggle", "In 5 seconds, juggle.", "In 5 seconds, bring it here."],
                         ids=["unparsed", "an unparsed task in a delay", "an ambiguous task in a delay"])
def test_an_unparsed_or_ambiguous_task_still_stops_and_asks(c, held, text):
    op = start_op(c, held)
    j = c.post("/commands", json={"text": text}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True and j["reply"].endswith("Please clarify.")
    assert wait(c, op["id"])["status"] == "STOPPED"


def test_the_parser_marks_what_it_understood():
    from server.parser import parse
    assert parse("In 5 minutes, look around.", {}).understood is True
    assert parse("In 5 seconds, juggle.", {}).understood is False
    assert parse("bring it here", {}).understood is False


class RunningBrain(FakeBrain):
    """A brain whose plan runs until it is stopped; then it reports the run aborted by the stop."""

    PLAN = {"kind": "proposal", "status": "proposed", "operation_id": "ep_run", "digest": "d" * 64, "source": "rules",
            "plan": {"goal": "Bring Sample B to the bench", "steps": [{"op": "locate_tag", "args": {}},
                                                                      {"op": "pick_tagged_object", "args": {}}]},
            "check": {"ok": True, "physical": True}, "needs_clarification": False}

    def __init__(self):
        super().__init__(self.PLAN)
        self.stop_seen, self.approved = threading.Event(), threading.Event()

    async def __call__(self, request):
        if request.url.path == "/stop":
            self.stop_seen.set()
        if request.url.path.endswith("/approve"):
            self.approved.set()
            while not self.stop_seen.is_set():
                await asyncio.sleep(0.01)
            return httpx.Response(200, json={"operation_id": "ep_run", "outcome": "aborted", "stopped": True,
                                             "failure": "stop was pressed", "steps": [{"op": "locate_tag", "status": "done"}]})
        return super().__call__(request)


def test_a_brain_plan_stopped_by_the_rule_shows_stopped(c, on_brain):
    fake = on_brain(RunningBrain())
    op = c.post("/commands", json={"text": "bring sample b to the bench"}).json()["operation"]
    assert c.post(f"/operations/{op['id']}/approve", json={"digest": op["digest"]}).status_code == 200
    assert fake.approved.wait(5)                                # the brain is running the plan
    fake.reply = {**UNKNOWN, "stopped": True}                   # an unclear command: the brain stops its own plan
    j = c.post("/commands", json={"text": "bring the thing"}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True
    done = wait(c, op["id"])
    assert done["status"] == "STOPPED" and [s["status"] for s in done["steps"]] == ["succeeded", "skipped"]


class SlowBrain(FakeBrain):
    """A brain that answers "what is running?" only after the console has stopped waiting."""

    async def __call__(self, request):
        if request.url.path == "/operations/running":
            await asyncio.sleep(1.0)
            return httpx.Response(200, json=[])              # "nothing", but too late to count
        return super().__call__(request)


def test_a_brain_too_slow_to_say_what_runs_counts_as_running(c, on_brain, monkeypatch):
    monkeypatch.setattr(main.config, "BRAIN_QUICK_TIMEOUT_S", 0.2)
    on_brain(SlowBrain(UNKNOWN))
    t0 = time.monotonic()
    j = c.post("/commands", json={"text": "bring the thing"}).json()
    assert j["kind"] == "clarify" and j["stopped"] is True and main.engine.stopped
    assert time.monotonic() - t0 < 0.9                       # it stopped without waiting for the slow answer


def test_a_brain_plan_aborted_for_an_unknown_outcome_still_needs_verification(c, on_brain):
    class UnknownBrain(RunningBrain):
        async def __call__(self, request):
            if request.url.path.endswith("/approve"):           # aborted, but not by a stop: a person must check
                return httpx.Response(200, json={"operation_id": "ep_run", "outcome": "aborted", "stopped": False,
                                                 "failure": "pick_tagged_object outcome unknown", "steps": []})
            return await super().__call__(request)

    on_brain(UnknownBrain())
    op = c.post("/commands", json={"text": "bring sample b to the bench"}).json()["operation"]
    assert c.post(f"/operations/{op['id']}/approve", json={"digest": op["digest"]}).status_code == 200
    assert wait(c, op["id"])["status"] == "NEEDS_VERIFICATION"
