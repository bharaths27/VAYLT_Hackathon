"""EC07 / EC15: disposal requests refuse, and future intent never becomes an immediate move."""

import pytest

from engram.planner import refusal


@pytest.mark.parametrize("text", [
    "Dump Sample B in the trash.",
    "Get rid of Sample B, we don't need it.",
    "Discard the goggles",
    "dispose of sample b",
])
def test_disposal_requests_refuse_without_a_plan(taught, text):
    out = taught.command(text, "test")
    assert out["kind"] == "proposal" and out["status"] == "refused"
    assert "plan" not in out or not out.get("plan")
    assert taught.robot.calls == [("observe", {})]


@pytest.mark.parametrize("text", [
    "Once the incubation timer goes off, take Sample B to the bench",
    "After the incubation timer goes off, take Sample B to the bench",
    "As soon as the incubation timer goes off, take Sample B to the bench",
    "When the incubation timer goes off, take Sample B to the bench",
])
def test_future_intent_is_scheduled_not_moved(taught, text):
    out = taught.command(text, "test")
    assert out["kind"] == "scheduled" and out["created"] is True
    assert out["request"].lower().startswith("take sample b")
    assert taught.robot.calls == [("observe", {})]


@pytest.mark.parametrize("text", [
    "Take Sample B to the bench once the incubation timer goes off",
    "Take Sample B to the bench after the timer",
    "Take Sample B to the bench in 5 minutes",
])
def test_future_intent_that_does_not_parse_is_refused_never_moved(taught, text):
    out = taught.command(text, "test")
    assert out["kind"] == "proposal" and out["status"] == "refused"
    assert "When <event>, <task>" in out["reason"]


def test_ordinary_requests_still_plan():
    assert refusal("Take Sample B to the bench") is None


@pytest.mark.parametrize("text", [
    "After the experiment finished, what happened to sample A?",
    "Once the timer went off, where did Sample B go?",
    "When the incubation ended, which station was used",
])
def test_a_question_about_the_past_is_never_scheduled_or_moved(taught, text):
    # Practice check M2 (EC15 t10.2): recall is not a commitment.
    out = taught.command(text, "test")
    assert out["kind"] != "scheduled"
    assert taught.prospective.list() == []
    assert taught.robot.calls == [("observe", {})]


def test_a_real_future_instruction_is_still_scheduled_alongside_the_question_guard(taught):
    out = taught.command("When the incubation timer goes off, take Sample B to the bench", "test")
    assert out["kind"] == "scheduled" and out["created"] is True
    assert len(taught.prospective.list()) == 1


@pytest.mark.parametrize("text", [
    "Do not move Sample B",
    "Don't touch the goggles",
    "Please never take Sample A to the bench",
    "don’t bring the beaker to the bench",
])
def test_negated_requests_never_plan_a_move(taught, text):
    out = taught.command(text, "test")
    assert out["kind"] == "proposal" and out["status"] == "refused"
    assert "will not move" in out["reason"]
    assert taught.robot.calls == [("observe", {})]


def test_negation_can_still_be_taught_as_a_rule(taught):
    out = taught.command("Remember that you must never move Sample B without goggles", "test")
    assert out["kind"] == "taught"


# A rule cannot wait for an event: "When calibration ends, remember that Sample B must stay at the bench" was saved
# as a future task, and when the trigger fired the brain proposed MOVING Sample B to the bench. It is refused now,
# the way the console answers: say the rule now, or schedule a robot action.
DEFERRED_RULES = [
    "When calibration ends, remember that Sample B must stay at the bench",
    "When calibration ends, remember Sample B must stay at the bench",
    "When calibration ends, note that the bench is wet",
    "When calibration ends, from now on keep Sample B at the bench",
    "After the meeting, rule: goggles before any move",
    "Once the timer goes off, learn that the beaker is fragile",
    "As soon as the bench is clear, new rule: Sample A stays at home",
]


def _sources(engine) -> int:
    return engine.conn.execute("SELECT COUNT(*) FROM source").fetchone()[0]


@pytest.mark.parametrize("text", DEFERRED_RULES)
def test_a_deferred_rule_is_neither_scheduled_nor_taught(taught, text):
    from engram.prospective import parse_when

    facts, sources = len(taught.memory.current_facts()), _sources(taught)
    out = taught.command(text, "test")
    assert out["kind"] == "proposal" and out["status"] == "refused", out
    assert "rule now" in out["reason"] and "robot action" in out["reason"], out["reason"]
    assert parse_when(text) is None
    assert taught.prospective.list() == []
    assert (len(taught.memory.current_facts()), _sources(taught)) == (facts, sources)  # nothing taught
    assert taught.robot.calls == [("observe", {})]


@pytest.mark.parametrize("text,request_", [("When calibration ends, open the gripper", "open the gripper"),
                                           ("When calibration ends, bring Sample B to the bench",
                                            "bring Sample B to the bench")])
def test_a_deferred_robot_action_still_schedules(taught, text, request_):
    from engram.prospective import parse_when

    assert parse_when(text) == ("calibration ends", request_)
    out = taught.command(text, "test")
    assert out["kind"] == "scheduled" and out["created"] is True and out["request"] == request_, out


@pytest.mark.parametrize("text", ["Remember that goggles come before Sample B", "Note that goggles come before Sample B",
                                  "From now on, goggles come before Sample B", "Rule: goggles come before Sample B"])
def test_a_rule_said_now_is_taught(engine, text):
    # The brain recognises a rule in one place (engram.teach.rule_body), for teaching it and for refusing to
    # schedule it, so the two cannot drift apart.
    out = engine.command(text, "test")
    assert out["kind"] == "taught", out
    assert not engine.robot.state()["stopped"]
