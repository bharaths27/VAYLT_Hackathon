"""A spoken stop stops the robot. It is never planned, scheduled, taught, or refused as an unknown object.

Measured: whisper.cpp writes "Stop." and the brain answered "I don't know which object you mean."
Expected outcomes were written down before the fix.
"""
import pytest

from conftest import REQUEST

STOP = [
    "Stop.", "Stop!", "stop, stop", "STOP", "Stop now.", "Please stop.", "Halt.", "Emergency stop!",
    " Stop. ", "Stop, stop, stop.", "Stop it.", "E-stop!", "Whoa!", "Freeze.", "Abort!", "Emergency!",
    "Stop. Bring Sample B to the bench.", "Stop opening the gripper.",
    # Deliberate: a needless stop costs one clear_stop, a missed stop can hurt someone.
    "Don't stop the timer.", "When the timer ends, stop.", "Remember to stop at the bench.",
]


def episodes(engine):
    return engine.conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]


@pytest.mark.parametrize("text", STOP)
def test_spoken_stop_stops_the_robot(taught, text):
    before = episodes(taught)
    out = taught.command(text, "test")
    assert out["kind"] == "stopped" and out["stopped"] is True, out
    assert taught.robot.state()["stopped"]
    assert episodes(taught) == before
    assert taught.prospective.list() == []


def test_spoken_stop_blocks_a_pending_plan(taught):
    p = taught.propose(REQUEST)
    taught.command("Stop.", "test")
    r = taught.approve_and_run(p["operation_id"], p["digest"], "me")
    assert r["outcome"] == "aborted"
    assert not any(op == "pick_tagged_object" for op, _ in taught.robot.calls)


@pytest.mark.parametrize("text", ["Don't open the gripper.", "Open the gripper after calibration finishes."])
def test_negated_and_future_requests_still_refuse(taught, text):
    out = taught.command(text, "test")
    assert out["kind"] == "proposal" and out["status"] == "refused"
    assert not taught.robot.state()["stopped"]


def test_ordinary_request_still_proposes(taught):
    out = taught.command(REQUEST, "test")
    assert out["kind"] == "proposal" and out["status"] == "proposed"
    assert not taught.robot.state()["stopped"]
