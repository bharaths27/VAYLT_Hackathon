"""Jog the arm with the keyboard and save named poses to arm_poses.json.
Run from ~/engram_robot with the adapter server stopped:   sudo python3 tools/arm_pose.py

Keys:
  3 4 5 6   select joint: wrist / elbow / shoulder / base     (1 = claw, for testing only)
  w / s     +50 / -50 us        W / S   +10 / -10 us
  n         save the current arm pose under a name (e.g. home, look, reach, carry)
  g         go to a saved pose      l   list saved poses
  q         quit (arm stays where it is)

Poses the robot uses:
  home   folded/safe, also the startup pose
  look   (optional) camera aimed ahead while driving to a tag
  reach  claw down at the object, open, ready to close
  carry  object lifted clear of the floor
The claw is never part of a pose; gripper_open/gripper_close control it.
"""
import json
import os
import sys
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drivers.hiwonder_board import HiwonderBoard  # noqa: E402

FILE = "arm_poses.json"
NAMES = {1: "claw", 3: "wrist", 4: "elbow", 5: "shoulder", 6: "base"}
JOINTS = (3, 4, 5, 6)

poses = json.load(open(FILE)) if os.path.exists(FILE) else {}
cur = {j: int(poses.get("home", {}).get(str(j), 1500)) for j in JOINTS}
cur[1] = 1500
sel = 4

b = HiwonderBoard()
fd = sys.stdin.fileno()
saved_tty = termios.tcgetattr(fd)


def line(prompt):
    termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)
    try:
        return input(prompt).strip()
    finally:
        tty.setcbreak(fd)


def status():
    parts = [f"{'>' if j == sel else ' '}{NAMES[j]}={cur[j]}" for j in JOINTS]
    print("\r" + "  ".join(parts) + "      ", end="", flush=True)


try:
    start = "saved 'home'" if "home" in poses else "all joints at 1500 (centre)"
    input(f"Arm area clear? Enter to move slowly to {start}...")
    b.set_pwm_servos([(j, cur[j]) for j in JOINTS], 1500)
    time.sleep(1.6)
    print(__doc__.split("Keys:")[1].split("Poses")[0])
    tty.setcbreak(fd)
    status()
    while True:
        k = sys.stdin.read(1)
        if k in "13456":
            sel = int(k)
            if sel == 1:
                cur.setdefault(1, 1500)
        elif k in "wsWS":
            step = {"w": 50, "s": -50, "W": 10, "S": -10}[k]
            cur[sel] = max(500, min(2500, cur[sel] + step))
            b.set_pwm_servo(sel, cur[sel], 150)
        elif k == "n":
            name = line("\npose name: ")
            if name:
                poses[name] = {str(j): cur[j] for j in JOINTS}
                json.dump(poses, open(FILE, "w"), indent=2)
                print(f"saved '{name}': {poses[name]}")
        elif k == "l":
            print("\n" + json.dumps(poses, indent=1))
        elif k == "g":
            name = line("\ngo to pose: ")
            if name in poses:
                for j in JOINTS:
                    cur[j] = int(poses[name][str(j)])
                b.set_pwm_servos([(j, cur[j]) for j in JOINTS], 1000)
                time.sleep(1.1)
            else:
                print(f"no pose '{name}'")
        elif k == "q":
            break
        status()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)
    print()
    b.close()
