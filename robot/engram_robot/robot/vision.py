"""ArUco tag detection -> bearing and forward distance from a pinhole model.

Distance uses the known tag edge length and the calibrated focal length (R04: tools/calibrate.py).
"""
import math
from dataclasses import asdict, dataclass

import cv2
import numpy as np

from .config import Config


@dataclass
class TagDetection:
    tag_id: int
    distance_m: float     # forward (optical-axis) distance camera -> tag
    bearing_rad: float    # + = tag is to the left
    side_px: float
    center_px: tuple

    def to_dict(self):
        d = asdict(self)
        d["bearing_deg"] = round(math.degrees(self.bearing_rad), 2)
        d["distance_m"] = round(self.distance_m, 4)
        d["bearing_rad"] = round(self.bearing_rad, 4)
        d["side_px"] = round(self.side_px, 1)
        d["center_px"] = [round(c, 1) for c in self.center_px]
        return d


class TagDetector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg.aruco_dict))
        self._det = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())

    def detect(self, frame: np.ndarray | None) -> dict[int, TagDetection]:
        if frame is None:
            return {}
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        corners, ids, _ = self._det.detectMarkers(gray)
        out: dict[int, TagDetection] = {}
        if ids is None:
            return out
        f = self.cfg.focal_px
        cx = gray.shape[1] / 2
        for c, i in zip(corners, ids.flatten()):
            pts = c.reshape(4, 2)
            side = float(np.mean([np.linalg.norm(pts[k] - pts[(k + 1) % 4]) for k in range(4)]))
            u, v = float(pts[:, 0].mean()), float(pts[:, 1].mean())
            tid = int(i)
            out[tid] = TagDetection(tid, f * self.cfg.tag_size(tid) / side, math.atan2(cx - u, f), side, (u, v))
        return out

    def annotate(self, frame: np.ndarray) -> np.ndarray:
        img = frame.copy()
        for d in self.detect(frame).values():
            u, v = map(int, d.center_px)
            h = int(d.side_px / 2)
            cv2.rectangle(img, (u - h, v - h), (u + h, v + h), (0, 200, 0), 2)
            cv2.putText(img, f"id{d.tag_id} {d.distance_m:.2f}m {math.degrees(d.bearing_rad):+.0f}deg",
                        (u - h, max(12, v - h - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        return img
