"""Recall, heard memory and the ENGRAM package wired into the engine and API."""

import hashlib

from fastapi.testclient import TestClient

from conftest import REQUEST, ROOT
from engram.api import create_app
from engram.recall import is_memory_question
from perception.voice import describe_reply


def _episodes(engine):
    return engine.conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]


def _tasks(engine):
    return engine.conn.execute("SELECT COUNT(*) FROM prospective_task").fetchone()[0]


def test_question_about_the_past_is_answered_not_scheduled_or_planned(taught):
    p = taught.propose(REQUEST)
    taught.approve_and_run(p["operation_id"], p["digest"], "me")
    episodes, tasks, calls = _episodes(taught), _tasks(taught), len(taught.robot.calls)
    r = taught.command("After the experiment finished, what happened to Sample B?")
    assert r["kind"] == "answer" and r["answers"][0]["text"].endswith("-> success")
    assert (_episodes(taught), _tasks(taught), len(taught.robot.calls)) == (episodes, tasks, calls)


def test_polite_requests_and_plain_commands_still_plan(engine):
    assert not is_memory_question("Could you bring the beaker to the bench?")
    assert not is_memory_question("Can you hand me the beaker?")
    assert not is_memory_question("Bring the beaker to the bench.")
    assert engine.command("Could you bring the beaker to the bench?")["kind"] == "proposal"


def test_real_future_instruction_is_still_scheduled(engine):
    r = engine.command("When the centrifuge stops, carry the beaker to the bench.")
    assert r["kind"] == "scheduled" and _tasks(engine) == 1


def test_unknown_is_said_not_guessed(engine):
    r = engine.command("What happened with the spectrometer?")
    assert r["kind"] == "answer" and r["answers"] == [] and r["reply"] == "I have no record of that."


def test_recall_and_package_over_http(engine):
    c = TestClient(create_app(engine))
    c.post("/commands", json={"text": "Remember that Sample B needs goggles", "who": "voice"})
    r = c.post("/memory/recall", json={"question": "What did I say about Sample B?"}).json()
    assert r["kinds"] == ["heard"] and "goggles" in r["answers"][0]["text"]
    assert c.post("/memory/recall", json={"question": "x", "kinds": ["telepathic"]}).status_code == 422
    pkg = c.get("/memory/package").json()
    assert pkg["loaded"] and pkg["sha256"] == hashlib.sha256((ROOT / "engram/package/ENGRAM.md").read_bytes()).hexdigest()


def test_practice_verifier_reads_an_answer():
    from engram.practice import behavior
    assert behavior({"kind": "answer", "answers": [], "missing": ["x"], "reply": "I have no record of that."}) == "answer"


def test_voice_client_reads_an_answer():
    screen, spoken = describe_reply({"kind": "answer", "answers": [{"kind": "episodic", "text": "x -> success"}],
                                     "missing": [], "reply": "x -> success"})
    assert screen.startswith("FROM MEMORY") and spoken == "x -> success"
