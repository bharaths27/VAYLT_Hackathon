"""DC wheel motors: (v m/s, w rad/s) -> per-side PWM duty, with calibration.

DC motors take a duty cycle, not a speed. Below some duty (the deadband) they don't turn at all,
and above it ground speed is roughly linear in duty. Four fixed (non-steering) wheels drive like a
tank: left pair and right pair. Skid-steer turns slip, so actual turn rate < commanded;
`turn_slip` compensates. Measure everything with tools/calibrate_motors.py (wheels ON the floor).

Without encoders the robot can't measure its own motion, so drive_bounded/turn_bounded honestly
report UNKNOWN; approach_tag is closed-loop on the camera and still reports SUCCEEDED/FAILED.
"""
import json
import os
from dataclasses import asdict, dataclass


@dataclass
class DriveCalib:
    track_width_m: float = 0.16    # left-wheel to right-wheel centre distance (tape measure)
    mps_at_full: float = 0.45      # ground speed at duty 1.0 (calibrate_motors.py speed)
    deadband: float = 0.25         # highest duty that does NOT move the robot on the floor
    max_duty: float = 0.8          # never exceed (current, battery sag, tipping)
    left_sign: int = 1             # flip to -1 if that side runs backwards
    right_sign: int = 1
    left_trim: float = 1.0         # <1 slows the faster side so straight drives stay straight
    right_trim: float = 1.0
    turn_slip: float = 1.6         # skid-steer: commanded wheel diff needed per unit of real turn
    # Hiwonder board wiring (from tools/hw_test.py): which motor IDs are the left/right wheels
    left_motor_ids: tuple = (1, 3)
    right_motor_ids: tuple = (2, 4)
    motor_signs: dict = None       # {motor_id: +1/-1} so positive duty = wheel rolls forward
    # Claw: PWM servo channel and pulse widths (us) found with `hw_test.py servo`
    claw_servo_id: int = 1
    claw_open_us: int = 1500
    claw_closed_us: int = 1500     # placeholder = no motion until calibrated

    @classmethod
    def load(cls, path="drive_calib.json"):
        c = cls()
        if os.path.exists(path):
            for k, v in json.load(open(path)).items():
                setattr(c, k, v)
        c.motor_signs = {int(k): int(v) for k, v in (c.motor_signs or {}).items()}
        return c

    def save(self, path="drive_calib.json"):
        json.dump(asdict(self), open(path, "w"), indent=2)


def speed_to_duty(speed: float, c: DriveCalib) -> float:
    if abs(speed) < 1e-4:
        return 0.0
    k = c.mps_at_full / (1.0 - c.deadband)     # m/s per unit duty above the deadband
    d = c.deadband + abs(speed) / k
    return min(d, c.max_duty) * (1 if speed > 0 else -1)


def wheel_duties(v: float, w: float, c: DriveCalib) -> tuple[float, float]:
    half = w * c.track_width_m / 2 * c.turn_slip
    left = speed_to_duty(v - half, c) * c.left_trim * c.left_sign
    right = speed_to_duty(v + half, c) * c.right_trim * c.right_sign
    return left, right


class GpioMotors:
    """4 DC motors on H-bridge drivers (TB6612 / L298N / DRV8833) wired to Pi 5 GPIO.

    pins = {"fl": (fwd, bwd, enable_or_None), "rl": ..., "fr": ..., "rr": ...}  (BCM numbers)
    gpiozero uses the lgpio backend on the Pi 5. Note: software PWM; fine for DC motors.
    """
    def __init__(self, pins: dict):
        from gpiozero import Motor
        self.m = {k: Motor(f, b, enable=e, pwm=True) for k, (f, b, e) in pins.items()}

    def set(self, left: float, right: float):
        for k in ("fl", "rl"):
            self.m[k].value = max(-1.0, min(1.0, left))
        for k in ("fr", "rr"):
            self.m[k].value = max(-1.0, min(1.0, right))

    def stop(self):
        for m in self.m.values():
            m.stop()

    def close(self):
        self.stop()
        for m in self.m.values():
            m.close()


class VendorBoardMotors:
    """Robot kits with an expansion/controller board (e.g. an STM32 on serial or I2C) ship their own
    Python SDK with a set-motor-duty call. Wrap it here; don't reimplement the protocol.
    Fill in after r01_probe shows the board and the vendor scripts."""
    def __init__(self):
        raise NotImplementedError("wrap the vendor SDK's motor call here (R01)")

    def set(self, left: float, right: float):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def close(self):
        pass
