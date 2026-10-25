"""Acceptance checks from the R-packet, run against the simulator.

    pytest -v tests/            (~1 min, real-time physics)

Each test name maps to a packet requirement so results can go straight into the evidence log.
"""
import json
import math
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot import auth  # noqa: E402
from robot.config import Config  # noqa: E402
from robot.server import create_app  # noqa: E402
from robot.service import RobotService  # noqa: E402
from robot.sim import SimDriver, SimTag, default_world  # noqa: E402

SECRET = "test-secret"
_n = 0


def oid(prefix="op"):
    global _n
    _n += 1
    return f"{prefix}-{_n:04d}-{time.time_ns()}"


def run(svc, op, args=None, op_id=None, signed=True):
    args = args or {}
    op_id = op_id or oid(op)
    tok = auth.sign(SECRET, op_id, op, args) if signed else None
    return svc.execute(op, args, op_id, tok)


@pytest.fixture
def make(tmp_path):
    made = []

    def _make(driver=None, journal=None, **world):
        cfg = Config()
        d = driver or (SimDriver(cfg, **world) if world else default_world(cfg))
        svc = RobotService(d, cfg, SECRET, str(journal or tmp_path / "journal.jsonl"))
        made.append(svc)
        return svc, d

    yield _make
    for s in made:
        s.close()


# ---------------------------------------------------------------- acceptance 1: obstacle + watchdog
def test_A1_lidar_vetoes_drive_into_obstacle(make):
    svc, d = make(obstacles=[(0.8, 0.0, 0.10)])
    code, r = run(svc, "drive_bounded", {"distance_m": 1.5, "speed_mps": 0.2})
    assert r["status"] == "VETOED" and r["code"] == "E_LIDAR_VETO"
    assert not d.collided
    gap = 0.8 - 0.10 - d.pose[0]  # LiDAR (robot centre) to obstacle surface
    assert gap >= svc.cfg.safety_margin_m, gap
    assert d.velocity() == (0.0, 0.0)


def test_A1_supervisor_stops_robot_even_if_controller_ignores_obstacles(make):
    """A buggy/stalled controller keeps the lease alive but never checks LiDAR: supervisor still stops."""
    svc, d = make(obstacles=[(0.6, 0.0, 0.10)])
    svc.safety.command(0.2, 0.0)
    stop = threading.Event()

    def bad_controller():
        while not stop.is_set():
            svc.safety._lease_until = time.monotonic() + 0.5  # renews lease, bypasses all checks
            time.sleep(0.02)

    threading.Thread(target=bad_controller, daemon=True).start()
    time.sleep(4.0)
    stop.set()
    assert svc.safety.trip and svc.safety.trip[0] == "E_LIDAR_VETO"
    assert not d.collided and d.velocity() == (0.0, 0.0)


def test_A1_watchdog_stops_when_control_loop_dies(make):
    svc, d = make()
    svc.safety.command(0.1, 0.0)  # one command, then the "planner/controller" goes silent
    time.sleep(svc.cfg.lease_s + 0.3)
    assert d.velocity() == (0.0, 0.0)
    assert svc.safety.trip[0] == "E_WATCHDOG"


def test_A1_stale_lidar_means_no_motion(make):
    svc, d = make()
    d.lidar_fail = True
    time.sleep(0.7)
    code, r = run(svc, "drive_bounded", {"distance_m": 0.3})
    assert r["status"] == "ABORTED" and r["code"] == "E_LIDAR_STALE"
    assert abs(d.pose[0]) < 0.01


def test_A1_estop_aborts_motion_latches_and_requires_reset(make):
    svc, d = make()
    out = {}
    t = threading.Thread(target=lambda: out.update(r=run(svc, "drive_bounded", {"distance_m": 1.0})[1]))
    t.start()
    time.sleep(1.0)
    svc.emergency_stop("test button")
    t.join(5)
    assert out["r"]["status"] == "ABORTED" and out["r"]["code"] == "E_ESTOP"
    x = d.pose[0]
    time.sleep(0.3)
    assert abs(d.pose[0] - x) < 0.002
    code, r = run(svc, "drive_bounded", {"distance_m": 0.2})
    assert (code, r["status"], r["code"]) == (423, "REJECTED", "E_ESTOP")
    svc.reset("deniz")
    code, r = run(svc, "drive_bounded", {"distance_m": 0.2})
    assert r["status"] == "SUCCEEDED"


# ---------------------------------------------------------------- acceptance 2: deliver marked object
def test_A2_pick_and_place_tagged_object_with_sensor_evidence(make):
    svc, d = make(tags=[SimTag(7, 0.8, 0.05), SimTag(20, 1.6, 0.4, kind="zone", lidar_radius=0.04)])
    _, r = run(svc, "pick_tagged", {"tag_id": 7})
    assert r["status"] == "SUCCEEDED", r
    ev = r["postcondition"]["evidence"]
    assert abs(ev["tag_distance_change_m"]) <= 0.03 and d.held == 7
    _, r = run(svc, "place_at", {"zone_tag_id": 20})
    assert r["status"] == "SUCCEEDED", r
    assert d.held is None and svc.held_tag is None
    assert math.hypot(d.tags[7].x - 1.6, d.tags[7].y - 0.4) < 0.4  # object ended up at the zone
    assert r["timing"]["duration_ms"] > 0


# ---------------------------------------------------------------- acceptance 3: honest grasp state
def test_A3_missed_grasp_is_reported_failed(make):
    svc, d = make(tags=[SimTag(7, 0.7, 0.0)], grasp_miss_prob=1.0)
    _, r = run(svc, "pick_tagged", {"tag_id": 7})
    assert (r["status"], r["code"]) == ("FAILED", "E_GRASP_FAILED")
    assert d.held is None and svc.held_tag is None and d.claw_open


def test_A3_unverifiable_grasp_is_unknown_not_success(make):
    svc, d = make(tags=[SimTag(7, 0.7, 0.0)])
    orig = d.set_gripper

    def close_and_occlude(open_):
        orig(open_)
        d.occlude_held = not open_  # claw hides the tag once closed
    d.set_gripper = close_and_occlude
    _, r = run(svc, "pick_tagged", {"tag_id": 7})
    assert r["status"] == "UNKNOWN" and r["code"] == "E_GRASP_UNVERIFIED"
    assert svc.held_tag is None


def test_A3_place_without_verified_hold_does_not_move(make):
    svc, d = make()
    _, r = run(svc, "place_at", {"zone_tag_id": 20})
    assert r["code"] == "E_NOT_HOLDING" and r["status"] != "SUCCEEDED"
    assert abs(d.pose[0]) < 0.001


# ---------------------------------------------------------------- authority / allowlist
def test_physical_ops_require_matching_unexpired_authorization(make):
    svc, d = make()
    op_id = oid()
    assert run(svc, "gripper_close", op_id=op_id, signed=False)[1]["code"] == "E_AUTH"
    old = auth.sign(SECRET, op_id, "gripper_close", {}, ttl_s=5, now=time.time() - 10)
    assert svc.execute("gripper_close", {}, op_id, old)[1]["message"] == "authorization expired"
    tok = auth.sign(SECRET, op_id, "drive_bounded", {"distance_m": 0.1})
    assert svc.execute("drive_bounded", {"distance_m": 1.0}, op_id, tok)[1]["message"] == \
        "args differ from what was approved"
    forged = auth.sign("wrong-secret", op_id, "gripper_close", {})
    assert svc.execute("gripper_close", {}, op_id, forged)[1]["message"] == "bad signature"
    assert d.claw_open  # nothing happened


def test_read_only_ops_need_no_authorization(make):
    svc, _ = make()
    code, r = run(svc, "observe", signed=False)
    assert code == 200 and r["status"] == "SUCCEEDED" and len(r["data"]["tags"]) >= 2


def test_unknown_ops_and_out_of_bounds_args_are_rejected(make):
    svc, d = make()
    assert run(svc, "exec_python", {"code": "motors.go()"})[1]["code"] == "E_UNKNOWN_OP"
    assert run(svc, "drive_bounded", {"distance_m": 5.0})[1]["code"] == "E_ARGS"
    assert run(svc, "drive_bounded", {"distance_m": 0.2, "speed_mps": 2.0})[1]["code"] == "E_ARGS"
    assert run(svc, "turn_bounded", {"angle_deg": 90, "torque": 9})[1]["code"] == "E_ARGS"
    assert run(svc, "pick_tagged", {"tag_id": True})[1]["code"] == "E_ARGS"
    assert abs(d.pose[0]) < 0.001


# ---------------------------------------------------------------- idempotency / restart
def test_duplicate_operation_id_never_executes_twice(make, tmp_path):
    svc, d = make()
    op_id = oid()
    _, r1 = run(svc, "drive_bounded", {"distance_m": 0.2}, op_id=op_id)
    x = d.pose[0]
    _, r2 = run(svc, "drive_bounded", {"distance_m": 0.2}, op_id=op_id)
    assert r2["replayed"] and r2["status"] == r1["status"] and abs(d.pose[0] - x) < 0.002
    starts = [json.loads(line) for line in open(tmp_path / "journal.jsonl")]
    assert sum(1 for s in starts if s["phase"] == "started" and s["operation_id"] == op_id) == 1


def test_interrupted_op_becomes_unknown_after_restart_and_is_not_replayed(make, tmp_path):
    j = tmp_path / "crash.jsonl"
    j.write_text(json.dumps({"ts": 1, "phase": "started", "operation_id": "op-crashed-0001",
                             "op": "drive_bounded", "args": {"distance_m": 0.5}}) + "\n")
    svc, d = make(journal=j)
    assert "op-crashed-0001" in svc.journal.recovered
    _, r = run(svc, "drive_bounded", {"distance_m": 0.5}, op_id="op-crashed-0001")
    assert r["status"] == "UNKNOWN" and r["code"] == "E_INTERRUPTED" and r["replayed"]
    assert abs(d.pose[0]) < 0.001


# ---------------------------------------------------------------- primitives + HTTP contract
def test_turn_and_drive_report_verified_postconditions(make):
    svc, d = make()
    _, r = run(svc, "turn_bounded", {"angle_deg": 90})
    assert r["status"] == "SUCCEEDED" and abs(math.degrees(d.pose[2]) - 90) < 3
    _, r = run(svc, "drive_bounded", {"distance_m": 0.3})
    assert r["status"] == "SUCCEEDED" and abs(d.pose[1] - 0.3) < 0.02


def test_search_then_approach(make):
    svc, d = make(tags=[SimTag(9, -0.9, 0.2)])  # behind the robot
    assert run(svc, "detect_tag", {"tag_id": 9})[1]["code"] == "E_TAG_NOT_FOUND"
    _, r = run(svc, "search_tag", {"tag_id": 9})
    assert r["status"] == "SUCCEEDED"
    _, r = run(svc, "approach_tag", {"tag_id": 9, "stop_distance_m": 0.3})
    assert r["status"] == "SUCCEEDED", r
    assert abs(math.hypot(d.pose[0] + 0.9, d.pose[1] - 0.2) - 0.3) < 0.04


def test_http_contract(make):
    svc, _ = make()
    c = TestClient(create_app(svc))
    caps = c.get("/robot/capabilities").json()
    ops = {p["op"] for p in caps["primitives"]}
    assert {"stop", "drive_bounded", "turn_bounded", "observe", "detect_tag", "approach_tag",
            "gripper_open", "gripper_close", "pick_tagged", "place_at"} <= ops
    st = c.get("/robot/state").json()
    assert st["safety"]["supervisor_alive"] and st["pose"] is not None
    fr = c.get("/robot/frame")
    assert fr.status_code == 200 and fr.headers["content-type"] == "image/jpeg"
    op_id = oid()
    body = {"op": "gripper_close", "args": {}, "operation_id": op_id}
    assert c.post("/robot/execute", json=body).status_code == 403
    body["authorization"] = auth.sign(SECRET, op_id, "gripper_close", {})
    r = c.post("/robot/execute", json=body)
    assert r.status_code == 200 and r.json()["status"] == "SUCCEEDED"
    assert c.post("/robot/stop").json()["latched"] is True
    assert c.post("/robot/reset", json={"operator": "brian"}).json()["cleared"] is True


# ---------------------------------------------------------------- forward ultrasonic instead of LiDAR
def test_US_ultrasonic_vetoes_forward_drive_into_obstacle(make):
    svc, d = make(obstacles=[(0.8, 0.0, 0.10)], range_sensor="ultrasonic")
    _, r = run(svc, "drive_bounded", {"distance_m": 1.5, "speed_mps": 0.2})
    assert r["status"] == "VETOED" and not d.collided
    assert 0.8 - 0.10 - d.pose[0] >= svc.cfg.safety_margin_m


def test_US_rear_is_unsensed_so_reverse_is_slow_short_and_capped(make):
    svc, d = make(range_sensor="ultrasonic")
    assert svc.state()["safety"]["rear_sensed"] is False
    _, r = run(svc, "drive_bounded", {"distance_m": -0.5})
    assert r["code"] == "E_ARGS" and abs(d.pose[0]) < 0.001       # too far blind: refused, no motion
    _, r = run(svc, "drive_bounded", {"distance_m": -0.15, "speed_mps": 0.2})
    assert r["status"] == "SUCCEEDED" and r["timing"]["duration_ms"] > 2500  # capped at 0.05 m/s
    svc.safety.begin_op()
    with pytest.raises(Exception) as e:
        svc.safety.command(-0.2, 0.0)                                # fast blind reverse refused
    assert e.value.code == "E_UNSENSED"


def test_US_pick_and_place_still_verified_with_ultrasonic(make):
    svc, d = make(tags=[SimTag(7, 0.8, 0.05), SimTag(20, 1.6, 0.4, kind="zone", lidar_radius=0.04)],
                  range_sensor="ultrasonic")
    assert run(svc, "pick_tagged", {"tag_id": 7})[1]["status"] == "SUCCEEDED"
    assert run(svc, "place_at", {"zone_tag_id": 20})[1]["status"] == "SUCCEEDED"


# ---------------------------------------------------------------- arm poses
def test_arm_pose_allowlisted_names_only_and_reported_unverified(make):
    svc, d = make()
    _, r = run(svc, "arm_pose", {"pose": "reach"})
    assert r["status"] == "UNKNOWN" and d.arm_pose == "reach"   # no joint feedback -> never SUCCEEDED
    assert run(svc, "arm_pose", {"pose": "flail"})[1]["code"] == "E_ARGS"
    assert run(svc, "arm_pose", {"pose": 3})[1]["code"] == "E_ARGS"
    caps = {p["op"]: p for p in svc.capabilities()["primitives"]}
    assert caps["arm_pose"]["args"][0]["choices"] == ["carry", "home", "look", "reach"]


def test_pick_uses_reach_and_carry_poses(make):
    svc, d = make(tags=[SimTag(7, 0.7, 0.0)])
    _, r = run(svc, "pick_tagged", {"tag_id": 7})
    ev = r["postcondition"]["evidence"]
    assert r["status"] == "SUCCEEDED" and ev["arm_reach"] and ev["arm_carry"] and d.arm_pose == "carry"
