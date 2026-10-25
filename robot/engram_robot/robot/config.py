"""All tunable numbers live here. R04 calibration overwrites these (see README)."""
import json
import math
from dataclasses import asdict, dataclass, field


@dataclass
class Config:
    # --- motion limits (hard clamps applied by the safety supervisor) ---
    max_speed: float = 0.20          # m/s
    creep_speed: float = 0.05        # m/s, final approach
    max_turn_rate: float = 1.0       # rad/s
    max_drive_m: float = 1.5         # longest single drive_bounded

    # --- safety envelope ---
    # Motion is vetoed when any LiDAR return inside the motion cone is closer than
    #   safety_margin + v * latency + v^2 / (2 * decel)
    # i.e. the faster you go, the earlier you stop. Deniz calibrates these.
    safety_margin_m: float = 0.10
    safety_latency_s: float = 0.25
    safety_decel: float = 0.5        # m/s^2, measured braking
    veto_cone_deg: float = 30.0      # half-angle around direction of travel
    lidar_min_valid_m: float = 0.02  # returns below this are "no return" (RPLidar reports 0)
    lidar_stale_s: float = 0.5       # no fresh scan for this long -> no motion
    lease_s: float = 0.5             # control loop must renew motion this often (watchdog)
    # Range sensor without rear coverage (forward ultrasonic): reversing is "blind".
    # Blind reverse is only allowed this slowly and this far per operation.
    blind_reverse_max_speed: float = 0.05
    blind_reverse_max_m: float = 0.20
    tick_hz: float = 20.0
    # Brain link: once a brain sends heartbeats, motion stops if none arrives for this long
    link_timeout_s: float = 1.5

    # --- camera / tags ---
    cam_width: int = 640
    cam_height: int = 480
    cam_hfov_deg: float = 62.0       # replace with calibrated focal_px_override after R04
    focal_px_override: float = 0.0
    aruco_dict: str = "DICT_4X4_50"
    default_tag_size_m: float = 0.04                      # object tags (black square edge)
    tag_sizes_m: dict = field(default_factory=lambda: {i: 0.06 for i in range(20, 30)})  # zone tags

    # --- manipulation ---
    grasp_standoff_m: float = 0.12   # camera-to-tag distance at which the claw closes
    grasp_bearing_tol_deg: float = 6.0
    approach_dist_tol_m: float = 0.015
    place_standoff_m: float = 0.35   # camera-to-zone-tag distance when releasing
    verify_backoff_m: float = 0.10   # reverse this far to check whether the object came along
    gripper_settle_s: float = 0.6
    tag_lost_ticks: int = 10         # consecutive misses before E_TAG_LOST

    # --- authorization / journal ---
    auth_ttl_max_s: float = 60.0
    journal_path: str = "robot_journal.jsonl"

    @property
    def focal_px(self) -> float:
        if self.focal_px_override > 0:
            return self.focal_px_override
        return (self.cam_width / 2) / math.tan(math.radians(self.cam_hfov_deg / 2))

    def tag_size(self, tag_id: int) -> float:
        return float(self.tag_sizes_m.get(int(tag_id), self.default_tag_size_m))

    def envelope(self, speed: float) -> float:
        s = abs(speed)
        return self.safety_margin_m + s * self.safety_latency_s + s * s / (2 * self.safety_decel)

    @classmethod
    def load(cls, path: str | None) -> "Config":
        cfg = cls()
        if path:
            with open(path) as f:
                data = json.load(f)
            for k, v in data.items():
                if k == "tag_sizes_m":
                    v = {int(i): float(s) for i, s in v.items()}
                if not hasattr(cfg, k):
                    raise KeyError(f"unknown config key {k}")
                setattr(cfg, k, v)
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)
