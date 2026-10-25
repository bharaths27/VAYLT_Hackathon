"""Brain Pi <-> bot Pi over real HTTP: client library, approval gate, heartbeat watchdog."""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot.client import RobotClient  # noqa: E402
from robot.config import Config  # noqa: E402
from robot.server import create_app  # noqa: E402
from robot.service import RobotService  # noqa: E402
from robot.sim import default_world  # noqa: E402

SECRET = "link-test-secret"


@pytest.fixture
def bot(tmp_path):
    cfg = Config()
    d = default_world(cfg)
    svc = RobotService(d, cfg, SECRET, str(tmp_path / "j.jsonl"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", svc, d
    server.should_exit = True
    svc.close()


def test_brain_reads_and_needs_approval_for_motion(bot):
    url, svc, d = bot
    brain = RobotClient(url, SECRET, name="brain-test")
    try:
        assert brain.execute("observe")["status"] == "SUCCEEDED"
        r = brain.execute("drive_bounded", {"distance_m": 0.2})              # not approved
        assert r["status"] == "REJECTED" and r["code"] == "E_AUTH" and abs(d.pose[0]) < 0.001
        r = brain.execute("drive_bounded", {"distance_m": 0.2}, approved=True)
        assert r["status"] == "SUCCEEDED" and abs(d.pose[0] - 0.2) < 0.03
        time.sleep(0.5)
        assert brain.state()["safety"]["brain_link"]["ok"] is True
    finally:
        brain.close()


def test_robot_stops_when_brain_goes_silent(bot):
    url, svc, d = bot
    brain = RobotClient(url, SECRET, name="brain-test")
    time.sleep(0.5)
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=brain.execute("drive_bounded", {"distance_m": 1.2, "speed_mps": 0.1}, approved=True)))
    t.start()
    time.sleep(1.5)
    brain._running = False          # simulate the brain crashing / losing the cable: heartbeats stop
    t.join(10)
    assert out["r"]["status"] == "ABORTED" and out["r"]["code"] == "E_LINK_LOST"
    assert d.pose[0] < 0.6 and d.velocity() == (0.0, 0.0)


def test_clean_disconnect_releases_link(bot):
    url, svc, d = bot
    brain = RobotClient(url, SECRET, name="brain-test")
    time.sleep(0.5)
    brain.close()
    time.sleep(svc.cfg.link_timeout_s + 0.3)
    assert svc.state()["safety"]["brain_link"] is None   # manual use still works afterwards
