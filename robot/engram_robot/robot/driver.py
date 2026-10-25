"""The only hardware-specific surface. Implement this once for the real robot (see drivers/real_template.py).

Rules for implementers:
  * Every method must return quickly (< ~20 ms). Run camera and LiDAR in their own threads and
    return the latest sample; the safety supervisor calls lidar_scan() at tick_hz.
  * set_velocity() must be safe to call repeatedly. If the motor board supports a command
    timeout in firmware, enable it (~0.3 s): that is the last line of defence if Python dies.
  * Never report what you do not know: gripper_state() returns "unknown" without a sensor,
    odometry() returns None without encoders.
"""
from abc import ABC, abstractmethod

import numpy as np


class RobotDriver(ABC):
    @abstractmethod
    def set_velocity(self, v_mps: float, w_radps: float) -> None:
        """Linear (forward +) and angular (counter-clockwise +) velocity."""

    @abstractmethod
    def stop(self) -> None:
        """Zero all motion immediately."""

    @abstractmethod
    def lidar_scan(self) -> tuple[float, np.ndarray, np.ndarray]:
        """(monotonic timestamp of scan, angles rad robot frame [0 = forward, + = left], ranges m)."""

    @abstractmethod
    def get_frame(self) -> np.ndarray | None:
        """Latest BGR camera frame, or None if the camera is down."""

    @abstractmethod
    def set_gripper(self, open_: bool) -> None:
        """Open (True) or close (False) the claw."""

    def gripper_state(self) -> str:
        """'open' | 'closed' | 'holding' | 'empty' | 'unknown'. 'holding'/'empty' need a real sensor."""
        return "unknown"

    def odometry(self) -> tuple[float, float, float] | None:
        """(x m, y m, theta rad) from encoders/IMU, or None if unavailable."""
        return None

    def arm_pose_names(self) -> list[str]:
        """Named arm poses this robot knows (e.g. home, look, reach, carry). [] = no arm."""
        return []

    def set_arm_pose(self, name: str) -> None:
        """Move the arm to a saved pose and return when it should be there (no joint feedback)."""
        raise NotImplementedError("this robot has no arm")

    def info(self) -> dict:
        return {"driver": type(self).__name__}

    def close(self) -> None:
        pass
