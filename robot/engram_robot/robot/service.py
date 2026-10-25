"""RobotService: the allowlisted primitive registry and the execute() contract.

Nothing outside this file can move the robot: the HTTP layer only calls execute()/emergency_stop(),
and every primitive moves through SafetySupervisor.command(). There is no eval, no free-form
motor command, no model-generated code path.

Result status (only SUCCEEDED means a sensor confirmed the postcondition):
  SUCCEEDED  postcondition verified by a sensor (camera / odometry / gripper sensor)
  FAILED     executed; a sensor showed the postcondition does NOT hold (or a precondition failed)
  UNKNOWN    executed; effect could not be verified -> planner must not assume success
  VETOED     LiDAR envelope stopped the motion
  ABORTED    e-stop, watchdog, stale LiDAR or supervisor fault stopped the motion
  REJECTED   never started (unknown op, bad args, bad/missing authorization, busy, e-stop latched)
"""
import logging
import math
import statistics
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from . import auth
from .config import Config
from .driver import RobotDriver
from .journal import Journal
from .safety import SafetyStop, SafetySupervisor
from .vision import TagDetector

log = logging.getLogger("robot.service")
REQUIRED = object()

REJECT_HTTP = {"E_UNKNOWN_OP": 400, "E_ARGS": 422, "E_BAD_OPERATION_ID": 422,
               "E_AUTH": 403, "E_BUSY": 409, "E_ESTOP": 423}


class Fail(Exception):
    def __init__(self, code, message, status="FAILED", evidence=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status, self.evidence = code, message, status, evidence or {}


@dataclass
class Arg:
    type: type
    default: Any = REQUIRED
    min: float | None = None
    max: float | None = None
    doc: str = ""
    choices: Any = None  # for str args: list or zero-arg callable returning the allowed values

    def allowed(self):
        return list(self.choices() if callable(self.choices) else (self.choices or []))

    def describe(self, name):
        d = {"name": name, "type": self.type.__name__, "required": self.default is REQUIRED, "doc": self.doc}
        if self.type is str:
            d["choices"] = self.allowed()
        if self.default is not REQUIRED:
            d["default"] = self.default
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        return d


@dataclass
class Spec:
    name: str
    fn: Callable
    args: dict
    physical: bool
    timeout_s: float
    effect: str
    postcondition: str
    codes: list = field(default_factory=list)

    def describe(self):
        return {"op": self.name, "args": [a.describe(n) for n, a in self.args.items()],
                "physical_effect": self.physical, "requires_authorization": self.physical,
                "timeout_s": self.timeout_s, "effect": self.effect, "postcondition": self.postcondition,
                "failure_codes": self.codes}


def outcome(status, code, message, description, verified, holds, evidence=None, data=None):
    return {"status": status, "code": code, "message": message,
            "postcondition": {"description": description, "verified": verified, "holds": holds,
                              "evidence": evidence or {}},
            "data": data or {}}


class RobotService:
    def __init__(self, driver: RobotDriver, cfg: Config, secret: str, journal_path: str | None = None):
        self.d, self.cfg, self.secret = driver, cfg, secret
        self.safety = SafetySupervisor(driver, cfg)
        self.detector = TagDetector(cfg)
        self.journal = Journal(journal_path or cfg.journal_path)
        self._exec_lock = threading.Lock()
        self.current_op: dict | None = None
        self.held_tag: int | None = None  # set only by a SUCCEEDED pick_tagged
        self.specs = self._build_specs()
        time.sleep(2.5 / cfg.tick_hz)  # let the supervisor take its first LiDAR scan

    # ======================================================================= registry
    def _build_specs(self) -> dict[str, Spec]:
        c = self.cfg
        tag = Arg(int, min=0, max=49, doc="ArUco id (DICT_4X4_50)")
        spd = Arg(float, 0.15, 0.02, c.max_speed, "m/s")
        S = [
            Spec("stop", self.p_stop, {}, False, 2, "zero wheel velocity (soft stop, not latched)",
                 "robot stationary (odometry over 0.3 s)", ["E_UNVERIFIED"]),
            Spec("drive_bounded", self.p_drive, {"distance_m": Arg(float, min=-c.max_drive_m, max=c.max_drive_m,
                                                                     doc="+ forward, - reverse"), "speed_mps": spd},
                 True, 20, "drive straight a bounded distance",
                 "odometry displacement within 0.02 m of distance_m (UNKNOWN without odometry)",
                 ["E_LIDAR_VETO", "E_WATCHDOG", "E_TIMEOUT", "E_POSTCONDITION", "E_UNVERIFIED"]),
            Spec("turn_bounded", self.p_turn, {"angle_deg": Arg(float, min=-180, max=180, doc="+ = left/CCW"),
                                                "rate_deg_s": Arg(float, 45.0, 5, math.degrees(c.max_turn_rate))},
                 True, 20, "rotate in place a bounded angle",
                 "odometry heading change within 3 deg of angle_deg (UNKNOWN without odometry)",
                 ["E_WATCHDOG", "E_TIMEOUT", "E_POSTCONDITION", "E_UNVERIFIED"]),
            Spec("observe", self.p_observe, {}, False, 3, "capture a frame and list visible tags",
                 "frame captured; data.tags lists every detected tag", ["E_CAMERA"]),
            Spec("detect_tag", self.p_detect, {"tag_id": tag}, False, 3, "look for one tag without moving",
                 "tag visible in >=2 of 3 frames; data has distance/bearing", ["E_TAG_NOT_FOUND", "E_CAMERA"]),
            Spec("search_tag", self.p_search, {"tag_id": tag, "max_turn_deg": Arg(float, 360.0, 30, 360)},
                 True, 40, "rotate in 30 deg steps until the tag is seen",
                 "tag visible after search", ["E_TAG_NOT_FOUND", "E_WATCHDOG", "E_TIMEOUT"]),
            Spec("approach_tag", self.p_approach, {"tag_id": tag,
                                                    "stop_distance_m": Arg(float, 0.30, c.grasp_standoff_m, 1.0),
                                                    "speed_mps": spd},
                 True, 40, "visual-servo toward a visible tag and stop at stop_distance_m",
                 f"tag at stop_distance_m +/- {2 * c.approach_dist_tol_m} m and centred within "
                 f"{c.grasp_bearing_tol_deg} deg",
                 ["E_TAG_NOT_FOUND", "E_TAG_LOST", "E_LIDAR_VETO", "E_WATCHDOG", "E_TIMEOUT"]),
            Spec("gripper_open", self.p_gripper_open, {}, True, 3, "open the claw",
                 "gripper reports open (UNKNOWN without a gripper sensor)", ["E_UNVERIFIED"]),
            Spec("gripper_close", self.p_gripper_close, {}, True, 3, "close the claw",
                 "gripper reports closed; does NOT imply an object is held", ["E_UNVERIFIED"]),
            Spec("arm_pose", self.p_arm_pose, {"pose": Arg(str, choices=self.d.arm_pose_names,
                                                           doc="a saved pose name")},
                 True, 5, "move the arm to a saved pose (claw unchanged)",
                 "UNKNOWN: arm servos have no position feedback", ["E_UNVERIFIED"]),
            Spec("pick_tagged", self.p_pick, {"tag_id": tag}, True, 60,
                 "open claw, approach tagged object to grasp standoff, close, reverse to verify",
                 "object's tag stays at constant distance while the robot reverses (it travels with the robot)",
                 ["E_TAG_NOT_FOUND", "E_TAG_LOST", "E_GRASP_FAILED", "E_GRASP_UNVERIFIED",
                  "E_EVIDENCE_CONFLICT", "E_LIDAR_VETO", "E_WATCHDOG", "E_TIMEOUT"]),
            Spec("place_at", self.p_place, {"zone_tag_id": tag,
                                             "object_tag_id": Arg(int, None, 0, 49,
                                                                  "defaults to the verified held object")},
                 True, 60, "approach zone marker, release, reverse to verify",
                 "object's tag recedes as the robot reverses (it stayed at the zone)",
                 ["E_NOT_HOLDING", "E_TAG_NOT_FOUND", "E_TAG_LOST", "E_RELEASE_FAILED", "E_PLACE_UNVERIFIED",
                  "E_LIDAR_VETO", "E_WATCHDOG", "E_TIMEOUT"]),
        ]
        return {s.name: s for s in S}

    def capabilities(self) -> dict:
        c = self.cfg
        return {
            "primitives": [s.describe() for s in self.specs.values()],
            "statuses": {"SUCCEEDED": "postcondition verified by a sensor",
                         "FAILED": "sensor shows postcondition false, or precondition failed",
                         "UNKNOWN": "effect not verifiable; do not assume success",
                         "VETOED": "LiDAR envelope stopped motion", "ABORTED": "e-stop / watchdog / stale LiDAR",
                         "REJECTED": "not started (args, auth, busy, unknown op, e-stop latched)"},
            "limits": {"max_speed_mps": c.max_speed, "max_turn_rate_radps": c.max_turn_rate,
                       "max_drive_m": c.max_drive_m, "lease_s": c.lease_s,
                       "envelope_m": {"creep": round(c.envelope(c.creep_speed), 3),
                                      "max": round(c.envelope(c.max_speed), 3)},
                       "auth_ttl_max_s": c.auth_ttl_max_s},
            "camera": {"width": c.cam_width, "height": c.cam_height, "focal_px": round(c.focal_px, 2),
                       "aruco_dict": c.aruco_dict, "default_tag_size_m": c.default_tag_size_m,
                       "tag_sizes_m": c.tag_sizes_m, "grasp_standoff_m": c.grasp_standoff_m},
            "driver": self.d.info(),
        }

    # ======================================================================= execute
    def execute(self, op: str, args: dict | None, operation_id: str, authorization: str | None = None):
        args = {} if args is None else args
        if not isinstance(operation_id, str) or not (8 <= len(operation_id) <= 128):
            return self._reject(op, operation_id, "E_BAD_OPERATION_ID", "operation_id must be 8-128 chars")
        if operation_id in self.journal.results:
            r = dict(self.journal.results[operation_id])
            r["replayed"] = True  # idempotent: never re-executes
            return 200, r
        spec = self.specs.get(op)
        if spec is None:
            return self._reject(op, operation_id, "E_UNKNOWN_OP", f"'{op}' is not an allowlisted primitive")
        try:
            clean = self._validate(spec, args)
        except Fail as f:
            return self._reject(op, operation_id, f.code, f.message)
        if spec.physical:
            ok, why = auth.verify(self.secret, authorization, operation_id, op, args, self.cfg.auth_ttl_max_s)
            if not ok:
                return self._reject(op, operation_id, "E_AUTH", why)
            if self.safety.estop_latched:
                return self._reject(op, operation_id, "E_ESTOP", "e-stop latched; operator must POST /robot/reset")
        if not self._exec_lock.acquire(blocking=False):
            busy = self.current_op["operation_id"] if self.current_op else "?"
            return self._reject(op, operation_id, "E_BUSY", f"operation {busy} in progress")
        started_at, t0 = time.time(), time.monotonic()
        try:
            self.current_op = {"operation_id": operation_id, "op": op, "started_at": started_at}
            self.journal.started(operation_id, op, args)
            self.safety.begin_op()
            deadline = t0 + spec.timeout_s
            try:
                res = spec.fn(deadline=deadline, **clean)
            except SafetyStop as s:
                st = "VETOED" if s.code in ("E_LIDAR_VETO", "E_UNSENSED") else "ABORTED"
                res = outcome(st, s.code, s.message, spec.postcondition, False, None)
            except Fail as f:
                res = outcome(f.status, f.code, f.message, spec.postcondition,
                              bool(f.evidence), False if f.evidence else None, f.evidence)
            except Exception as e:
                log.error(traceback.format_exc())
                res = outcome("FAILED", "E_HW", f"{type(e).__name__}: {e}", spec.postcondition, False, None)
            finally:
                self.safety.halt()
            t1 = time.monotonic()
            res.update({"operation_id": operation_id, "op": op, "args": clean,
                        "timing": {"started_at": started_at, "ended_at": time.time(),
                                   "duration_ms": round((t1 - t0) * 1000, 1)},
                        "safety": {"estop_latched": self.safety.estop_latched}})
            self.journal.finished(res)
            return 200, res
        finally:
            self.current_op = None
            self._exec_lock.release()

    def _reject(self, op, operation_id, code, message):
        return REJECT_HTTP.get(code, 400), {"operation_id": operation_id, "op": op, "status": "REJECTED",
                                            "code": code, "message": message}

    @staticmethod
    def _validate(spec: Spec, args: dict) -> dict:
        if not isinstance(args, dict):
            raise Fail("E_ARGS", "args must be an object")
        extra = set(args) - set(spec.args)
        if extra:
            raise Fail("E_ARGS", f"unknown args {sorted(extra)}")
        out = {}
        for name, a in spec.args.items():
            val = args.get(name)
            if val is None:
                if a.default is REQUIRED:
                    raise Fail("E_ARGS", f"missing required arg '{name}'")
                out[name] = a.default
                continue
            if a.type is str:
                if not isinstance(val, str) or val not in a.allowed():
                    raise Fail("E_ARGS", f"'{name}' must be one of {a.allowed()}")
                out[name] = val
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise Fail("E_ARGS", f"'{name}' must be a number")
            if a.type is int:
                if isinstance(val, float) and not val.is_integer():
                    raise Fail("E_ARGS", f"'{name}' must be an integer")
                val = int(val)
            else:
                val = float(val)
                if not math.isfinite(val):
                    raise Fail("E_ARGS", f"'{name}' must be finite")
            if (a.min is not None and val < a.min) or (a.max is not None and val > a.max):
                raise Fail("E_ARGS", f"'{name}'={val} outside [{a.min}, {a.max}]")
            out[name] = val
        return out

    def emergency_stop(self, reason="operator e-stop", latch=True):
        if latch:
            self.safety.estop(reason)
        else:
            self.safety.halt()

    def reset(self, operator: str) -> bool:
        return self.safety.reset(operator)

    # ======================================================================= helpers
    def _tick(self):
        time.sleep(1.0 / self.cfg.tick_hz)

    @staticmethod
    def _check(deadline):
        if time.monotonic() > deadline:
            raise Fail("E_TIMEOUT", "primitive exceeded its timeout")

    def _frame(self):
        f = self.d.get_frame()
        if f is None:
            raise Fail("E_CAMERA", "no camera frame")
        return f

    def _detect(self):
        return self.detector.detect(self._frame())

    def _measure(self, tag_id, n=3):
        """Median forward distance over n frames; None if seen in fewer than 2."""
        seen = []
        for _ in range(n):
            d = self._detect().get(tag_id)
            if d:
                seen.append(d.distance_m)
            self._tick()
        return round(statistics.median(seen), 4) if len(seen) >= 2 else None

    def _drive(self, distance, speed, deadline):
        sign, target = (1 if distance > 0 else -1), abs(distance)
        if sign < 0 and not self.safety.sensed(math.pi):  # forward-only range sensor: blind reverse
            if target > self.cfg.blind_reverse_max_m + 1e-9:
                raise Fail("E_ARGS", f"rear is unsensed; reverse limited to {self.cfg.blind_reverse_max_m} m")
            speed = min(speed, self.cfg.blind_reverse_max_speed)
        odo0 = self.d.odometry()
        if odo0 is None:
            t_end = time.monotonic() + target / speed
            while time.monotonic() < t_end:
                self._check(deadline)
                self.safety.command(sign * speed, 0.0)
                self._tick()
            self.safety.halt()
            return {"method": "open_loop_timing", "traveled_m": None}
        x0, y0, th0 = odo0

        def traveled():
            x, y, _ = self.d.odometry()
            return sign * ((x - x0) * math.cos(th0) + (y - y0) * math.sin(th0))

        while True:
            self._check(deadline)
            rem = target - traveled()
            if rem <= 0.003:
                break
            self.safety.command(sign * min(speed, max(0.02, rem * 2)), 0.0)
            self._tick()
        self.safety.halt()
        time.sleep(0.05)
        return {"method": "odometry", "traveled_m": round(traveled(), 4)}

    def _turn(self, angle, rate, deadline):
        sign, target = (1 if angle > 0 else -1), abs(angle)
        odo = self.d.odometry()
        if odo is None:
            t_end = time.monotonic() + target / rate
            while time.monotonic() < t_end:
                self._check(deadline)
                self.safety.command(0.0, sign * rate)
                self._tick()
            self.safety.halt()
            return {"method": "open_loop_timing", "turned_rad": None}
        turned, last = 0.0, odo[2]
        while True:
            self._check(deadline)
            th = self.d.odometry()[2]
            turned += sign * ((th - last + math.pi) % (2 * math.pi) - math.pi)
            last = th
            rem = target - turned
            if rem <= 0.005:
                break
            self.safety.command(0.0, sign * min(rate, max(0.1, rem * 2)))
            self._tick()
        self.safety.halt()
        time.sleep(0.05)
        th = self.d.odometry()[2]
        turned += sign * ((th - last + math.pi) % (2 * math.pi) - math.pi)
        return {"method": "odometry", "turned_rad": round(turned, 4)}

    def _approach(self, tag_id, stop_dist, speed, deadline):
        c = self.cfg
        if self._measure(tag_id) is None:
            raise Fail("E_TAG_NOT_FOUND", f"tag {tag_id} not visible; try search_tag")
        tol, btol, misses = c.approach_dist_tol_m, math.radians(c.grasp_bearing_tol_deg), 0
        while True:
            self._check(deadline)
            det = self._detect().get(tag_id)
            if det is None:
                self.safety.halt()
                misses += 1
                if misses > c.tag_lost_ticks:
                    raise Fail("E_TAG_LOST", f"tag {tag_id} lost during approach")
                self._tick()
                continue
            misses = 0
            err, b = det.distance_m - stop_dist, det.bearing_rad
            if abs(err) <= tol and abs(b) <= btol:
                self.safety.halt()
                return det
            w = max(-c.max_turn_rate, min(c.max_turn_rate, 1.5 * b)) if abs(b) > 0.3 * btol else 0.0
            if abs(b) > math.radians(12):
                v = 0.0
            elif err > 0.15:
                v = speed
            elif err > tol:
                v = c.creep_speed
            elif err < -tol:
                v = -c.creep_speed
            else:
                v = 0.0
            self.safety.command(v, w)
            self._tick()

    # ======================================================================= primitives
    def p_stop(self, deadline):
        self.safety.halt()
        a = self.d.odometry()
        time.sleep(0.3)
        b = self.d.odometry()
        desc = "robot stationary"
        if a is None or b is None:
            return outcome("UNKNOWN", "E_UNVERIFIED", "stop sent; no odometry to confirm", desc, False, None)
        moved = math.hypot(b[0] - a[0], b[1] - a[1])
        ok = moved < 0.005 and abs(b[2] - a[2]) < 0.01
        return outcome("SUCCEEDED" if ok else "FAILED", "OK" if ok else "E_POSTCONDITION",
                       "stopped" if ok else "still moving after stop", desc, True, ok, {"moved_m": round(moved, 4)})

    def p_drive(self, deadline, distance_m, speed_mps):
        if abs(distance_m) < 0.01:
            raise Fail("E_ARGS", "|distance_m| must be >= 0.01")
        ev = self._drive(distance_m, speed_mps, deadline)
        desc = self.specs["drive_bounded"].postcondition
        if ev["traveled_m"] is None:
            return outcome("UNKNOWN", "E_UNVERIFIED", "driven open-loop; no odometry", desc, False, None, ev)
        ok = abs(ev["traveled_m"] - abs(distance_m)) <= 0.02
        return outcome("SUCCEEDED" if ok else "FAILED", "OK" if ok else "E_POSTCONDITION",
                       f"traveled {ev['traveled_m']} m", desc, True, ok, ev)

    def p_turn(self, deadline, angle_deg, rate_deg_s):
        ev = self._turn(math.radians(angle_deg), math.radians(rate_deg_s), deadline)
        desc = self.specs["turn_bounded"].postcondition
        if ev["turned_rad"] is None:
            return outcome("UNKNOWN", "E_UNVERIFIED", "turned open-loop; no odometry", desc, False, None, ev)
        turned = math.degrees(ev["turned_rad"])
        ok = abs(turned - abs(angle_deg)) <= 3.0
        ev["turned_deg"] = round(turned, 2)
        return outcome("SUCCEEDED" if ok else "FAILED", "OK" if ok else "E_POSTCONDITION",
                       f"turned {turned:.1f} deg", desc, True, ok, ev)

    def p_observe(self, deadline):
        frame = self._frame()
        tags = [d.to_dict() for d in self.detector.detect(frame).values()]
        return outcome("SUCCEEDED", "OK", f"{len(tags)} tag(s) visible", "frame captured", True, True,
                       data={"tags": tags, "frame_shape": list(frame.shape), "captured_at": time.time()})

    def p_detect(self, deadline, tag_id):
        hits = [self._detect().get(tag_id) for _ in range(3)]
        hits = [h for h in hits if h]
        desc = "tag visible in >=2 of 3 frames"
        if len(hits) < 2:
            return outcome("FAILED", "E_TAG_NOT_FOUND", f"tag {tag_id} not visible", desc, True, False,
                           {"frames_seen": len(hits)})
        best = hits[-1].to_dict()
        best["distance_m"] = round(statistics.median(h.distance_m for h in hits), 4)
        return outcome("SUCCEEDED", "OK", f"tag {tag_id} at {best['distance_m']} m, {best['bearing_deg']} deg",
                       desc, True, True, {"frames_seen": len(hits)}, data={"detection": best})

    def p_search(self, deadline, tag_id, max_turn_deg):
        self._pose("look")  # camera is on the arm: aim it ahead before looking
        turned = 0.0
        while True:
            dist = self._measure(tag_id)
            if dist is not None:
                det = self._detect().get(tag_id)
                return outcome("SUCCEEDED", "OK", f"found tag {tag_id} after {turned:.0f} deg", "tag visible",
                               True, True, {"turned_deg": turned},
                               data={"detection": det.to_dict() if det else {"distance_m": dist}})
            if turned + 30 > max_turn_deg:
                raise Fail("E_TAG_NOT_FOUND", f"tag {tag_id} not seen in {turned:.0f} deg sweep",
                           evidence={"turned_deg": turned})
            self._turn(math.radians(30), math.radians(45), deadline)
            turned += 30

    def p_approach(self, deadline, tag_id, stop_distance_m, speed_mps):
        self._pose("look")
        det = self._approach(tag_id, stop_distance_m, speed_mps, deadline)
        final = self._measure(tag_id)
        desc = self.specs["approach_tag"].postcondition
        if final is None:
            return outcome("UNKNOWN", "E_TAG_LOST", "tag not visible after stopping", desc, False, None)
        ok = abs(final - stop_distance_m) <= 2 * self.cfg.approach_dist_tol_m
        return outcome("SUCCEEDED" if ok else "FAILED", "OK" if ok else "E_POSTCONDITION",
                       f"stopped {final} m from tag {tag_id}", desc, True, ok,
                       {"final_distance_m": final, "bearing_deg": round(math.degrees(det.bearing_rad), 2)})

    def _gripper(self, open_):
        self.d.set_gripper(open_)
        time.sleep(self.cfg.gripper_settle_s)
        return self.d.gripper_state()

    def p_gripper_open(self, deadline):
        s = self._gripper(True)
        self.held_tag = None
        desc = self.specs["gripper_open"].postcondition
        if s == "unknown":
            return outcome("UNKNOWN", "E_UNVERIFIED", "open commanded; no gripper sensor", desc, False, None)
        ok = s == "open"
        return outcome("SUCCEEDED" if ok else "FAILED", "OK" if ok else "E_POSTCONDITION", f"gripper {s}",
                       desc, True, ok, {"gripper_state": s})

    def p_gripper_close(self, deadline):
        s = self._gripper(False)
        desc = self.specs["gripper_close"].postcondition
        if s == "unknown":
            return outcome("UNKNOWN", "E_UNVERIFIED", "close commanded; no gripper sensor", desc, False, None)
        ok = s in ("closed", "holding", "empty")
        return outcome("SUCCEEDED" if ok else "FAILED", "OK" if ok else "E_POSTCONDITION", f"gripper {s}",
                       desc, True, ok, {"gripper_state": s})

    def p_arm_pose(self, deadline, pose):
        self.d.set_arm_pose(pose)
        return outcome("UNKNOWN", "E_UNVERIFIED", f"arm commanded to '{pose}'",
                       self.specs["arm_pose"].postcondition, False, None, data={"pose": pose})

    def _pose(self, name):
        """Use a saved arm pose if this robot has it; skip silently otherwise (no-arm robots)."""
        if name in self.d.arm_pose_names():
            self.d.set_arm_pose(name)
            return True
        return False

    def p_pick(self, deadline, tag_id):
        c = self.cfg
        desc = self.specs["pick_tagged"].postcondition
        self._gripper(True)
        self.held_tag = None
        self._pose("look") or self._pose("home")
        aligned = self._approach(tag_id, c.grasp_standoff_m, 0.75 * c.max_speed, deadline)
        reached = self._pose("reach")
        d0 = self._measure(tag_id)
        sensor = self._gripper(False)
        lifted = self._pose("carry")
        back = self._drive(-c.verify_backoff_m, 2 * c.creep_speed, deadline)
        d1 = self._measure(tag_id)
        moved = back["traveled_m"] if back["traveled_m"] is not None else c.verify_backoff_m
        ev = {"aligned": aligned.to_dict(), "tag_distance_before_m": d0, "tag_distance_after_reverse_m": d1,
              "robot_reversed_m": moved, "gripper_sensor": sensor, "arm_reach": reached, "arm_carry": lifted}
        if sensor == "empty":
            self._gripper(True)
            return outcome("FAILED", "E_GRASP_FAILED", "gripper sensor reports empty", desc, True, False, ev)
        if d0 is None or d1 is None:
            return outcome("UNKNOWN", "E_GRASP_UNVERIFIED",
                           "tag not visible before/after reverse; cannot tell whether object is held", desc,
                           False, None, ev)
        delta = d1 - d0
        ev["tag_distance_change_m"] = round(delta, 4)
        if abs(delta) <= 0.03:
            self.held_tag = tag_id
            return outcome("SUCCEEDED", "OK", f"object {tag_id} travelled with the robot", desc, True, True, ev)
        if delta >= 0.5 * moved:
            if sensor == "holding":
                return outcome("UNKNOWN", "E_EVIDENCE_CONFLICT", "sensor says holding but tag stayed behind",
                               desc, True, None, ev)
            self._gripper(True)
            return outcome("FAILED", "E_GRASP_FAILED", f"object {tag_id} stayed behind; claw reopened",
                           desc, True, False, ev)
        return outcome("UNKNOWN", "E_GRASP_UNVERIFIED", "ambiguous tag motion during reverse", desc, False, None, ev)

    def p_place(self, deadline, zone_tag_id, object_tag_id):
        c = self.cfg
        desc = self.specs["place_at"].postcondition
        obj = object_tag_id if object_tag_id is not None else self.held_tag
        if obj is None:
            raise Fail("E_NOT_HOLDING", "no verified held object (and no object_tag_id); robot did not move")
        self._approach(zone_tag_id, c.place_standoff_m, 0.75 * c.max_speed, deadline)
        d0 = self._measure(obj)
        self._gripper(True)
        back = self._drive(-1.5 * c.verify_backoff_m, 2 * c.creep_speed, deadline)
        d1, dz = self._measure(obj), self._measure(zone_tag_id)
        moved = back["traveled_m"] if back["traveled_m"] is not None else 1.5 * c.verify_backoff_m
        ev = {"object_tag": obj, "tag_distance_before_m": d0, "tag_distance_after_reverse_m": d1,
              "zone_tag_distance_m": dz, "robot_reversed_m": moved}
        if d0 is None or d1 is None:
            return outcome("UNKNOWN", "E_PLACE_UNVERIFIED", "object tag not visible; cannot verify release",
                           desc, False, None, ev)
        delta = d1 - d0
        ev["tag_distance_change_m"] = round(delta, 4)
        if delta >= 0.5 * moved:
            self.held_tag = None
            if dz is not None:
                ev["object_to_zone_marker_m"] = round(dz - d1, 3)
            return outcome("SUCCEEDED", "OK", f"object {obj} left at zone {zone_tag_id}", desc, True, True, ev)
        if abs(delta) <= 0.03:
            return outcome("FAILED", "E_RELEASE_FAILED", "object still travelling with robot", desc, True, False, ev)
        return outcome("UNKNOWN", "E_PLACE_UNVERIFIED", "ambiguous tag motion", desc, False, None, ev)

    # ======================================================================= state
    def state(self) -> dict:
        odo = self.d.odometry()
        return {
            "time": time.time(),
            "pose": None if odo is None else {"x_m": round(odo[0], 4), "y_m": round(odo[1], 4),
                                               "theta_deg": round(math.degrees(odo[2]), 2)},
            "gripper": self.d.gripper_state(),
            "held_tag_verified": self.held_tag,
            "busy": self.current_op,
            "safety": self.safety.status(),
            "recovered_interrupted_ops": self.journal.recovered,
            "driver": self.d.info(),
        }

    def close(self):
        self.safety.close()
        self.d.close()
