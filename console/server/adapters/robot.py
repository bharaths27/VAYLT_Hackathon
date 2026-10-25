"""Robot adapter. Talks to Brian's controller API, or a simulator so the HUD
can be built and rehearsed with no robot attached.

Honesty rule: the simulator stamps every result and every frame as SIMULATED.
Never record the demo video in mock mode and present it as the real robot.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import random
import time
from typing import Any

import httpx

from .. import config


def normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    lidar = raw.get("lidar") or {}
    return {
        "mode": raw.get("mode", "unknown"),
        "battery_v": raw.get("battery_v"),
        "lidar_min_cm": lidar.get("min_cm", raw.get("lidar_min_cm")),
        "lidar_sectors": lidar.get("sectors", raw.get("lidar_sectors", [])),
        "veto": bool(raw.get("veto", False)),
        "gripper": raw.get("gripper", "unknown"),
        "holding_tag": raw.get("holding_tag"),
        "detections": raw.get("detections", []),
        "pose": raw.get("pose", {}),
        "estop": bool(raw.get("estop", False)),
        "watchdog_ok": raw.get("watchdog_ok", True),
        "simulated": bool(raw.get("simulated", False)),
        "connected": raw.get("connected", True),
    }


class MockRobot:
    """Tiny 2D world: 3 tagged objects, 2 zones, 1 LiDAR ring."""

    def __init__(self) -> None:
        self.x, self.y, self.th = 0.0, 0.0, 0.0  # cm, cm, rad (0 = facing +y)
        self.objects = {9: [-40.0, 120.0, "A"], 7: [10.0, 140.0, None], 8: [55.0, 110.0, None]}  # tag ids match Brian's arena (objects 7, 8)
        self.zone_pos = {"A": (-90.0, 60.0), "B": (90.0, 60.0)}
        self.holding: int | None = None
        self.gripper = "open"
        self.obstacle_cm: float | None = None
        self.estop = False
        self.mode = "idle"
        self._frame_n = 0

    # ---- sensing ---------------------------------------------------------------
    def _rel(self, ox, oy):
        dx, dy = ox - self.x, oy - self.y
        dist = math.hypot(dx, dy)
        bearing = math.atan2(dx, dy) - self.th
        bearing = (bearing + math.pi) % (2 * math.pi) - math.pi
        return dist, bearing

    def detections(self):
        out = []
        for tag, (ox, oy, _z) in self.objects.items():
            if tag == self.holding:
                continue
            d, b = self._rel(ox, oy)
            if abs(b) < math.radians(31) and d < 260:
                cx = 320 + (b / math.radians(31)) * 300
                size = max(18, min(150, 5200 / max(d, 1)))
                cy = 260 - 2400 / max(d, 20)
                out.append({"tag_id": tag, "bbox": [cx - size / 2, cy - size / 2, size, size],
                            "distance_cm": round(d, 1), "bearing_deg": round(math.degrees(b), 1)})
        return out

    def lidar(self):
        sectors = []
        for i in range(36):
            base = 180 + 40 * math.sin(i / 3 + self.th)
            if self.obstacle_cm is not None and (i <= 3 or i >= 33):
                base = self.obstacle_cm
            sectors.append(round(base + random.uniform(-2, 2), 1))
        return sectors

    async def state(self):
        sec = self.lidar()
        front = min(sec[:4] + sec[-3:])
        return normalize_state({
            "mode": "estop" if self.estop else self.mode, "battery_v": 7.8 - 0.0001 * self._frame_n,
            "lidar": {"min_cm": front, "sectors": sec}, "veto": front < 20,
            "gripper": self.gripper, "holding_tag": self.holding, "detections": self.detections(),
            "pose": {"x_cm": round(self.x, 1), "y_cm": round(self.y, 1), "heading_deg": round(math.degrees(self.th), 1)},
            "estop": self.estop, "simulated": True,
        })

    # ---- acting ----------------------------------------------------------------
    async def stop(self):
        self.estop = True
        self.mode = "estop"
        return {"status": "stopped", "simulated": True}

    async def clear_stop(self):
        self.estop = False
        self.mode = "idle"
        return {"status": "cleared", "simulated": True}

    async def execute(self, op: str, args: dict, operation_id: str) -> dict:
        t0 = time.time()
        if self.estop:
            return self._res(operation_id, "rejected", t0, reason="E-STOP latched")
        self.mode = "executing"
        try:
            return await self._do(op, args, operation_id, t0)
        finally:
            if not self.estop:
                self.mode = "idle"

    async def _move(self, dist):
        steps = max(1, int(abs(dist) / 2))
        for _ in range(steps):
            if self.estop:
                return "stopped"
            if dist > 0 and self.obstacle_cm is not None and self.obstacle_cm < 20:
                return "vetoed"
            self.x += math.sin(self.th) * dist / steps
            self.y += math.cos(self.th) * dist / steps
            await asyncio.sleep(0.04)
        return "ok"

    async def _turn(self, rad):
        steps = max(1, int(abs(math.degrees(rad)) / 4))
        for _ in range(steps):
            if self.estop:
                return "stopped"
            self.th += rad / steps
            await asyncio.sleep(0.03)
        return "ok"

    async def _do(self, op, a, oid, t0):
        if op == "stop":
            return await self.stop()
        if op == "observe":
            await asyncio.sleep(0.3)
            return self._res(oid, "succeeded", t0, post={"detections": self.detections()})
        if op == "detect_tag":
            await asyncio.sleep(0.4)  # look only, no motion (effect "none")
            obj = self.objects.get(a["tag_id"])
            if obj is not None and a["tag_id"] != self.holding:
                d, b = self._rel(obj[0], obj[1])
                if d < 300:  # sim pretends a wide FOV camera; real detection is Brian's
                    return self._res(oid, "succeeded", t0, post={"tag_visible": a["tag_id"], "distance_cm": round(d, 1),
                                                                 "bearing_deg": round(math.degrees(b), 1), "verified_by": "sim_camera"})
            return self._res(oid, "failed", t0, reason=f"tag {a['tag_id']} not in view")
        if op == "search_tag":
            obj = self.objects.get(a["tag_id"])
            if obj is None or a["tag_id"] == self.holding:
                return self._res(oid, "failed", t0, reason=f"tag {a['tag_id']} not found")
            _, b = self._rel(obj[0], obj[1])
            if await self._turn(b) != "ok":
                return self._res(oid, "stopped", t0)
            return self._res(oid, "succeeded", t0, post={"tag_visible": a["tag_id"], "verified_by": "sim_camera"})
        if op == "drive_bounded":
            r = await self._move(a["distance_cm"])
            return self._res(oid, "succeeded" if r == "ok" else r, t0, post={"pose": [self.x, self.y]})
        if op == "turn_bounded":
            r = await self._turn(math.radians(a["degrees"]))
            return self._res(oid, "succeeded" if r == "ok" else r, t0)
        if op == "approach_tag":
            ox, oy, _ = self.objects[a["tag_id"]]
            d, b = self._rel(ox, oy)
            if await self._turn(b) != "ok":
                return self._res(oid, "stopped", t0)
            r = await self._move(max(0.0, d - a.get("stop_cm", 15)))
            d2, _ = self._rel(ox, oy)
            return self._res(oid, "succeeded" if r == "ok" else r, t0, post={"distance_cm": round(d2, 1)})
        if op == "gripper_open":
            await asyncio.sleep(0.4)
            if self.holding is not None:
                self.objects[self.holding][0:2] = [self.x + math.sin(self.th) * 12, self.y + math.cos(self.th) * 12]
            self.holding, self.gripper = None, "open"
            return self._res(oid, "succeeded", t0)
        if op == "gripper_close":
            await asyncio.sleep(0.4)
            self.gripper = "closed"
            return self._res(oid, "unknown", t0, reason="no object sensor on bare close")
        if op == "pick_tagged":
            ox, oy, _ = self.objects[a["tag_id"]]
            d, _ = self._rel(ox, oy)
            await asyncio.sleep(0.9)
            self.gripper = "closed"
            if d < 25:
                self.holding = a["tag_id"]
                return self._res(oid, "succeeded", t0, post={"holding_tag": a["tag_id"], "verified_by": "sim_gripper_current"})
            return self._res(oid, "failed", t0, reason=f"object {d:.0f}cm away")
        if op == "place_at":
            zx, zy = self.zone_pos[a["zone"]]
            d, b = self._rel(zx, zy)
            if await self._turn(b) != "ok":
                return self._res(oid, "stopped", t0)
            r = await self._move(max(0.0, d - 10))
            if r != "ok":
                return self._res(oid, r, t0)
            await asyncio.sleep(0.4)
            tag = self.holding
            if tag is None:
                return self._res(oid, "failed", t0, reason="not holding anything")
            self.objects[tag] = [zx, zy, a["zone"]]
            self.holding, self.gripper = None, "open"
            return self._res(oid, "succeeded", t0, post={"tag_in_zone": [tag, a["zone"]], "verified_by": "sim_camera"})
        return self._res(oid, "rejected", t0, reason="unknown op")

    def _res(self, oid, status, t0, post=None, reason=None):
        return {"operation_id": oid, "status": status, "postcondition": post or {}, "reason": reason,
                "duration_ms": int((time.time() - t0) * 1000), "simulated": True}

    # ---- simulator controls (mock only) -----------------------------------------
    def set_obstacle(self, cm):
        self.obstacle_cm = cm

    # ---- camera ----------------------------------------------------------------
    async def frame(self) -> bytes:
        from PIL import Image, ImageDraw
        self._frame_n += 1
        W, H = 640, 480
        img = Image.new("RGB", (W, H), (8, 14, 22))
        d = ImageDraw.Draw(img)
        # sky to floor gradient
        for yy in range(H):
            c = int(10 + 20 * (yy / H))
            d.line([(0, yy), (W, yy)], fill=(c // 2, c, c + 8))
        horizon = 200
        # perspective floor grid that slides as the robot moves
        off = (self.y % 20) / 20
        for i in range(1, 14):
            z = (i - off) ** 1.6
            yy = horizon + z * 3.2
            if yy < H:
                d.line([(0, yy), (W, yy)], fill=(20, 60, 70))
        shift = (math.degrees(self.th) * 6) % 80
        for i in range(-12, 14):
            x0 = 320 + i * 80 - shift
            d.line([(320 + (x0 - 320) * 0.08, horizon), (x0 * 2.4 - 448, H)], fill=(20, 60, 70))
        colors = {9: (220, 60, 60), 7: (60, 120, 230), 8: (230, 200, 70)}
        for det in sorted(self.detections(), key=lambda q: -q["distance_cm"]):
            x, y, w, h = det["bbox"]
            col = colors.get(det["tag_id"], (200, 200, 200))
            d.rectangle([x, y + h * 0.15, x + w, y + h * 1.25], fill=tuple(int(v * 0.6) for v in col))
            m = w * 0.5  # fake marker square
            d.rectangle([x + w / 2 - m / 2, y + h * 0.35, x + w / 2 + m / 2, y + h * 0.35 + m], fill=(240, 240, 240))
            d.rectangle([x + w / 2 - m / 3, y + h * 0.35 + m / 6, x + w / 2 + m / 3, y + h * 0.35 + m * 5 / 6], fill=(10, 10, 10))
        if self.obstacle_cm is not None:
            d.rectangle([180, 140, 460, 470], fill=(70, 40, 30))
        # noise + label
        for _ in range(250):
            px, py = random.randrange(W), random.randrange(H)
            d.point((px, py), fill=(40, 70, 80))
        d.rectangle([0, H - 22, 210, H], fill=(0, 0, 0))
        d.text((6, H - 18), f"SIMULATED FEED  #{self._frame_n}", fill=(255, 180, 0))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()


class HttpRobot:
    """Brian's robot adapter (engram_robot/robot/server.py). Differences handled here:
      - units: console uses cm and (+ = right) degrees; robot uses meters and (+ = LEFT/CCW) degrees
      - zones: console says "A"/"B"; robot places at a zone marker tag (ENGRAM_ZONE_TAGS, default A=20,B=21)
      - every physical primitive needs a short-lived HMAC token over {operation_id, op, args}
        (same recipe as engram_robot/robot/auth.py, secret ENGRAM_ROBOT_SECRET)
      - statuses come back upper case; only SUCCEEDED with postcondition.verified counts as sensor-verified
    """

    PHYSICAL = {"drive_bounded", "turn_bounded", "approach_tag", "gripper_open", "gripper_close",
                "pick_tagged", "place_at", "search_tag"}
    STATUS = {"SUCCEEDED": "succeeded", "FAILED": "failed", "UNKNOWN": "unknown", "VETOED": "vetoed",
              "ABORTED": "stopped", "REJECTED": "rejected"}

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.c = httpx.AsyncClient(timeout=config.STEP_TIMEOUT_S)
        self.fast = httpx.AsyncClient(timeout=2.5)
        self.secret = config.ROBOT_SECRET
        self.zone_tags = config.ZONE_TAGS
        self.creep_envelope_m = None

    # ---- translation console -> robot ---------------------------------------------------------
    def translate(self, op: str, a: dict) -> dict:
        if op == "drive_bounded":
            return {"distance_m": round(float(a["distance_cm"]) / 100.0, 4)}
        if op == "turn_bounded":
            return {"angle_deg": -float(a["degrees"])}  # console + = right (CW); robot + = left (CCW)
        if op == "approach_tag":
            return {"tag_id": int(a["tag_id"]), "stop_distance_m": round(max(0.12, float(a.get("stop_cm", 30)) / 100.0), 3)}
        if op == "place_at":
            zone = str(a["zone"]).upper()
            if zone not in self.zone_tags:
                raise ValueError(f"no zone marker tag configured for zone {zone}")
            return {"zone_tag_id": int(self.zone_tags[zone])}
        if op in ("detect_tag", "pick_tagged", "search_tag"):
            return {"tag_id": int(a["tag_id"])}
        return {}

    def sign(self, operation_id: str, op: str, args: dict, ttl_s: float = 45) -> str:
        canon = json.dumps(args or {}, sort_keys=True, separators=(",", ":"))
        payload = {"operation_id": operation_id, "op": op, "args_sha256": hashlib.sha256(canon.encode()).hexdigest(),
                   "exp": time.time() + ttl_s}
        body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
        sig = hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    # ---- calls ---------------------------------------------------------------------------------
    async def _envelope(self):
        if self.creep_envelope_m is None:
            try:
                r = await self.fast.get(f"{self.base}/robot/capabilities")
                self.creep_envelope_m = float(r.json()["limits"]["envelope_m"]["creep"])
            except Exception:  # noqa: BLE001
                self.creep_envelope_m = 0.15
        return self.creep_envelope_m

    async def state(self):
        try:
            r = await self.fast.get(f"{self.base}/robot/state")
            r.raise_for_status()
            j = r.json()
        except (httpx.HTTPError, ValueError):
            return normalize_state({"mode": "unreachable", "connected": False})
        saf = j.get("safety") or {}
        front = saf.get("lidar_min_front_m")
        env = await self._envelope()
        pose = j.get("pose")
        estop = bool(saf.get("estop_latched"))
        return normalize_state({
            "mode": "estop" if estop else ("executing" if j.get("busy") else "idle"),
            "lidar": {"min_cm": None if front is None else round(front * 100, 1), "sectors": []},
            "veto": front is not None and front < env,
            "gripper": j.get("gripper", "unknown"),
            "holding_tag": j.get("held_tag_verified"),
            "detections": [],  # Brian's /robot/frame already draws detected tags onto the image
            "pose": {} if not pose else {"x_cm": pose["x_m"] * 100, "y_cm": pose["y_m"] * 100,
                                         "heading_deg": -pose["theta_deg"]},
            "estop": estop,
            "watchdog_ok": saf.get("supervisor_alive", True),
            "simulated": "sim" in json.dumps(j.get("driver", {})).lower(),
            "connected": True,
        })

    async def stop(self):
        try:
            r = await self.fast.post(f"{self.base}/robot/stop", json={"latch": True, "reason": "operator e-stop (iPad console)"})
            r.raise_for_status()  # an error reply did not confirm the stop: report it, never pass it off as one
            return r.json()
        except httpx.HTTPError as e:
            return {"status": "stop_request_failed", "error": str(e)}

    async def clear_stop(self):
        try:
            r = await self.fast.post(f"{self.base}/robot/reset", json={"operator": "ipad_console"})
            return r.json()
        except httpx.HTTPError as e:
            return {"status": "reset_failed", "error": str(e)}

    async def execute(self, op, args, operation_id):
        if op == "place_at":
            # The zone marker may be out of view after a pick. Searching for it is part of the
            # approved "place in zone X" step; it gets its own operation id and token.
            try:
                zt = self.translate(op, args)["zone_tag_id"]
            except (ValueError, KeyError) as e:
                return {"operation_id": operation_id, "status": "rejected", "reason": str(e)}
            look = await self._call("detect_tag", {"tag_id": zt}, operation_id + ".look")
            if look["status"] != "succeeded":
                found = await self._call("search_tag", {"tag_id": zt}, operation_id + ".search")
                if found["status"] != "succeeded":
                    found["reason"] = f"zone marker {zt} not found: {found.get('reason')}"
                    return found
        return await self._call(op, args, operation_id)

    async def _call(self, op, args, operation_id):
        try:
            rargs = self.translate(op, args)
        except (ValueError, KeyError) as e:
            return {"operation_id": operation_id, "status": "rejected", "reason": str(e)}
        body = {"op": op, "args": rargs, "operation_id": operation_id}
        if op in self.PHYSICAL:
            body["authorization"] = self.sign(operation_id, op, rargs)
        try:
            r = await self.c.post(f"{self.base}/robot/execute", json=body)
            j = r.json()
        except httpx.TimeoutException:
            # We do NOT know whether the motor moved. Report unknown, never retry.
            return {"operation_id": operation_id, "status": "unknown", "reason": "timeout, physical effect uncertain"}
        except (httpx.HTTPError, ValueError) as e:
            return {"operation_id": operation_id, "status": "failed", "reason": f"robot unreachable: {e}"}
        raw = str(j.get("status", "UNKNOWN")).upper()
        pc = j.get("postcondition") or {}
        post = dict(j.get("data") or {})
        if raw == "SUCCEEDED" and pc.get("verified") and pc.get("holds") is not False:
            post["verified_by"] = "robot sensor: " + str(pc.get("description", ""))[:80]
        return {"operation_id": operation_id, "status": self.STATUS.get(raw, "unknown"),
                "postcondition": post, "reason": None if raw == "SUCCEEDED" else f"{j.get('code')}: {j.get('message')}",
                "duration_ms": (j.get("timing") or {}).get("duration_ms"), "robot_code": j.get("code"),
                "robot_args": rargs, "replayed": j.get("replayed", False)}

    async def frame(self):
        r = await self.fast.get(f"{self.base}/robot/frame", params={"annotate": "true"})
        r.raise_for_status()
        return r.content

    def set_obstacle(self, cm):  # real robot: obstacles are real
        pass


class OpenCVCamera:
    """Fallback if Brian's frame endpoint is not up: read a USB cam on the Pi."""

    def __init__(self, idx: int) -> None:
        import cv2  # noqa
        self.cv2 = cv2
        self.cap = cv2.VideoCapture(idx)

    async def frame(self):
        ok, img = await asyncio.to_thread(self.cap.read)
        if not ok:
            raise RuntimeError("camera read failed")
        ok, buf = self.cv2.imencode(".jpg", img, [self.cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes()


def make_robot():
    return MockRobot() if config.ROBOT_URL == "mock" else HttpRobot(config.ROBOT_URL)


def make_camera(robot):
    if config.CAMERA.startswith("opencv:"):
        return OpenCVCamera(int(config.CAMERA.split(":")[1]))
    return robot
