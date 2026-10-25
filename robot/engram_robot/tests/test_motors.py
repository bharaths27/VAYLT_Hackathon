import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drivers.motors import DriveCalib, speed_to_duty, wheel_duties


def test_deadband_and_linear_mapping():
    c = DriveCalib(mps_at_full=0.45, deadband=0.25, max_duty=0.8)
    assert speed_to_duty(0.0, c) == 0.0
    assert abs(speed_to_duty(0.45 * 0.5, c) - (0.25 + 0.75 * 0.5)) < 1e-9   # half speed above deadband
    assert speed_to_duty(10, c) == 0.8 and speed_to_duty(-10, c) == -0.8    # clamped
    assert 0.25 < speed_to_duty(0.05, c) < 0.4                              # creep is above deadband


def test_turn_signs_trim_and_slip():
    c = DriveCalib(right_sign=-1, left_trim=0.9)
    l, r = wheel_duties(0.0, 1.0, c)            # turn left: left wheels back, right forward
    assert l < 0 and r < 0                       # right sign flipped by wiring
    l2, r2 = wheel_duties(0.1, 0.0, c)
    assert l2 > 0 and r2 < 0 and abs(l2 / -r2 - 0.9) < 1e-9
