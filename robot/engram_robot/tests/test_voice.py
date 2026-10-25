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
