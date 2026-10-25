"""The SpeculationGate: partials only prepare, stale answers are dropped, finals decide."""

import threading

import pytest

from engram.spark.voice import SpeculationGate, Thresholds, future_event, is_stop
from spark_fakes import FakeIntent, clock, decision, fake, spawn  # noqa: F401  (fixtures)

PARTIAL_ACTIONS = {"stop", "ignored", "dispatched", "queued", "unchanged"}


class Ctx:
    """A memory-context version the test can bump."""

    def __init__(self):
        self.v = 0

    def __call__(self):
        return self.v


def make_gate(fake, spawn, clock, ctx=None, prefetch=None, **kw):
    return SpeculationGate(fake, prefetch=prefetch, context_version=ctx or (lambda: 0), clock=clock, spawn=spawn,
                           **kw)


# ---- partials only prepare ---------------------------------------------------------------------------------------


def test_partials_only_prepare_and_never_return_a_proposal(fake, spawn, clock):
    prefetched = []
    gate = make_gate(fake, spawn, clock, prefetch=lambda text: prefetched.append(text) or {"goal": text})
    for rev, text in enumerate(["Open...", "Open the", "Open the claw", "Open the claw"]):
        r = gate.on_partial(1, rev, text)
        spawn.run_all()
        assert r.action in PARTIAL_ACTIONS
    assert not hasattr(gate, "engine")
    p = gate.prepared()
    assert p.decision.preset == "open_gripper" and p.key.text == "Open the claw"
    assert p.bundle == {"goal": "Open the claw"} and prefetched == ["Open the claw"]
    assert all(partial for _, partial in fake.calls)


def test_open_alone_prepares_no_preset(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open...")
    spawn.run_all()
    assert gate.prepared() is None
    assert fake.calls == [("Open...", True)]


def test_open_the_claw_prepares_open(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    assert gate.on_partial(1, 0, "Open the claw").action == "dispatched"
    spawn.run_all()
    assert gate.prepared().decision.preset == "open_gripper"


def test_a_prepared_preset_is_withdrawn_when_the_text_grows(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    gate.on_partial(1, 1, "Open the claw after")
    assert gate.prepared() is None


# ---- finals ------------------------------------------------------------------------------------------------------


def test_final_equal_to_prepared_text_reuses_the_prediction(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    r = gate.on_final(1, 1, "Open the claw")
    assert r.action == "propose" and r.preset == "open_gripper" and r.reused
    assert len(fake.calls) == 1 and gate.stats.requests == 1 and gate.stats.reused == 1


def test_a_below_threshold_partial_answer_is_asked_again_at_the_final(spawn, clock):
    # Measured on the real model: "Look around." scored 0.47 as a partial and 0.63-0.66 as a final.
    calls = []

    def model(text, partial):
        calls.append((text, partial))
        return decision("look_around", confidence=0.47 if partial else 0.65)

    gate = make_gate(model, spawn, clock)
    gate.on_partial(1, 0, "Look around.")
    spawn.run_all()
    assert gate.prepared() is None                      # below threshold: nothing prepared
    r = gate.on_final(1, 1, "Look around.")
    assert r.action == "propose" and r.preset == "look_around" and not r.reused
    assert calls == [("Look around.", True), ("Look around.", False)]   # exactly one extra call
    assert gate.stats.reused == 0


@pytest.mark.parametrize("partial_answer", [
    decision(None, confidence=0.9),                          # nothing to do
    decision("open_gripper", ready=0.2),                     # not ready yet
    decision("open_gripper", directed=0.2),                  # not addressed to the robot
], ids=["no preset", "not ready", "not directed"])
def test_no_action_partial_answers_are_never_reused(spawn, clock, partial_answer):
    calls = []

    def model(text, partial):
        calls.append(partial)
        return partial_answer if partial else decision("open_gripper")

    gate = make_gate(model, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    assert gate.on_final(1, 1, "Open the claw").action == "propose"
    assert calls == [True, False]


def test_final_that_differs_is_classified_as_a_final(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the")
    spawn.run_all()
    r = gate.on_final(1, 1, "Open the claw")
    assert r.action == "propose" and not r.reused
    assert fake.calls == [("Open the", True), ("Open the claw", False)]


def test_late_future_qualifier_routes_to_a_when_commitment(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    assert gate.prepared() is not None
    r = gate.on_final(1, 1, "Open the claw after calibration finishes")
    assert r.action == "schedule"
    assert r.commitment == "When calibration finishes, Open the gripper"
    assert len(fake.calls) == 2


def test_future_qualifier_blocks_even_when_the_model_misses_it(spawn, clock):
    fake = FakeIntent({"Open the claw after calibration finishes": decision("open_gripper")})  # future=0.0
    r = make_gate(fake, spawn, clock).on_final(1, 0, "Open the claw after calibration finishes")
    assert r.action == "schedule"


def test_future_is_only_scheduled_when_the_request_is_ready(spawn, clock):
    text = "Open the claw after calibration"
    fake = FakeIntent({text: decision("open_gripper", ready=0.3, future=0.9)})
    r = make_gate(fake, spawn, clock).on_final(1, 0, text)
    assert r.action == "clarify" and r.commitment is None


def test_model_future_flag_without_an_event_asks_to_rephrase(spawn, clock):
    fake = FakeIntent({"Open the claw in a bit": decision("open_gripper", future=0.9)})
    r = make_gate(fake, spawn, clock).on_final(1, 0, "Open the claw in a bit")
    assert r.action == "clarify" and "When <event>, <task>" in r.reason


def test_late_no_dont_discards_the_prepared_preset(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    r = gate.on_final(1, 1, "Open the claw, no, don't")
    assert r.action == "none" and r.preset is None


@pytest.mark.parametrize("text", ["Please don't open the claw", "Do not open the claw",
                                  "Please don" + chr(0x2019) + "t open the claw", "Open the claw, no, don't"])
def test_negation_in_the_words_blocks_even_when_the_model_misses_it(spawn, clock, text):
    fake = FakeIntent({text: decision("open_gripper")})  # the model raises no flags at all
    assert make_gate(fake, spawn, clock).on_final(1, 0, text).action == "none"


def test_negation_from_the_model_blocks_even_without_negation_words(spawn, clock):
    text = "Open the claw, on reflection leave it shut"      # no local negation, correction or future cue
    gate = make_gate(FakeIntent({text: decision("open_gripper", negated=0.9)}), spawn, clock)
    assert not any(gate._flags(text, decision("open_gripper")))   # so only the model's flag can block it
    assert gate.on_final(1, 0, text).action == "none"


@pytest.mark.parametrize("text", [
    "Open the claw; actually, close it",
    "ACTUALLY close the claw",
    "Close the claw instead",
    "Open the claw, or rather close it",
    "Open the claw I mean close it",
    "Open the claw no wait close it",
    "Open the claw, scratch that, close it",
])
def test_correction_words_block_even_when_the_model_is_confident(spawn, clock, text):
    fake = FakeIntent({text: decision("close_gripper", confidence=0.97, ready=0.95, correction=0.05)})
    r = make_gate(fake, spawn, clock).on_final(1, 0, text)
    assert r.action == "none" and r.preset is None


def test_correction_means_no_action(spawn, clock):
    text = "Open the claw, I mean close it"
    fake = FakeIntent({text: decision("close_gripper")})
    assert make_gate(fake, spawn, clock).on_final(1, 0, text).action == "none"


@pytest.mark.parametrize("text", ["Where are the goggles?", "Open the"])
def test_questions_and_unfinished_finals_do_nothing(fake, spawn, clock, text):
    assert make_gate(fake, spawn, clock).on_final(1, 0, text).action == "none"


def test_a_failed_intent_request_never_acts(spawn, clock):
    fake = FakeIntent(fail=True)
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    assert gate.prepared() is None and gate.stats.errors == 1
    assert gate.on_final(1, 1, "Open the claw").action == "clarify"


# ---- stale answers are dropped -----------------------------------------------------------------------------------


def test_answer_for_an_older_revision_is_dropped(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    assert gate.on_partial(1, 1, "Open the claw after").action == "queued"
    spawn.run_next()                      # the answer for revision 0 arrives after revision 1 exists
    assert gate.stats.stale_dropped == 1 and gate.prepared() is None


def test_answer_from_before_the_text_changed_and_changed_back_is_dropped(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    gate.on_partial(1, 1, "Open the")
    gate.on_partial(1, 2, "Open the claw")      # same words as revision 0, but said again later
    spawn.run_next()
    assert gate.stats.stale_dropped == 1 and gate.prepared() is None
    spawn.run_all()                             # the newest revision is asked about and kept
    assert gate.prepared().key.revision == 2


def test_answer_for_an_older_utterance_is_dropped(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    gate.on_partial(2, 0, "Close the claw")
    spawn.run_next()
    assert gate.stats.stale_dropped == 1
    spawn.run_all()
    assert gate.prepared().decision.preset == "close_gripper"


def test_answer_after_the_memory_context_changed_is_dropped(fake, spawn, clock):
    ctx = Ctx()
    gate = make_gate(fake, spawn, clock, ctx=ctx)
    gate.on_partial(1, 0, "Open the claw")
    ctx.v += 1
    gate.on_partial(1, 1, "Open the claw")      # the caller's thread sees the new context first
    spawn.run_next()
    assert gate.stats.stale_dropped == 1
    spawn.run_all()                             # asked again under the new context
    assert gate.prepared().key.context == 1 and len(fake.calls) == 2


def test_answer_is_not_used_if_the_context_changed_unseen_while_it_was_out(fake, spawn, clock):
    ctx = Ctx()
    gate = make_gate(fake, spawn, clock, ctx=ctx)
    gate.on_partial(1, 0, "Open the claw")
    ctx.v += 1                                  # nobody on the caller's thread has looked yet
    spawn.run_all()
    assert gate.prepared() is None              # checked against a fresh read when used
    r = gate.on_final(1, 1, "Open the claw")
    assert r.action == "propose" and not r.reused and len(fake.calls) == 2


def test_context_and_prefetch_run_only_on_the_callers_thread():
    me = threading.get_ident()
    seen, workers = [], []
    fake = FakeIntent()

    def classifier(text, partial):
        workers.append(threading.get_ident())
        return fake(text, partial)

    def ctx():
        seen.append(("context", threading.get_ident()))
        return 0

    def prefetch(text):
        seen.append(("prefetch", threading.get_ident()))
        return {"goal": text}

    gate = SpeculationGate(classifier, prefetch=prefetch, context_version=ctx)   # real worker threads
    for rev, text in enumerate(["Open", "Open the", "Open the claw"]):
        gate.on_partial(1, rev, text)
    assert gate.wait_idle(5)
    gate.on_partial(1, 3, "Open the claw")
    assert gate.prepared().bundle == {"goal": "Open the claw"}
    assert gate.on_final(1, 4, "Open the claw").action == "propose"
    assert workers and me not in workers                    # the model was asked off-thread
    assert {kind for kind, _ in seen} == {"context", "prefetch"} and {t for _, t in seen} == {me}


def test_answer_that_arrives_expired_is_dropped(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock, ttl_s=2.0)
    gate.on_partial(1, 0, "Open the claw")
    clock.advance(2.5)
    spawn.run_all()
    assert gate.stats.stale_dropped == 1 and gate.prepared() is None


def test_expired_prediction_is_not_reused_by_the_final(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock, ttl_s=2.0)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    clock.advance(2.5)
    r = gate.on_final(1, 1, "Open the claw")
    assert not r.reused and gate.stats.expired == 1 and len(fake.calls) == 2


def test_prediction_is_not_reused_after_the_context_changed(fake, spawn, clock):
    ctx = Ctx()
    gate = make_gate(fake, spawn, clock, ctx=ctx)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    ctx.v += 1
    r = gate.on_final(1, 1, "Open the claw")
    assert r.action == "propose" and not r.reused and len(fake.calls) == 2


def test_late_transcripts_for_old_revisions_and_utterances_are_ignored(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(2, 3, "Open the claw")
    assert gate.on_partial(2, 1, "Open").action == "ignored"
    assert gate.on_partial(1, 9, "Close the claw").action == "ignored"
    assert gate.on_final(1, 9, "Close the claw").action == "ignored"
    spawn.run_all()
    gate.on_final(2, 4, "Open the claw")
    assert gate.on_partial(2, 5, "Open the claw more").action == "ignored"   # already final


# ---- one request in flight, one pending ----------------------------------------------------------------------------


def test_single_flight_keeps_only_the_newest_pending_partial(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    assert gate.on_partial(1, 0, "Open").action == "dispatched"
    texts = ["Open the", "Open the c", "Open the cl", "Open the cla", "Open the claw"]
    for rev, text in enumerate(texts, start=1):
        assert gate.on_partial(1, rev, text).action == "queued"
    assert len(spawn.jobs) == 1 and gate.in_flight.text == "Open"
    assert gate.pending == (1, 5, "Open the claw")
    assert gate.stats.coalesced == 4
    spawn.run_next()                      # stale answer for "Open" dropped; the newest pending goes out
    assert len(spawn.jobs) == 1 and gate.in_flight.text == "Open the claw" and gate.pending is None
    spawn.run_all()
    assert [t for t, _ in fake.calls] == ["Open", "Open the claw"]
    assert gate.prepared().decision.preset == "open_gripper"


def test_single_flight_with_real_threads():
    fake = FakeIntent()
    fake.hold()
    gate = SpeculationGate(fake)
    gate.on_partial(1, 0, "Open")
    assert fake.entered.acquire(timeout=5)
    for rev, text in enumerate(["Open the", "Open the c", "Open the cl", "Open the cla", "Open the claw"], start=1):
        gate.on_partial(1, rev, text)
    assert len(fake.calls) == 1 and gate.pending == (1, 5, "Open the claw")
    fake.release.set()
    assert gate.wait_idle(5)
    assert [t for t, _ in fake.calls] == ["Open", "Open the claw"] and fake.max_active == 1
    assert gate.prepared().key.text == "Open the claw"


def test_final_waits_for_the_identical_request_in_flight_instead_of_sending_it_again():
    fake = FakeIntent()
    fake.hold()
    gate = SpeculationGate(fake)
    gate.on_partial(1, 0, "Open the claw")
    assert fake.entered.acquire(timeout=5)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", gate.on_final(1, 1, "Open the claw")))
    t.start()
    fake.release.set()
    t.join(5)
    assert out["r"].action == "propose" and out["r"].reused
    assert fake.calls == [("Open the claw", True)]


def test_final_waits_for_a_stale_request_so_only_one_is_ever_in_flight():
    fake = FakeIntent()
    fake.hold()
    gate = SpeculationGate(fake)
    gate.on_partial(1, 0, "Open the")
    assert fake.entered.acquire(timeout=5)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", gate.on_final(1, 1, "Open the claw")))
    t.start()
    fake.release.set()
    t.join(5)
    assert out["r"].action == "propose" and not out["r"].reused
    assert fake.calls == [("Open the", True), ("Open the claw", False)] and fake.max_active == 1


def test_final_gives_up_if_the_request_in_flight_never_answers(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock, final_wait_s=0.05)
    gate.on_partial(1, 0, "Open the claw")         # dispatched but never run
    assert gate.on_final(1, 1, "Open the claw").action == "clarify"


# ---- stop ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Stop", "stop!", "Halt", "freeze", "please STOP the arm", "Open the claw, stop"])
def test_stop_is_local_and_never_asks_the_model(fake, spawn, clock, text):
    gate = make_gate(fake, spawn, clock)
    assert gate.on_partial(1, 0, text).action == "stop"
    assert gate.on_final(1, 1, text).action == "stop"
    assert fake.calls == [] and spawn.jobs == []


def test_stop_voids_a_held_request_and_rejects_the_rest_of_the_utterance():
    fake = FakeIntent()
    fake.hold()
    gate = SpeculationGate(fake)
    gate.on_partial(1, 0, "Open the claw")
    assert fake.entered.acquire(timeout=5)                   # held in flight
    assert gate.on_partial(1, 1, "Open the claw now").action == "queued"
    assert gate.on_partial(1, 2, "Open the claw now, stop").action == "stop"
    assert gate.pending is None                              # the queued partial is discarded at once
    fake.release.set()
    assert gate.wait_idle(5)
    assert gate.prepared() is None and gate.stats.stale_dropped == 1
    assert gate.on_partial(1, 3, "Open the claw").reason == "stopped"     # a later revision without "stop"
    late = gate.on_final(1, 4, "Open the claw")
    assert late.action == "ignored" and late.reason == "stopped"
    assert fake.calls == [("Open the claw", True)]
    assert gate.on_partial(2, 0, "Close the claw").action == "dispatched"   # the next utterance starts clean


def test_stop_voids_a_stored_prediction(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    gate.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    assert gate.prepared() is not None
    gate.on_partial(1, 1, "Open the claw, stop")
    assert gate.prepared() is None
    assert gate.on_final(1, 2, "Open the claw").action == "ignored"
    assert len(fake.calls) == 1


def test_stop_ends_a_final_that_is_waiting_for_the_model():
    fake = FakeIntent()
    fake.hold()
    gate = SpeculationGate(fake)
    gate.on_partial(1, 0, "Open the")
    assert fake.entered.acquire(timeout=5)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", gate.on_final(1, 1, "Open the claw")))
    t.start()
    assert gate.on_partial(1, 2, "stop").action == "stop"
    t.join(5)
    fake.release.set()
    assert out["r"].action == "ignored" and len(fake.calls) == 1


@pytest.mark.parametrize("partial_first", [True, False], ids=["reused prediction", "fresh final"])
def test_stop_during_the_finals_prefetch_discards_the_final(spawn, clock, partial_first):
    entered, release = threading.Event(), threading.Event()

    def slow_prefetch(text):
        entered.set()
        assert release.wait(5)
        return {"goal": text}

    fake = FakeIntent()
    gate = SpeculationGate(fake, prefetch=slow_prefetch, clock=clock, spawn=spawn)
    if partial_first:
        gate.on_partial(1, 0, "Open the claw")
        spawn.run_all()                                  # prediction stored; its bundle not fetched yet
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", gate.on_final(1, 1, "Open the claw")))
    t.start()
    assert entered.wait(5)                               # the final is inside the prefetch
    assert gate.on_partial(1, 2, "stop").action == "stop"
    release.set()
    t.join(5)
    assert out["r"].action == "ignored" and out["r"].reason == "stopped"
    assert len(fake.calls) == 1


@pytest.mark.parametrize("partial_first", [True, False], ids=["reused prediction", "fresh final"])
@pytest.mark.parametrize("advance,expected", [(0.0, "propose"), (2.5, "clarify")], ids=["in time", "expired"])
def test_a_prediction_that_expires_during_the_prefetch_is_not_acted_on(spawn, clock, partial_first, advance,
                                                                        expected):
    entered, release = threading.Event(), threading.Event()

    def slow_prefetch(text):
        entered.set()
        assert release.wait(5)
        return {"goal": text}

    fake = FakeIntent()
    gate = SpeculationGate(fake, prefetch=slow_prefetch, clock=clock, spawn=spawn, ttl_s=2.0)
    if partial_first:
        gate.on_partial(1, 0, "Open the claw")
        spawn.run_all()                                  # valid prediction, bundle not fetched yet
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", gate.on_final(1, 1, "Open the claw")))
    t.start()
    assert entered.wait(5)                               # the final chose its answer and is now prefetching
    clock.advance(advance)                               # 2.5 s is past the 2 s TTL
    release.set()
    t.join(5)
    assert out["r"].action == expected and len(fake.calls) == 1
    assert gate.stats.expired == (1 if expected == "clarify" else 0)


def test_stop_wins_even_with_malformed_ids(fake, spawn, clock):
    gate = make_gate(fake, spawn, clock)
    assert gate.on_partial(-1, True, "Stop").action == "stop"
    assert gate.on_final("x", None, "halt").action == "stop"
    with pytest.raises(ValueError):
        gate.on_partial(-1, 0, "Open the claw")
    assert fake.calls == []


def test_partial_of_a_new_utterance_queued_during_a_final_is_sent_afterwards():
    fake = FakeIntent()
    fake.hold()
    gate = SpeculationGate(fake)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", gate.on_final(1, 0, "Open the claw")))
    t.start()
    assert fake.entered.acquire(timeout=5)              # the final is being classified
    assert gate.on_partial(2, 0, "Close the claw").action == "queued"
    fake.release.set()
    t.join(5)
    assert out["r"].action == "none"                    # superseded by the newer utterance; never acted on
    assert gate.wait_idle(5) and gate.prepared().decision.preset == "close_gripper"
    assert fake.max_active == 1


def test_stop_word_matching_uses_whole_words():
    assert not is_stop("Open the stopwatch drawer") and not is_stop("the antifreeze bottle")


# ---- pieces --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,event", [
    ("Open the claw after calibration finishes", "calibration finishes"),
    ("Once calibration finishes, open the claw", "calibration finishes"),
    ("Open the claw when the timer goes off.", "the timer goes off"),
    ("Open the claw later", None),
    ("Open the claw", None),
])
def test_future_event(text, event):
    assert future_event(text) == event


def test_a_classifier_that_cannot_be_called_is_refused_when_wiring():
    with pytest.raises(TypeError):
        SpeculationGate(object())


def test_thresholds_are_explicitly_experimental():
    assert "EXPERIMENTAL" in Thresholds.__doc__
    th = Thresholds()
    assert th.block < min(th.preset, th.ready, th.directed)
