import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import voice_bridge  # noqa: E402
from engram.api import create_app  # noqa: E402


def test_dry_sends_nothing():
    out = voice_bridge.handle("[BLANK_AUDIO] bring the beaker to the bench ", "http://127.0.0.1:9", dry=True)
    assert "heard: bring the beaker to the bench" in out and "(dry)" in out


def test_task_by_voice_becomes_a_proposal(engine):
    from fastapi.testclient import TestClient

    client = TestClient(create_app(engine))

    def fake_send(text, brain_url, dry=False):
        return client.post("/commands", json={"text": text, "who": "voice"}).json()

    voice_bridge.send, real = fake_send, voice_bridge.send
    try:
        out = voice_bridge.handle("bring the beaker to the bench", "unused", dry=False)
    finally:
        voice_bridge.send = real
    assert "waiting for approval" in out or "proposed" in out
    assert engine.robot.state().get("holding") in (None, "")


def test_a_spoken_clip_with_nothing_in_it_is_still_sent_but_a_typed_blank_is_not():
    out = voice_bridge.handle("[BLANK_AUDIO]", "http://127.0.0.1:9", dry=True, spoken=True)
    assert "heard: (nothing)" in out and "'text': ''" in out      # the brain answers a misheard command
    assert voice_bridge.handle("   ", "http://127.0.0.1:9", dry=True) == "heard nothing"


def test_a_misheard_clip_gets_the_brains_please_clarify(engine):
    from fastapi.testclient import TestClient

    client = TestClient(create_app(engine))

    def fake_send(text, brain_url, dry=False):
        return client.post("/commands", json={"text": text, "who": "voice"}).json()

    voice_bridge.send, real = fake_send, voice_bridge.send
    try:
        out = voice_bridge.handle("(coughs)", "unused", dry=False, spoken=True)
    finally:
        voice_bridge.send = real
    assert out.endswith("I didn't catch anything. Please clarify.")


def test_words_in_brackets_are_sent_not_dropped():
    out = voice_bridge.handle("(don't open the claw) [BLANK_AUDIO]", "http://127.0.0.1:9", dry=True, spoken=True)
    heard, sent = out.splitlines()
    assert heard == "heard: (don't open the claw)" and "(don't open the claw)" in sent   # the refusal reaches the brain


def test_the_noise_markers_are_the_same_list_everywhere():
    import importlib.util
    from pathlib import Path

    from console.server import voice as console_voice

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("robot_intents", root / "robot/engram_robot/voice/intents.py")
    robot_intents = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(robot_intents)
    for other in (robot_intents, voice_bridge):
        assert other.NON_SPEECH == console_voice.NON_SPEECH
        assert other.NON_SPEECH_DESCRIBERS == console_voice.NON_SPEECH_DESCRIBERS
