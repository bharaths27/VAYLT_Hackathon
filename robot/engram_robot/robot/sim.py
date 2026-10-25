"""Simulated robot so the adapter, planner and UI can be built before the hardware is identified.

Renders real ArUco markers into a pinhole-camera frame (so the real detector is exercised),
ray-casts a 360-degree LiDAR, integrates differential-drive kinematics, and models a claw that
can miss. Test hooks: grasp_miss_prob, occlude_held, lidar_fail.
"""
import math
import random
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from .config import Config
from .driver import RobotDriver

ROBOT_RADIUS = 0.10
HELD_OFFSET = 0.12  # tag of a held object sits this far in front of the camera


@dataclass
class SimTag:
    tag_id: int
    x: float
    y: float
    kind: str = "object"        # "object" (graspable, on the floor) or "zone" (marker on a post)
    lidar_radius: float = 0.0   # 0 = below the LiDAR plane (small tabletop object)


class SimDriver(RobotDriver):
    def __init__(self, cfg: Config, tags=None, obstacles=None, pose=(0.0, 0.0, 0.0),
                 grasp_miss_prob=0.0, grasp_sensor=False, arena_half=2.0, seed=0,
                 range_sensor="lidar"):
        self.cfg = cfg
        self.tags = {t.tag_id: t for t in (tags or [])}
        self.obstacles = list(obstacles or [])  # (x, y, radius)
        self.pose = list(pose)
        self.arena_half = arena_half
        self.grasp_miss_prob = grasp_miss_prob
        self.grasp_sensor = grasp_sensor
        self.range_sensor = range_sensor  # "lidar" (360 deg) or "ultrasonic" (one forward beam)
        self._us_last = (0.0, None)
        self.rng = random.Random(seed)
        self.held: int | None = None
        self.claw_open = True
        self.collided = False
        self.occlude_held = False
        self.lidar_fail = False
        self.arm_pose = "home"
        self._v = self._w = 0.0
        self._lock = threading.Lock()
        self._marker_cache: dict = {}
        self._dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg.aruco_dict))
        self._angles = np.linspace(-math.pi, math.pi, 360, endpoint=False)
        self._running = True
        threading.Thread(target=self._integrate, daemon=True, name="sim-physics").start()

    # ---------------- physics ----------------
    def _integrate(self):
        last = time.monotonic()
        while self._running:
            time.sleep(0.005)
            now = time.monotonic()
            dt, last = now - last, now
            with self._lock:
                x, y, th = self.pose
                nx = x + self._v * math.cos(th) * dt
                ny = y + self._v * math.sin(th) * dt
                if self._body_hits(nx, ny):
                    self.collided = True
                    self._v = self._w = 0.0
                    continue
                self.pose = [nx, ny, (th + self._w * dt + math.pi) % (2 * math.pi) - math.pi]
                if self.held is not None:
                    t = self.tags[self.held]
                    t.x = nx + HELD_OFFSET * math.cos(self.pose[2])
                    t.y = ny + HELD_OFFSET * math.sin(self.pose[2])

    def _body_hits(self, x, y):
        for ox, oy, r in self._lidar_circles():
            if math.hypot(ox - x, oy - y) < r + ROBOT_RADIUS:
                return True
        return abs(x) > self.arena_half - ROBOT_RADIUS or abs(y) > self.arena_half - ROBOT_RADIUS

    def _lidar_circles(self):
        circles = list(self.obstacles)
        circles += [(t.x, t.y, t.lidar_radius) for t in self.tags.values()
                    if t.lidar_radius > 0 and t.tag_id != self.held]
        return circles

    # ---------------- driver API ----------------
    def set_velocity(self, v_mps, w_radps):
        with self._lock:
            if not self.collided:
                self._v, self._w = float(v_mps), float(w_radps)

    def stop(self):
        with self._lock:
            self._v = self._w = 0.0

    def velocity(self):
        with self._lock:
            return self._v, self._w

    def odometry(self):
        with self._lock:
            return tuple(self.pose)

    def lidar_scan(self):
        if self.lidar_fail:
            return (time.monotonic() - 10.0, self._angles, np.full_like(self._angles, np.inf))
        with self._lock:
            x, y, th = self.pose
            circles = self._lidar_circles()
        a = self._angles + th
        dx, dy = np.cos(a), np.sin(a)
        L = self.arena_half
        with np.errstate(divide="ignore", invalid="ignore"):
            tx = np.where(dx > 0, (L - x) / dx, np.where(dx < 0, (-L - x) / dx, np.inf))
            ty = np.where(dy > 0, (L - y) / dy, np.where(dy < 0, (-L - y) / dy, np.inf))
        r = np.minimum(tx, ty)
        for cx, cy, cr in circles:
            ox, oy = cx - x, cy - y
            proj = ox * dx + oy * dy
            perp2 = ox * ox + oy * oy - proj * proj
            hit = (perp2 < cr * cr) & (proj > 0)
            t = proj - np.sqrt(np.clip(cr * cr - perp2, 0, None))
            r = np.where(hit & (t < r), t, r)
        r = np.clip(r, 0, 8.0)
        now = time.monotonic()
        if self.range_sensor == "ultrasonic":
            # one forward reading: nearest echo within a ~±12 deg beam, 3 m max range, ~15 Hz update
            if now - self._us_last[0] < 1 / 15 and self._us_last[1] is not None:
                return self._us_last[1]
            d = float(r[np.abs(self._angles) <= math.radians(12)].min())
            out = (now, np.array([0.0]), np.array([d if d <= 3.0 else np.inf]))
            self._us_last = (now, out)
            return out
        return now, self._angles.copy(), r

    def set_gripper(self, open_):
        time.sleep(0.05)
        with self._lock:
            if open_:
                self.claw_open, self.held = True, None
                return
            self.claw_open = False
            x, y, th = self.pose
            for t in self.tags.values():
                if t.kind != "object":
                    continue
                fwd = (t.x - x) * math.cos(th) + (t.y - y) * math.sin(th)
                lat = -(t.x - x) * math.sin(th) + (t.y - y) * math.cos(th)
                if abs(fwd - self.cfg.grasp_standoff_m) < 0.035 and abs(math.atan2(lat, fwd)) < math.radians(8):
                    if self.rng.random() >= self.grasp_miss_prob:
                        self.held = t.tag_id
                    return

    def arm_pose_names(self):
        return ["carry", "home", "look", "reach"]

    def set_arm_pose(self, name):
        time.sleep(0.05)
        self.arm_pose = name

    def gripper_state(self):
        with self._lock:
            if self.claw_open:
                return "open"
            if not self.grasp_sensor:
                return "closed"  # closed, but no idea whether anything is in it
            return "holding" if self.held is not None else "empty"

    # ---------------- camera ----------------
    def _marker(self, tag_id, px):
        key = (tag_id, px)
        if key not in self._marker_cache:
            m = cv2.aruco.generateImageMarker(self._dict, tag_id, px, borderBits=1)
            pad = max(4, int(px * 0.15))
            self._marker_cache[key] = cv2.copyMakeBorder(m, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        return self._marker_cache[key]

    def get_frame(self):
        cfg = self.cfg
        W, H, f = cfg.cam_width, cfg.cam_height, cfg.focal_px
        cx, cy = W / 2, H / 2
        img = np.full((H, W), 170, np.uint8)
        with self._lock:
            x, y, th = self.pose
            tags = [(t.tag_id, t.x, t.y, t.kind) for t in self.tags.values()
                    if not (self.occlude_held and t.tag_id == self.held)]
        items = []
        for tid, tx, ty, kind in tags:
            fwd = (tx - x) * math.cos(th) + (ty - y) * math.sin(th)
            lat = -(tx - x) * math.sin(th) + (ty - y) * math.cos(th)
            if fwd < 0.05:
                continue
            px = int(round(f * cfg.tag_size(tid) / fwd))
            if px < 12:
                continue
            u = cx - f * lat / fwd
            v = cy + f * (0.02 if kind == "object" else -0.10) / fwd  # objects low, zone posts high
            items.append((fwd, tid, px, u, v))
        for _, tid, px, u, v in sorted(items, reverse=True):  # far first, near occludes far
            m = self._marker(tid, px)
            h = m.shape[0]
            x0, y0 = int(round(u - h / 2)), int(round(v - h / 2))
            if x0 < 0 or y0 < 0 or x0 + h > W or y0 + h > H:
                continue  # partially outside the frame -> not visible
            img[y0:y0 + h, x0:x0 + h] = m
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    def info(self):
        return {"driver": "SimDriver", "simulated": True}

    def close(self):
        self._running = False


def default_world(cfg: Config, **kw) -> SimDriver:
    """Demo arena: two objects (7, 8), two zone posts (20, 21), one obstacle."""
    tags = [
        SimTag(7, 1.00, 0.10),
        SimTag(8, 1.10, -0.45),
        SimTag(20, 1.80, 0.60, kind="zone", lidar_radius=0.04),
        SimTag(21, -0.80, 0.00, kind="zone", lidar_radius=0.04),
    ]
    return SimDriver(cfg, tags=tags, obstacles=[(0.0, -1.2, 0.10)], **kw)
