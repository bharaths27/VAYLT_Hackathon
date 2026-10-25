"""Voice console: intent parsing, and the full iPad -> voice server -> robot path against the simulator."""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot.client import RobotClient  # noqa: E402
from robot.config import Config  # noqa: E402
from robot.server import create_app as robot_app  # noqa: E402
from robot.service import RobotService  # noqa: E402
from robot.sim import SimTag, SimDriver  # noqa: E402
from voice.intents import normalize, parse  # noqa: E402
from voice.server import create_app as voice_app  # noqa: E402

SECRET = "voice-test-secret"


@pytest.mark.parametrize("said, op, args", [
    ("Stop!", "stop", {}),
    ("please STOP the robot now", "stop", {}),
    ("Pick up object seven.", "pick_tagged", {"tag_id": 7}),
    ("grab number 3", "pick_tagged", {"tag_id": 3}),
    ("Put it in zone twenty.", "place_at", {"zone_tag_id": 20}),
    ("Find tag 21", "search_tag", {"tag_id": 21}),
    ("Go to tag eight", "approach_tag", {"tag_id": 8}),
    ("Open the claw", "gripper_open", {}),
    ("close the gripper", "gripper_close", {}),
    ("Turn left.", "turn_bounded", {"angle_deg": 90.0}),
    ("turn right forty five degrees", "turn_bounded", {"angle_deg": -45.0}),
    ("spin around", "turn_bounded", {"angle_deg": 180.0}),
    ("Drive forward 30 centimeters", "drive_bounded", {"distance_m": 0.3}),
    ("move forward", "drive_bounded", {"distance_m": 0.2}),
    ("go forward 5 meters", "drive_bounded", {"distance_m": 1.5}),      # clamped to the robot's limit
    ("back up", "drive_bounded", {"distance_m": -0.1}),
    ("reverse 50 cm", "drive_bounded", {"distance_m": -0.2}),           # blind reverse cap
    ("What do you see?", "observe", {}),
    ("arm to reach", "arm_pose", {"pose": "reach"}),
])
def test_parse(said, op, args):
    r = parse(said, arm_poses=["home", "reach", "carry"])
    assert r["op"] == op and r["args"] == args, r


@pytest.mark.parametrize("said", ["", "[BLANK_AUDIO]", "make me a sandwich", "launch the missiles"])
def test_unknown_is_refused_not_guessed(said):
    assert parse(said)["op"] is None


def test_number_words():
    assert normalize("one hundred eighty") == "180"
    assert normalize("forty-five degrees") == "45 degrees"


class FakeWhisper:
    text = "pick up object seven"

    def check(self):
        return []

    def __call__(self, audio, filename):
        assert audio
        return self.text


@pytest.fixture
def stack(tmp_path):
    cfg = Config()
    d = SimDriver(cfg, tags=[SimTag(7, 0.7, 0.0)])
    svc = RobotService(d, cfg, SECRET, str(tmp_path / "j.jsonl"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(robot_app(svc), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    bot = RobotClient(f"http://127.0.0.1:{port}", SECRET, name="voice-test")
    whisper = FakeWhisper()
    TestClient.bot = bot
    yield TestClient(voice_app(bot, whisper, pin="1234")), d, whisper, svc
    bot.close()
    server.should_exit = True
    svc.close()


def test_voice_to_robot_needs_approval_and_pin(stack):
    c, d, whisper, _ = stack
    r = c.post("/api/transcribe", files={"audio": ("speech.m4a", b"fake-audio", "audio/mp4")}).json()
    assert r["intent"]["op"] == "pick_tagged"        # proposal only: nothing moved yet
    assert d.held is None and abs(d.pose[0]) < 0.001
    assert c.post("/api/execute", json={**r["intent"], "pin": "0000"}).status_code == 403
    res = c.post("/api/execute", json={"op": r["intent"]["op"], "args": r["intent"]["args"], "pin": "1234"}).json()
    assert res["status"] == "SUCCEEDED" and d.held == 7


def test_stop_needs_no_pin_and_latches(stack):
    c, d, _, svc = stack
    assert c.post("/api/stop").status_code == 200
    assert svc.safety.estop_latched
    r = c.post("/api/execute", json={"op": "gripper_close", "args": {}, "pin": "1234"}).json()
    assert r["status"] == "REJECTED" and r["code"] == "E_ESTOP"
    c.post("/api/reset", json={"operator": "ipad", "pin": "1234"})
    assert not svc.safety.estop_latched


def test_page_state_and_camera(stack):
    c, *_ = stack
    assert "Tap to talk" in c.get("/").text
    assert c.get("/api/state").json()["safety"]["supervisor_alive"]
    fr = c.get("/api/frame")
    assert fr.status_code == 200 and fr.content[:2] == b"\xff\xd8"
    assert c.get("/api/config").json() == {"pin_required": True, "speech_ready": True, "missing": [], "mode": "direct"}


def test_mjpeg_stream_sends_multiple_frames(stack):
    c, *_ = stack
    with c.stream("GET", "/api/stream?limit=3&fps=30") as r:
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
        body = b"".join(r.iter_bytes())
    assert body.count(b"--frame") == 3 and body.count(b"\xff\xd8") >= 3



class FakeBrain:
    def __init__(self):
        self.calls = []

    def command(self, text):
        self.calls.append(("command", text))
        return {"kind": "proposal", "operation_id": "ep_1", "status": "proposed", "digest": "d" * 64,
                "plan": {"steps": [{"op": "turn_bounded", "args": {"degrees": 90}}]}, "check": {"errors": []}}

    def approve(self, operation_id, digest):
        self.calls.append(("approve", operation_id, digest))
        return {"operation_id": operation_id, "outcome": "done", "failure": None}

    def stop(self):
        self.calls.append(("stop",))
        return {"stopped": True}


def test_brain_mode_routes_motion_through_the_brain(stack):
    _, d, whisper, svc = stack
    brain = FakeBrain()
    c = TestClient(voice_app(TestClient.bot, whisper, pin="1234", brain=brain))
    assert c.get("/api/config").json()["mode"] == "brain"
    # direct motion is refused: one motion path for the team
    assert c.post("/api/execute", json={"op": "turn_bounded", "args": {"angle_deg": 90}, "pin": "1234"}).status_code == 409
    assert abs(d.pose[2]) < 0.01
    r = c.post("/api/brain/command", json={"text": "turn left"}).json()
    assert r["status"] == "proposed" and brain.calls == [("command", "turn left")]
    assert c.post("/api/brain/approve", json={"operation_id": "ep_1", "digest": "d" * 64, "pin": "0000"}).status_code == 403
    r = c.post("/api/brain/approve", json={"operation_id": "ep_1", "digest": "d" * 64, "pin": "1234"}).json()
    assert r["outcome"] == "done" and brain.calls[-1][0] == "approve"
    # STOP goes straight to the robot (latched) and also tells the brain
    c.post("/api/stop")
    assert svc.safety.estop_latched and brain.calls[-1] == ("stop",)
    svc.reset("test")


# ---- Carter's rule: "ambiguous or misheard command: we stop the task and ask user to please clarify." ----

def robot_busy(monkeypatch, busy=True):
    """The robot server reports an operation in progress (or none)."""
    real = TestClient.bot.state
    monkeypatch.setattr(TestClient.bot, "state",
                        lambda: {**real(), "busy": {"operation_id": "op_1", "op": "drive_bounded"} if busy else None})


def transcribe(c):
    return c.post("/api/transcribe", files={"audio": ("speech.m4a", b"fake-audio", "audio/mp4")}).json()["intent"]


@pytest.mark.parametrize("heard, reason", [
    ("make me a sandwich", 'Sorry, I don\'t know how to do: "make me a sandwich".'),
    ("[BLANK_AUDIO]", "I didn't catch anything."),
], ids=["unclear", "misheard"])
def test_an_unclear_or_misheard_request_while_the_robot_is_busy_stops_it_and_asks(stack, monkeypatch, heard, reason):
    c, d, whisper, svc = stack
    whisper.text = heard
    robot_busy(monkeypatch)
    r = transcribe(c)
    assert r["op"] is None and r["stopped"] is True and r["say"] == f"I stopped. {reason} Please clarify."
    assert svc.safety.estop_latched                     # latched: a reset is needed to move again
    svc.reset("test")


def test_an_unclear_request_while_idle_asks_without_stopping(stack, monkeypatch):
    c, d, whisper, svc = stack
    whisper.text = "make me a sandwich"
    robot_busy(monkeypatch, busy=False)
    r = transcribe(c)
    assert r["stopped"] is False and r["say"] == 'Sorry, I don\'t know how to do: "make me a sandwich". Please clarify.'
    assert not svc.safety.estop_latched                 # no latch for a cough


@pytest.mark.parametrize("heard", ["pick up object seven", "Stop!"])
def test_a_clear_request_or_a_stop_while_busy_is_unchanged(stack, monkeypatch, heard):
    c, d, whisper, svc = stack
    whisper.text = heard
    robot_busy(monkeypatch)
    r = transcribe(c)
    assert r["op"] in ("pick_tagged", "stop") and "stopped" not in r and "clarify" not in r
    assert not svc.safety.estop_latched                 # (the iPad sends the stop itself, straight away)


def test_brain_mode_answers_only_a_misheard_request_here(stack, monkeypatch):
    _, d, whisper, svc = stack
    brain = FakeBrain()
    c = TestClient(voice_app(TestClient.bot, whisper, pin="1234", brain=brain))
    robot_busy(monkeypatch)
    whisper.text = "bring sample b to the bench"         # not parsed here, but the brain may understand it
    r = transcribe(c)
    assert r["op"] is None and "stopped" not in r and not svc.safety.estop_latched
    whisper.text = "[BLANK_AUDIO]"                       # nothing to send to the brain: misheard, answered here
    r = transcribe(c)
    assert r["stopped"] is True and r["say"] == "I stopped. I didn't catch anything. Please clarify."
    assert svc.safety.estop_latched and brain.calls[-1] == ("stop",)
    svc.reset("test")


def test_a_robot_that_cannot_say_whether_it_is_busy_is_stopped(stack, monkeypatch):
    c, d, whisper, svc = stack
    whisper.text = "make me a sandwich"

    def no_state():
        raise OSError("robot server not answering")

    monkeypatch.setattr(TestClient.bot, "state", no_state)
    r = transcribe(c)
    assert r["stopped"] is True and svc.safety.estop_latched    # a needless stop costs one reset
    svc.reset("test")


def test_brain_mode_a_parenthesized_request_is_speech_not_noise(stack, monkeypatch):
    _, d, whisper, svc = stack
    brain = FakeBrain()
    c = TestClient(voice_app(TestClient.bot, whisper, pin="1234", brain=brain))
    robot_busy(monkeypatch)
    whisper.text = "(don't open the claw)"               # words in brackets: for the brain, which refuses "don't"
    r = transcribe(c)
    assert "stopped" not in r and not svc.safety.estop_latched and ("stop",) not in brain.calls


@pytest.mark.parametrize("state", [{"safety": {}}, {"busy": "unknown"}, {"busy": 1}],
                         ids=["no busy key", "a busy value it does not know", "a number"])
def test_a_robot_that_does_not_say_it_is_idle_counts_as_busy(stack, monkeypatch, state):
    c, d, whisper, svc = stack
    whisper.text = "make me a sandwich"
    monkeypatch.setattr(TestClient.bot, "state", lambda: state)
    r = transcribe(c)
    assert r["stopped"] is True and svc.safety.estop_latched    # only "null" (or false) says idle
    svc.reset("test")
