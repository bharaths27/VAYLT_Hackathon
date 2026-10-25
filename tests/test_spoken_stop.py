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
    # "Wait", "Pause", "Hold on", "Hold it" and "Hang on" stop too, by the same rule.
    "Wait!", "Wait, don't open the gripper.", "Pause.", "Hold on.", "Hold  on, bring Sample B to the bench.",
    "Hold it!", "Hang on.", "Freeze!",
    "Remember to wait for the student.",  # deliberate: teach it with POST /memory/sources instead
    # A stop inside "When X, ..." still stops at once: it is never scheduled, and never refused as a rule.
    "When calibration ends, hold on.", "When calibration ends, remember to stop at the bench.",
]

# Phrases, not words: holding something is not a stop.
NOT_STOP = ["Hold the beaker", "Is the gripper holding Sample B?"]


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


@pytest.mark.parametrize("text", NOT_STOP)
def test_holding_something_is_not_a_stop(taught, text):
    assert taught.command(text, "test")["kind"] != "stopped"
    assert not taught.robot.state()["stopped"]


def test_stop_words_are_the_same_on_the_console():
    # One word list, two places: the console parser and this engine must stop on exactly the same words.
    from console.server.parser import STOP_WORDS as console_stop_words
    from engram.engine import STOP_WORDS

    assert STOP_WORDS.pattern == console_stop_words.pattern


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
