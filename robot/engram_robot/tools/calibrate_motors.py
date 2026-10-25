"""R01/R04: calibrate DC wheel motors. Run ON THE PI with the adapter server STOPPED.
Short bursts only (<= 1.5 s), Ctrl-C stops the motors. Keep a hand near the robot.

  python tools/calibrate_motors.py sides       # wheels LIFTED: fixes left/right sign
  python tools/calibrate_motors.py deadband    # on floor, tag 7 straight ahead ~0.8 m
  python tools/calibrate_motors.py speed       # on floor, same setup, re-aim between runs
  python tools/calibrate_motors.py turn        # on floor, mark the heading with tape

Results are written to drive_calib.json, which RealDriver loads.
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drivers.real_template import RealDriver  # noqa: E402
from robot.config import Config  # noqa: E402
from robot.vision import TagDetector  # noqa: E402

TAG = 7
cfg = Config.load("configs/ultrasonic.json")
d = RealDriver(cfg)
det = TagDetector(cfg)
c = d.calib
time.sleep(1.5)


def tag_dist():
    seen = []
    for _ in range(7):
        t = det.detect(d.get_frame()).get(TAG)
        if t:
            seen.append(t.distance_m)
        time.sleep(0.05)
    if len(seen) < 4:
        raise SystemExit(f"tag {TAG} not reliably visible; aim the robot at it")
    return statistics.median(seen)


def front_clear(min_m=0.5):
    _, _, r = d.lidar_scan()
    ok = len(r) and float(min(r)) > min_m
    if not ok:
        raise SystemExit(f"front range {min(r) if len(r) else 'none'} m < {min_m} m: refusing to drive")


def burst(left, right, secs):
    try:
        d.motors.set(left * c.left_sign, right * c.right_sign)
        time.sleep(secs)
    finally:
        d.motors.stop()
    time.sleep(0.4)


mode = sys.argv[1] if len(sys.argv) > 1 else "help"
try:
    if mode == "sides":
        for side in ("left", "right"):
            input(f"Wheels lifted? Press Enter to spin the {side} side forward for 1 s...")
            burst(0.4 if side == "left" else 0, 0.4 if side == "right" else 0, 1.0)
            if input(f"Did the {side} wheels spin FORWARD? [y/n] ").strip().lower() == "n":
                setattr(c, f"{side}_sign", -getattr(c, f"{side}_sign"))
    elif mode == "deadband":
        for duty in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]:
            front_clear()
            before = tag_dist()
            burst(duty, duty, 0.6)
            moved = before - tag_dist()
            print(f"duty {duty:.2f}: moved {moved * 100:.1f} cm")
            if moved > 0.01:
                c.deadband = round(duty - 0.05, 3)
                break
    elif mode == "speed":
        duty, secs, rates = 0.6, 1.0, []
        for i in range(3):
            front_clear(0.7)
            before = tag_dist()
            burst(duty, duty, secs)
            moved = before - tag_dist()
            rates.append(moved / secs)
            print(f"run {i + 1}: {moved * 100:.1f} cm in {secs}s -> {moved / secs:.3f} m/s (incl. spin-up)")
            input("Re-aim at the tag ~0.8 m away, Enter...")
        v = statistics.median(rates)
        c.mps_at_full = round(v / (duty - c.deadband) * (1 - c.deadband), 3)
        print("If it drifted sideways, lower the faster side's trim (left_trim/right_trim) in drive_calib.json")
    elif mode == "turn":
        from drivers.motors import wheel_duties
        w, secs = 1.0, 1.5
        l, r = wheel_duties(0.0, w, c)
        input(f"Will command {w} rad/s left for {secs}s (= {w * secs * 57.3:.0f} deg). Enter...")
        burst(l * c.left_sign, r * c.right_sign, secs)  # undo sign: burst re-applies it
        real = float(input("Measured turn in degrees: "))
        c.turn_slip = round(c.turn_slip * (w * secs * 57.2958) / max(real, 1.0), 3)
    else:
        print(__doc__)
        raise SystemExit(0)
    c.save()
    print("saved drive_calib.json:", c)
finally:
    d.close()
