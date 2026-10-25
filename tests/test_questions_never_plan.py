"""A question is answered from memory and never becomes a plan (10-25 recall benchmark: 8 of 24
questions became motion proposals, e.g. "Can the 500 mL beaker go in the drying oven?")."""

import pytest

from engram.recall import asks_question


def _counts(engine):
    episodes = engine.conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
    tasks = engine.conn.execute("SELECT COUNT(*) FROM prospective_task").fetchone()[0]
    return episodes, tasks, len(engine.robot.calls)


@pytest.mark.parametrize("text", [
    "Can the 500 mL beaker go in the drying oven?",
    "Should I bring the beaker to the bench?",
    "Is it safe to carry the beaker to the bench?",
    "Could we bring the beaker to the bench?",
    "Will the beaker fit on the bench?",
    "Where is the beaker",
    "Is the beaker on the bench",
    "which bench is the beaker on",
    "Do you know where the beaker is",
    "Have you seen the beaker on the bench",
    "do we have the beaker on the bench",
])
def test_a_question_is_answered_never_planned_scheduled_or_moved(engine, text):
    before = _counts(engine)
    r = engine.command(text)
    assert r["kind"] == "answer" and "reply" in r
    assert _counts(engine) == before


@pytest.mark.parametrize("text", [
    "Could you bring the beaker to the bench?",
    "Can you bring the beaker to the bench?",
    "Would you bring the beaker to the bench?",
    "Please bring the beaker to the bench.",
    "Bring the beaker to the bench.",
    "Have the robot bring the beaker to the bench.",
])
def test_requests_and_commands_still_go_to_the_planner(engine, text):
    assert engine.command(text)["kind"] == "proposal"


def test_a_future_instruction_is_still_scheduled(engine):
    assert engine.command("When the centrifuge stops, carry the beaker to the bench.")["kind"] == "scheduled"


@pytest.mark.parametrize("text,expected", [
    ("Can the beaker go in the oven?", True),
    ("can the beaker go in the oven", True),
    ("What is on the bench", True),
    ("Could you bring the beaker?", False),
    ("Please, where is it?", False),
    ("When the timer ends, open the gripper.", False),
    ("Do not move Sample B.", False),
    ("Bring the beaker to the bench.", False),
    ("Do you know where the beaker is", True),
    ("Have you seen the beaker", True),
    ("Have the robot bring the beaker to the bench.", False),
    ("Do the pick again.", False),
])
def test_asks_question(text, expected):
    assert asks_question(text) is expected
