"""Voice presets reach the real Engine only as unapproved proposals or future tasks."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from engram.checker import check_plan
from engram.capabilities import CAPABILITIES
from engram.db import SCHEMA
from engram.engine import Engine, EngineError
from engram.planner import refusal
from engram.robot.mock import MockRobot
from engram.spark.audio_windows import SAMPLE_RATE, AudioOverflow, LiveTranscriber, OutOfOrder, PcmWindow
from engram.spark.intent_client import MAX_TEXT, IntentClient
from engram.spark.presets import PRESETS
from engram.spark.voice_integration import VoiceIntegration, memory_version
from spark_fakes import (  # noqa: F401  (fixtures)
    REJECTIONS, SAMPLES, FakeIntent, RecordingTransport, clock, decision, fake, spawn,
)

OBSERVED_ONLY = [("observe", {})]   # the fixture's overhead scan; nothing else may reach the robot
ROOT = Path(__file__).resolve().parents[2]


def count(engine, table):
    return engine.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def nothing_happened(engine):
    return (count(engine, "episode") == 0 and count(engine, "approval") == 0 and count(engine, "prospective_task") == 0
            and engine.robot.calls == OBSERVED_ONLY)


@pytest.fixture
def voice(taught, fake, spawn, clock):
    return VoiceIntegration(taught, fake, spawn=spawn, clock=clock)


def test_partials_never_create_proposals_or_robot_calls(voice, spawn):
    engine = voice.engine
    outs = []
    for rev, text in enumerate(["Open...", "Open the", "Open the claw", "Open the claw", "Close the claw",
                                "Open the claw after calibration finishes", "Open the claw, no, don't"]):
        outs.append(voice.on_partial(1, rev, text))
        spawn.run_all()
        outs.append(voice.on_partial(1, rev, text))   # again, after the answer arrived
    assert {o["kind"] for o in outs} == {"partial"}
    assert any(o["preparing"] == "open_gripper" for o in outs)
    assert nothing_happened(engine) and engine.robot.state()["stopped"] is False


def test_prepared_partial_carries_the_prefetched_memory_bundle(voice, spawn):
    voice.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    out = voice.on_partial(1, 0, "Open the claw")
    assert out["preparing"] == "open_gripper" and out["memory"]["goal"] == "Open the claw"


def test_a_preset_reaches_engine_propose_and_stays_unapproved(voice, spawn, fake, monkeypatch):
    engine = voice.engine
    asked = []
    real_propose = engine.propose
    monkeypatch.setattr(engine, "propose", lambda request: asked.append(request) or real_propose(request))
    voice.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    out = voice.on_final(1, 1, "Open the claw")
    assert asked == ["Open the gripper"]
    assert out["kind"] == "proposal" and out["status"] == "proposed" and out["preset"] == "open_gripper"
    assert out["reused_prediction"] and len(fake.calls) == 1
    assert out["plan"]["steps"] == [{"op": "gripper_open", "args": {}}] and out["check"]["ok"]
    row = engine.conn.execute("SELECT outcome, request FROM episode WHERE id = ?", (out["operation_id"],)).fetchone()
    assert (row["outcome"], row["request"]) == ("proposed", "Open the gripper")
    assert count(engine, "approval") == 0 and engine.robot.calls == OBSERVED_ONLY


def test_the_voice_proposal_still_needs_a_person_and_the_exact_digest(voice, spawn):
    engine = voice.engine
    out = voice.on_final(1, 0, "Close the claw")
    assert out["status"] == "proposed" and engine.robot.calls == OBSERVED_ONLY
    with pytest.raises(EngineError, match="digest"):
        engine.approve_and_run(out["operation_id"], "0" * 64, "carter")
    assert engine.robot.calls == OBSERVED_ONLY
    ran = engine.approve_and_run(out["operation_id"], out["digest"], "carter")   # a person, by digest
    assert ran["outcome"] == "success" and engine.robot.calls[-1] == ("gripper_close", {})


def test_late_future_qualifier_becomes_a_when_commitment_not_a_move(voice, spawn):
    engine = voice.engine
    voice.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    out = voice.on_final(1, 1, "Open the claw after calibration finishes")
    assert out["kind"] == "scheduled" and out["created"]
    assert (out["trigger"], out["request"]) == ("calibration finishes", "Open the gripper")
    assert count(engine, "episode") == 0 and engine.robot.calls == OBSERVED_ONLY
    # When the event happens, the commitment becomes an ordinary proposal; it still waits for a person.
    fired = engine.fire_trigger("calibration finishes")
    assert fired[0]["proposal"]["status"] == "proposed"
    assert fired[0]["proposal"]["plan"]["steps"] == [{"op": "gripper_open", "args": {}}]
    assert count(engine, "approval") == 0 and engine.robot.calls == OBSERVED_ONLY


def test_late_negation_discards_the_prepared_preset(voice, spawn):
    voice.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    out = voice.on_final(1, 1, "Open the claw, no, don't")
    assert out["kind"] == "none"
    assert nothing_happened(voice.engine)


def test_engine_refusal_words_still_apply_to_what_was_said(taught, spawn, clock):
    text = "Open the claw and throw the beaker"
    voice = VoiceIntegration(taught, FakeIntent({text: decision("open_gripper")}), spawn=spawn, clock=clock)
    out = voice.on_final(1, 0, text)
    assert out["kind"] == "none" and "throw" in out["reason"]
    assert nothing_happened(taught)


@pytest.mark.parametrize("text,said", [
    ("Open the claw and throw the beaker", decision("open_gripper")),                         # act now
    ("Throw the beaker and open the claw after calibration finishes",                         # act later
     decision("open_gripper", future=0.95)),
])
def test_one_precheck_guards_both_proposing_and_scheduling(taught, spawn, clock, text, said):
    voice = VoiceIntegration(taught, FakeIntent({text: said}), spawn=spawn, clock=clock)
    out = voice.on_final(1, 0, text)
    assert out["kind"] == "none" and "throw" in out["reason"]
    assert taught.prospective.list() == [] and nothing_happened(taught)


def test_a_clean_deferred_preset_is_still_scheduled(taught, spawn, clock):
    voice = VoiceIntegration(taught, FakeIntent(), spawn=spawn, clock=clock)
    out = voice.on_final(1, 0, "Open the claw after calibration finishes")
    assert out["kind"] == "scheduled" and out["request"] == "Open the gripper"
    assert [t["request"] for t in taught.prospective.list()] == ["Open the gripper"]


def test_refusal_future_ok_skips_only_the_deferral_check():
    assert "later" in refusal("Open the claw after calibration finishes")
    assert refusal("Open the claw after calibration finishes", future_ok=True) is None
    assert "throw" in refusal("Throw it after calibration finishes", future_ok=True)
    assert "will not move" in refusal("Don't open the claw after calibration", future_ok=True)


@pytest.mark.parametrize("text", ["Wait!", "Hold on", "pause", "Hang on.", "Whoa"])
def test_new_stop_words_in_a_partial_stop_the_robot(voice, fake, spawn, text):
    out = voice.on_partial(1, 0, text)
    assert out["kind"] == "stop" and out["state"]["stopped"] is True
    assert fake.calls == [] and spawn.jobs == [] and count(voice.engine, "episode") == 0


def test_holding_something_in_a_partial_is_not_a_stop(voice, spawn):
    out = voice.on_partial(1, 0, "Hold the vial")
    assert out["kind"] == "partial" and voice.engine.robot.state()["stopped"] is False


def test_stop_stops_at_once_without_asking_the_model(voice, fake, spawn):
    out = voice.on_partial(1, 0, "Stop")
    assert out["kind"] == "stop" and out["state"]["stopped"] is True
    assert fake.calls == [] and spawn.jobs == []
    assert voice.on_final(2, 0, "halt!")["kind"] == "stop" and fake.calls == []
    assert count(voice.engine, "episode") == 0 and voice.engine.robot.calls == OBSERVED_ONLY


def test_nothing_from_a_stopped_utterance_is_proposed_after_the_stop(voice, spawn):
    voice.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    assert voice.on_partial(1, 1, "Open the claw")["preparing"] == "open_gripper"
    assert voice.on_partial(1, 2, "Open the claw, stop")["kind"] == "stop"
    assert voice.engine.robot.state()["stopped"] is True
    out = voice.on_final(1, 3, "Open the claw")
    assert out["kind"] == "ignored"
    assert count(voice.engine, "episode") == 0 and voice.engine.robot.calls == OBSERVED_ONLY


def test_new_memory_between_partial_and_final_means_asking_again(voice, spawn, fake):
    voice.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    before = memory_version(voice.engine)
    voice.engine.teach("Note", "The claw must be open before loading Sample B.")
    assert memory_version(voice.engine) != before
    out = voice.on_final(1, 1, "Open the claw")
    assert out["status"] == "proposed" and not out["reused_prediction"] and len(fake.calls) == 2


def test_an_overlong_transcript_is_never_classified_or_acted_on(taught, spawn, clock):
    net = RecordingTransport()                                  # would answer "open the gripper", confidently
    voice = VoiceIntegration(taught, IntentClient("placeholder", "intent-model-under-test", transport=net),
                             spawn=spawn, clock=clock)
    long_text = "Open the claw " + "and keep going " * 80        # over MAX_TEXT
    assert len(long_text) > MAX_TEXT
    voice.on_partial(1, 0, long_text)
    spawn.run_all()
    assert voice.gate.prepared() is None and voice.gate.stats.errors == 1
    out = voice.on_final(1, 1, long_text)
    assert out["kind"] == "clarify" and "say it again" in out["reason"] and net.sent == []
    assert nothing_happened(taught)
    assert voice.on_final(2, 0, "Open the claw")["status"] == "proposed" and len(net.sent) == 1   # control


def test_not_a_preset_does_nothing(voice):
    out = voice.on_final(1, 0, "Where are the goggles?")
    assert out["kind"] == "none" and nothing_happened(voice.engine)


def strict_engine(arena):
    """An Engine on a connection that only its creating thread may use (sqlite3's default)."""
    conn = sqlite3.connect(":memory:")                      # check_same_thread=True
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    eng = Engine(conn, MockRobot(arena), set(arena["zones"]))
    eng.load_arena(arena)
    eng.overhead_scan()
    eng.teach("SOP 004", (ROOT / "fixtures/lab/SOP_004.md").read_text())
    return eng


def test_real_threaded_worker_never_touches_a_thread_bound_connection(arena):
    engine = strict_engine(arena)
    fake = FakeIntent()
    voice = VoiceIntegration(engine, fake)                    # the default worker: real threads
    assert voice.on_partial(1, 0, "Open the claw")["status"] == "dispatched"
    assert voice.gate.wait_idle(5)
    out = voice.on_partial(1, 1, "Open the claw")
    assert out["preparing"] == "open_gripper" and out["memory"]["goal"] == "Open the claw"
    final = voice.on_final(1, 2, "Open the claw")
    assert final["status"] == "proposed" and final["reused_prediction"] and len(fake.calls) == 1
    assert voice.gate.stats.errors == 0 and engine.robot.calls == OBSERVED_ONLY


def test_presets_use_only_existing_capabilities_and_pass_the_checker(engine):
    for p in PRESETS.values():
        steps = p.plan_steps()
        assert all(s["op"] in CAPABILITIES and s["op"] != "stop" for s in steps)
        assert check_plan({"goal": p.request, "steps": steps}, engine.memory, engine.zones).ok


def test_fixed_requests_only_exist_once_registered(engine, fake):
    assert engine.command("Open the gripper")["status"] == "refused"   # unchanged engine behaviour
    VoiceIntegration(engine, fake)
    assert engine.command("open the gripper.")["status"] == "proposed"
    assert engine.command("Don't open the gripper")["status"] == "refused"
    assert engine.robot.calls == OBSERVED_ONLY


class _InstantWhisper:
    """Transcribes every window as the same words, instantly."""

    def __init__(self, text="Open the claw"):
        self.text, self.calls = text, []

    def transcribe(self, wav, cancel=None):
        self.calls.append("partial" if cancel is not None else "final")
        return self.text


def _live_partials(voice, utterance, window, stt, spawn):
    """Feed 0.6 s of audio, take the first partial through the gate, let the gate classify it."""
    window.append(b"\x01\x00" * int(0.6 * SAMPLE_RATE))
    tr = LiveTranscriber(stt, window, poll_s=0.005)
    tr.start()
    end, got = time.monotonic() + 2.0, None
    while got is None and time.monotonic() < end:
        got = tr.poll()
        time.sleep(0.005)
    assert got is not None
    voice.on_partial(utterance, got[0], got[1])
    spawn.run_all()
    return tr


def test_an_overflowed_utterance_can_never_reach_a_proposal(taught, spawn, clock):
    voice = VoiceIntegration(taught, FakeIntent(), spawn=spawn, clock=clock)
    stt, window = _InstantWhisper(), PcmWindow(max_seconds=1.0)
    tr = _live_partials(voice, 1, window, stt, spawn)
    assert voice.gate.prepared().decision.preset == "open_gripper"   # the partials prepared a preset
    with pytest.raises(AudioOverflow):
        window.append(b"\x01\x00" * int(0.6 * SAMPLE_RATE))           # "...no, don't" never fits
    out = voice.finish_live(1, 99, tr)
    assert out["kind"] == "clarify" and "say it again" in out["reason"]
    assert stt.calls.count("final") == 0                              # no final transcript at all
    assert voice.gate.prepared() is None                              # what the partials prepared is void
    assert voice.on_final(1, 100, "Open the claw")["kind"] == "ignored"   # and can never be reused
    assert nothing_happened(taught)
    # Control: the next utterance, which fits, still becomes a proposal through the same path.
    stt2, window2 = _InstantWhisper(), PcmWindow(max_seconds=1.0)
    tr2 = _live_partials(voice, 2, window2, stt2, spawn)
    done = voice.finish_live(2, 99, tr2)
    assert done["kind"] == "proposal" and done["status"] == "proposed" and done["reused_prediction"]
    assert taught.robot.calls == OBSERVED_ONLY


class _BlockingFinalWhisper:
    """Partials answer at once; the FINAL transcription blocks until the test releases it."""

    def __init__(self, text="Open the claw"):
        self.text, self.calls = text, []
        self.final_started, self.release = threading.Event(), threading.Event()

    def transcribe(self, wav, cancel=None):
        if cancel is not None:
            self.calls.append("partial")
            return self.text
        self.calls.append("final")
        self.final_started.set()
        assert self.release.wait(5), "the test never released the final"
        return self.text


@pytest.mark.parametrize("tail_s", [0.6, 0.05], ids=["tail overflows the window", "tail fits"])
def test_audio_arriving_while_the_final_is_made_never_becomes_a_proposal(taught, spawn, clock, tail_s):
    voice = VoiceIntegration(taught, FakeIntent(), spawn=spawn, clock=clock)
    stt, window = _BlockingFinalWhisper(), PcmWindow(max_seconds=1.0)
    tr = _live_partials(voice, 1, window, stt, spawn)
    assert voice.gate.prepared().decision.preset == "open_gripper"
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", voice.finish_live(1, 99, tr)))
    t.start()
    assert stt.final_started.wait(2)                            # "Open the claw" is being finalized...
    with pytest.raises(AudioOverflow):
        window.append(b"\x01\x00" * int(tail_s * SAMPLE_RATE))  # ...when "no, don't" arrives
    stt.release.set()
    t.join(5)
    assert out["r"]["kind"] == "clarify"                        # not final: say it again
    assert voice.gate.prepared() is None
    assert nothing_happened(taught)


@pytest.mark.parametrize("later", [[2, 3], [2, 1, 3]], ids=["chunk 1 dropped", "chunk 1 late"])
def test_an_utterance_with_a_missing_chunk_never_becomes_a_proposal(taught, spawn, clock, later):
    voice = VoiceIntegration(taught, FakeIntent(), spawn=spawn, clock=clock)
    stt, window = _InstantWhisper(), PcmWindow(max_seconds=5.0)
    tr = _live_partials(voice, 1, window, stt, spawn)           # chunk 0 arrived; "open the gripper" prepared
    assert voice.gate.prepared().decision.preset == "open_gripper"
    for seq in later:                                           # a caller that catches the error and carries on
        try:
            window.append(bytes(3200), seq=seq)
        except (AudioOverflow, OutOfOrder):
            pass
    out = voice.finish_live(1, 99, tr)
    assert out["kind"] == "clarify" and voice.gate.prepared() is None
    assert stt.calls.count("final") == 0
    assert nothing_happened(taught)


@pytest.mark.parametrize("flag", ["0", "false", "no", "off", " Off "])
def test_presets_turned_off_are_off_in_voice_integration(taught, fake, spawn, clock, monkeypatch, flag):
    monkeypatch.setenv("ENGRAM_VOICE_PRESETS", flag)
    voice = VoiceIntegration(taught, fake, spawn=spawn, clock=clock)
    voice.on_partial(1, 0, "Open the claw")
    spawn.run_all()
    out = voice.on_final(1, 1, "Open the claw")
    assert out["kind"] == "none" and "turned off" in out["reason"]
    later = voice.on_final(2, 0, "Open the claw after calibration finishes")
    assert later["kind"] == "none" and taught.prospective.list() == []
    assert fake.calls == []                                     # the intent model is never asked
    assert count(taught, "episode") == 0 and taught.robot.calls == OBSERVED_ONLY
    assert voice.on_partial(3, 0, "Stop")["kind"] == "stop"     # a stop still stops
    assert taught.command("Open the gripper")["status"] == "refused"   # nothing was registered


def test_voice_integration_reads_the_setting_exactly_as_the_api_does():
    import engram.api as api
    from engram.spark import presets

    assert api.voice_presets_enabled is presets.voice_presets_enabled


@pytest.mark.parametrize("kind", sorted(REJECTIONS))
def test_after_any_refused_chunk_nothing_of_the_utterance_is_proposed(taught, spawn, clock, kind):
    voice = VoiceIntegration(taught, FakeIntent(), spawn=spawn, clock=clock)
    stt, window = _InstantWhisper(), PcmWindow(max_seconds=5.0)
    tr = _live_partials(voice, 1, window, stt, spawn)           # chunk 0 arrived; "open the gripper" prepared
    assert voice.gate.prepared().decision.preset == "open_gripper"
    with pytest.raises(Exception):
        REJECTIONS[kind](window)                                # refused
    for _ in range(3):                                          # the producer catches it and carries on
        try:
            window.append(SAMPLES)
        except AudioOverflow:
            pass
    out = voice.finish_live(1, 99, tr)
    assert out["kind"] == "clarify" and voice.gate.prepared() is None
    assert stt.calls.count("final") == 0
    assert nothing_happened(taught)
