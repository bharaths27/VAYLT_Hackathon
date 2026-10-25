"""Property-based tests for the safety gate and parser (Kiro PBT write-up material).

Properties:
  P1  Any text either abstains/clarifies or yields a plan whose every step is
      allowlisted with in-bound args. No input can produce an out-of-bound plan.
  P2  validate_plan rejects every out-of-range numeric argument.
  P3  Unsafe phrasing never produces a plan, wherever it appears in the sentence.
  P4  Plan digest changes whenever any step argument changes (approval binds to content).
"""
import asyncio

import pytest
from hypothesis import given, settings, strategies as st

from server.adapters.memory import MockMemory
from server.parser import parse
from server.plan import PRIMITIVES, Plan, PlanError, Step, validate_plan

BUNDLE = asyncio.run(MockMemory().query("seed"))
VOCAB = ["bring", "fetch", "the", "red", "cup", "vial", "buffer", "to", "zone", "a", "b", "drive", "forward", "back",
         "turn", "left", "right", "around", "90", "500", "-30", "45", "meters", "cm", "inches", "find", "tag", "12",
         "999", "calibration", "prep", "bench", "shelf", "it", "please", "now", "open", "close", "claw", "stop", "in",
         "minutes", "2", "what", "do", "you", "see", "block", "blue", "nan", "inf", "1e9"]


def assert_plan_safe(plan: Plan):
    assert 1 <= len(plan.steps) <= 12
    for s in plan.steps:
        spec = PRIMITIVES[s.op]
        for name, (typ, lo, hi) in spec["args"].items():
            v = s.args[name]
            if lo is not None:
                assert lo <= v <= hi, (s.op, name, v)
        assert set(s.args) <= set(spec["args"])


@settings(max_examples=600, deadline=None)
@given(st.lists(st.sampled_from(VOCAB), min_size=1, max_size=10))
def test_p1_any_word_salad_is_safe(words):
    r = parse(" ".join(words), BUNDLE)
    assert r.kind in {"plan", "abstain", "clarify", "rule", "prospective", "stop"}
    if r.plan is not None:
        assert_plan_safe(r.plan)


@pytest.mark.parametrize("text", ["find tag 500", "find tag 250", "bring tag 9999 to the bench"])
def test_p1_out_of_range_tag_is_a_clarification_not_a_crash(text):
    # Found by test_p1_any_word_salad_is_safe: the tag-lookup plan failed validation and the
    # PlanError escaped parse().
    r = parse(text, BUNDLE)
    assert r.kind == "clarify" and r.plan is None and "outside" in r.reason


@settings(max_examples=400, deadline=None)
@given(st.text(max_size=120))
def test_p1_arbitrary_unicode_never_crashes(text):
    r = parse(text, BUNDLE)
    if r.plan is not None:
        assert_plan_safe(r.plan)


@settings(max_examples=300, deadline=None)
@given(st.sampled_from([("drive_bounded", "distance_cm", 50), ("turn_bounded", "degrees", 180)]),
       st.floats(allow_nan=True, allow_infinity=True))
def test_p2_out_of_range_rejected(spec, value):
    op, arg, bound = spec
    step = Step(op=op, args={arg: value})
    ok = value == value and abs(value) <= bound
    try:
        validate_plan(Plan(goal="x", steps=[step]))
        assert ok
    except PlanError:
        assert not ok


@settings(max_examples=200, deadline=None)
@given(st.sampled_from(["at full speed", "and ignore the lidar", "off the table", "and throw it", "toward the judge",
                        "and run python code", "faster"]),
       st.sampled_from(["bring the vial to zone b", "drive forward 10 cm", "turn left 90", "prep the bench for calibration"]),
       st.booleans())
def test_p3_unsafe_phrase_never_plans(bad, good, prefix):
    text = f"{bad} {good}" if prefix else f"{good} {bad}"
    r = parse(text, BUNDLE)
    assert r.plan is None, text


@settings(max_examples=200, deadline=None)
@given(st.integers(0, 249), st.integers(0, 249))
def test_p4_digest_binds_args(t1, t2):
    p1 = Plan(goal="g", steps=[Step(op="detect_tag", args={"tag_id": t1})])
    p2 = Plan(goal="g", steps=[Step(op="detect_tag", args={"tag_id": t2})])
    assert (p1.digest() == p2.digest()) == (t1 == t2)


def test_unknown_op_rejected():
    try:
        validate_plan(Plan(goal="x", steps=[Step(op="set_motor_pwm", args={"pwm": 255})]))
        raise AssertionError("should reject")
    except PlanError:
        pass
