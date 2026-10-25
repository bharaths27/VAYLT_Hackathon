"""Safety supervisor: the only path to the motors, and a thread that can stop them on its own.

Independent of the planner: the planner runs in another process and can only ask for typed
primitives. This thread keeps running if a primitive's control loop hangs or the planner dies.

It stops the robot when:
  * a LiDAR return in the direction of travel is inside the speed-dependent envelope (E_LIDAR_VETO)
  * the control loop stops renewing its motion lease (E_WATCHDOG)
  * the LiDAR goes stale (E_LIDAR_STALE)  -> fail closed: no LiDAR, no motion
  * an e-stop is latched (API, physical button) (E_ESTOP) -> stays stopped until an operator resets
"""
import logging
import math
import threading
import time
from collections import deque

import numpy as np

from .config import Config
from .driver import RobotDriver

log = logging.getLogger("robot.safety")


class SafetyStop(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class SafetySupervisor:
    def __init__(self, driver: RobotDriver, cfg: Config):
        self.d, self.cfg = driver, cfg
        self._lock = threading.RLock()
        self.estop_latched = False
        self.estop_reason: str | None = None
        self.trip: tuple[str, str] | None = None
        self._cmd = (0.0, 0.0)
        self._lease_until = 0.0
        self._scan: tuple[np.ndarray, np.ndarray] | None = None
        self._scan_t = -1e9
        self._loop_t = 0.0
        self._link_deadline: float | None = None   # None = no brain attached (manual/curl use)
        self.link_client: str | None = None
        self.events: deque = deque(maxlen=200)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="safety-supervisor")
        self._thread.start()

    # ---------- geometry ----------
    def _in_cone(self, direction: float):
        angles, _ = self._scan
        diff = np.abs((angles - direction + math.pi) % (2 * math.pi) - math.pi)
        return diff <= math.radians(self.cfg.veto_cone_deg)

    def sensed(self, direction: float) -> bool:
        """Does the range sensor have any beam in this direction? (forward ultrasonic: rear = False)"""
        return self._scan is not None and bool(self._in_cone(direction).any())

    def _cone_min(self, direction: float) -> float:
        if self._scan is None:
            return 0.0
        r = self._scan[1][self._in_cone(direction)]
        r = r[np.isfinite(r) & (r > self.cfg.lidar_min_valid_m)]
        return float(r.min()) if r.size else math.inf

    def _blocked(self, v: float) -> tuple[bool, float]:
        direction = 0.0 if v >= 0 else math.pi
        if not self.sensed(direction):
            # No sensor covers this direction: never "clear". Only a slow, short blind reverse is allowed
            # (distance capped by the primitives); forward motion always needs a sensor.
            blind_ok = v < 0 and abs(v) <= self.cfg.blind_reverse_max_speed + 1e-9
            return (not blind_ok), math.nan
        mn = self._cone_min(direction)
        return mn < self.cfg.envelope(v), mn

    def _stale(self) -> bool:
        return time.monotonic() - self._scan_t > self.cfg.lidar_stale_s

    def _event(self, code, msg):
        self.events.append({"t": time.time(), "code": code, "message": msg})
        log.warning("%s: %s", code, msg)

    def _halt_locked(self):
        self._cmd = (0.0, 0.0)
        self.d.stop()

    def _trip_locked(self, code, msg):
        self._halt_locked()
        self.trip = (code, msg)
        self._event(code, msg)

    # ---------- API used by primitives ----------
    def heartbeat(self, client: str):
        with self._lock:
            self._link_deadline = time.monotonic() + self.cfg.link_timeout_s
            self.link_client = client

    def release_link(self):
        with self._lock:
            self._link_deadline, self.link_client = None, None

    def _link_lost(self) -> bool:
        return self._link_deadline is not None and time.monotonic() > self._link_deadline

    def begin_op(self):
        with self._lock:
            self.trip = None

    def command(self, v: float, w: float):
        """Request motion for one control tick. Raises SafetyStop instead of moving if unsafe."""
        cfg = self.cfg
        with self._lock:
            if self.estop_latched:
                self._halt_locked()
                raise SafetyStop("E_ESTOP", self.estop_reason or "e-stop latched")
            if self.trip:
                raise SafetyStop(*self.trip)
            if self._link_lost():
                self._trip_locked("E_LINK_LOST", f"no heartbeat from brain '{self.link_client}'")
                raise SafetyStop(*self.trip)
            if time.monotonic() - self._loop_t > 5.0 / cfg.tick_hz:
                self._trip_locked("E_SAFETY_DOWN", "safety supervisor not running")
                raise SafetyStop(*self.trip)
            v = max(-cfg.max_speed, min(cfg.max_speed, float(v)))
            w = max(-cfg.max_turn_rate, min(cfg.max_turn_rate, float(w)))
            if v == 0.0 and w == 0.0:
                self._halt_locked()
                return
            if v != 0.0:
                if self._stale():
                    self._trip_locked("E_LIDAR_STALE", "no fresh LiDAR scan; refusing to move")
                    raise SafetyStop(*self.trip)
                blocked, mn = self._blocked(v)
                if blocked and math.isnan(mn):
                    self._trip_locked("E_UNSENSED", f"no range sensor covers this direction; blind reverse "
                                                    f"allowed only at <= {cfg.blind_reverse_max_speed} m/s")
                    raise SafetyStop(*self.trip)
                if blocked:
                    self._trip_locked("E_LIDAR_VETO", f"obstacle at {mn:.3f} m inside envelope "
                                                      f"{cfg.envelope(v):.3f} m (v={v:+.2f})")
                    raise SafetyStop(*self.trip)
            self._cmd = (v, w)
            self._lease_until = time.monotonic() + cfg.lease_s
            self.d.set_velocity(v, w)

    def halt(self):
        with self._lock:
            self._halt_locked()

    def estop(self, reason: str = "operator e-stop"):
        self.d.stop()  # first, without waiting for anything
        with self._lock:
            self.estop_latched, self.estop_reason = True, reason
            self._halt_locked()
            self._event("E_ESTOP", reason)

    def reset(self, operator: str) -> bool:
        with self._lock:
            was = self.estop_latched
            self.estop_latched, self.estop_reason, self.trip = False, None, None
            self._event("RESET", f"e-stop cleared by {operator}")
            return was

    # ---------- independent loop ----------
    def _run(self):
        period = 1.0 / self.cfg.tick_hz
        while self._running:
            t0 = time.monotonic()
            try:
                ts, angles, ranges = self.d.lidar_scan()
                if ts is not None:
                    self._scan, self._scan_t = (np.asarray(angles), np.asarray(ranges)), ts
            except Exception as e:  # a broken LiDAR must not kill the supervisor
                log.exception("lidar read failed: %s", e)
            with self._lock:
                self._loop_t = time.monotonic()
                v, w = self._cmd
                if v or w:
                    if time.monotonic() > self._lease_until:
                        self._trip_locked("E_WATCHDOG", "control lease expired; motion stopped")
                    elif self._link_lost():
                        self._trip_locked("E_LINK_LOST", f"brain '{self.link_client}' went silent; stopped")
                    elif v and self._stale():
                        self._trip_locked("E_LIDAR_STALE", "LiDAR went stale during motion")
                    elif v:
                        blocked, mn = self._blocked(v)
                        if blocked and math.isnan(mn):
                            self._trip_locked("E_UNSENSED", "moving in a direction no sensor covers")
                        elif blocked:
                            self._trip_locked("E_LIDAR_VETO", f"obstacle at {mn:.3f} m during motion")
                if self.estop_latched:
                    self.d.stop()
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def status(self) -> dict:
        with self._lock:
            return {
                "estop_latched": self.estop_latched,
                "estop_reason": self.estop_reason,
                "last_trip": self.trip[0] if self.trip else None,
                "commanded": {"v_mps": self._cmd[0], "w_radps": self._cmd[1]},
                "lidar_age_s": round(time.monotonic() - self._scan_t, 3),
                "lidar_min_front_m": _r(self._cone_min(0.0)),
                "lidar_min_rear_m": _r(self._cone_min(math.pi)) if self.sensed(math.pi) else None,
                "rear_sensed": self.sensed(math.pi),
                "envelope_at_max_speed_m": round(self.cfg.envelope(self.cfg.max_speed), 3),
                "brain_link": None if self._link_deadline is None else {
                    "client": self.link_client, "ok": not self._link_lost()},
                "supervisor_alive": time.monotonic() - self._loop_t < 5.0 / self.cfg.tick_hz,
                "recent_events": list(self.events)[-10:],
            }

    def close(self):
        self._running = False
        self.d.stop()


def _r(x):
    return None if not math.isfinite(x) else round(x, 3)
