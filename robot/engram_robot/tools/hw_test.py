"""Step-by-step hardware test for star-pi's Hiwonder board. LIFT THE ROBOT so wheels spin freely.
Stop the adapter server first (only one program may use /dev/ttyAMA0).

  python3 tools/hw_test.py battery          # read-only: battery voltage + frame counts
  python3 tools/hw_test.py beep             # harmless: confirms commands are understood
  python3 tools/hw_test.py wheel 1          # motor 1 forward, 25% duty, 0.5 s  (repeat for 2, 3, 4)
  python3 tools/hw_test.py wheel 1 -25      # same, backward
  python3 tools/hw_test.py servo 1 1500     # PWM servo 1 to centre (500..2500 us) - find the claw
  python3 tools/hw_test.py setup            # spin each motor, you say which wheel/direction -> drive_calib.json
  python3 tools/hw_test.py forward          # all 4 wheels forward together (uses drive_calib.json)
  python3 tools/hw_test.py backward
  python3 tools/hw_test.py spin             # turn left in place, then right
  python3 tools/hw_test.py stop             # all motors 0
Every motor command ends with a stop, also on Ctrl-C or error.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drivers.hiwonder_board import HiwonderBoard, HiwonderMotors  # noqa: E402
from drivers.motors import DriveCalib  # noqa: E402

MAX_TEST_DUTY = 40

if len(sys.argv) < 2:
    print(__doc__)
    raise SystemExit(0)
cmd, args = sys.argv[1], sys.argv[2:]
b = HiwonderBoard()
try:
    b.stop_motors()
    time.sleep(1.0)
    v = f"{b.battery_v:.2f} V" if b.battery_v else "no report yet"
    print(f"battery {v}, frames ok {b.frames_ok}, bad {b.frames_bad}")
    if b.battery_v and b.battery_v < 7.0:
        print("WARNING: battery low (< 7.0 V for a 2-cell pack); motors may brown out the board")

    if cmd == "beep":
        b.beep(2000, 150, 100, 2)
        time.sleep(0.6)
        print("You should have heard two beeps. No beep = the command layout differs; tell Claude.")
    elif cmd == "wheel":
        mid = int(args[0])
        duty = max(-MAX_TEST_DUTY, min(MAX_TEST_DUTY, float(args[1]) if len(args) > 1 else 25.0))
        secs = min(1.0, float(args[2]) if len(args) > 2 else 0.5)
        input(f"Robot lifted, wheels free? Enter to run motor {mid} at {duty:+.0f}% for {secs}s...")
        b.set_motor_duty([(mid, duty)])
        time.sleep(secs)
        b.stop_motors()
        print("Which wheel turned (front-left/front-right/rear-left/rear-right), and which direction?")
    elif cmd == "servo":
        sid, pulse = int(args[0]), int(args[1])
        b.set_pwm_servo(sid, pulse, 500)
        time.sleep(0.7)
        print(f"servo {sid} -> {pulse} us. Step in 100 us increments to find claw open/closed.")
    elif cmd == "setup":
        c = DriveCalib.load()
        left, right, signs = [], [], {}
        input("Robot lifted, wheels free? Enter to start...")
        for mid in (1, 2, 3, 4):
            while True:
                b.set_motor_duty([(mid, 25)])
                time.sleep(0.6)
                b.stop_motors()
                w = input(f"motor {mid}: which wheel? [fl/fr/rl/rr, Enter = spin again] ").strip().lower()
                if w in ("fl", "fr", "rl", "rr"):
                    break
            fwd = input(f"motor {mid}: did it roll FORWARD (as if the robot drove ahead)? [y/n] ").strip().lower()
            signs[mid] = 1 if fwd.startswith("y") else -1
            (left if w[1] == "l" else right).append(mid)
        if len(left) != 2 or len(right) != 2:
            raise SystemExit(f"expected 2 left + 2 right wheels, got left={left} right={right}; run setup again")
        c.left_motor_ids, c.right_motor_ids, c.motor_signs = left, right, signs
        c.save()
        print(f"saved drive_calib.json: left={left} right={right} signs={signs}")
    elif cmd in ("forward", "backward", "spin"):
        c = DriveCalib.load()
        m = HiwonderMotors(b, c.left_motor_ids, c.right_motor_ids, c.motor_signs)
        input("Robot lifted, wheels free? Enter to run...")
        if cmd == "spin":
            for l, r, name in ((-0.3, 0.3, "left"), (0.3, -0.3, "right")):
                print(f"spinning {name}")
                m.set(l, r)
                time.sleep(0.6)
                b.stop_motors()
                time.sleep(0.4)
        else:
            d = 0.3 if cmd == "forward" else -0.3
            m.set(d, d)
            time.sleep(0.6)
            b.stop_motors()
        print("All four should have turned the same way (spin: left side opposite to right side).")
    elif cmd == "battery":
        time.sleep(1.0)
        print(f"battery {b.battery_v} V")
    elif cmd != "stop":
        print(__doc__)
finally:
    b.stop_motors()
    time.sleep(0.1)
    b.close()
