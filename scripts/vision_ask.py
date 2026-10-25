"""Ask the intent model about camera frames from the command line. Advice only: this never moves the robot.

    export TYPESAFE_API_KEY=...            (read it with: read -rsp 'key: ' TYPESAFE_API_KEY; export TYPESAFE_API_KEY)
    export TYPESAFE_INTENT_MODEL=<model id from the TypeSafe console>

    python scripts/vision_ask.py hand  --image photo.jpg
    python scripts/vision_ask.py hand  --robot http://star-pi.local:8200 --every 1.0    (the robot's C920, live)
    python scripts/vision_ask.py hand  --webcam 0                                      (a laptop camera)
    python scripts/vision_ask.py scene --image room.jpg
    python scripts/vision_ask.py find  --what "pill bottle" --robot http://star-pi.local:8200

One JSON line per frame. Errors print {"error": ...} and the loop carries on.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engram.vision.intent_vision import (VisionClient, VisionError, classify_hand_signal,  # noqa: E402
                                         find_object, scene_check)


def robot_frame(base: str) -> bytes:
    with urllib.request.urlopen(base.rstrip("/") + "/robot/frame?annotate=false&quality=85", timeout=5) as r:
        return r.read()


def webcam_frame(cap) -> bytes:
    import cv2

    ok, img = cap.read()
    if not ok:
        raise VisionError("webcam gave no frame")
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return jpg.tobytes()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["hand", "scene", "find"])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", nargs="+", help="image file(s)")
    src.add_argument("--robot", help="robot server base URL, e.g. http://star-pi.local:8200")
    src.add_argument("--webcam", type=int, help="local camera index")
    ap.add_argument("--what", help="object to find (find mode)")
    ap.add_argument("--every", type=float, default=0.0,
                    help="start a live frame every N seconds, or as soon as the last answer is back if that is "
                         "slower (0 = one frame)")
    ap.add_argument("--timeout", type=float, default=4.0)
    a = ap.parse_args()
    if a.mode == "find" and not a.what:
        ap.error("find needs --what")

    try:
        client = VisionClient.from_env(timeout_s=a.timeout)
    except VisionError as e:
        print(json.dumps({"error": str(e)}))
        return 2

    def ask(img: bytes) -> dict:
        if a.mode == "hand":
            return classify_hand_signal(img, client)
        if a.mode == "scene":
            return scene_check(img, client)
        return find_object(img, a.what, client)

    def run(label: str, get) -> None:
        try:
            out = ask(get())
            out.pop("probs", None)
            out.pop("posture_probs", None)
            print(json.dumps({"source": label, **out}), flush=True)
        except (VisionError, OSError) as e:
            print(json.dumps({"source": label, "error": str(e)}), flush=True)

    if a.image:
        for p in a.image:
            run(p, lambda p=p: Path(p).read_bytes())
        return 0
    cap = None
    if a.webcam is not None:
        import cv2

        cap = cv2.VideoCapture(a.webcam)
    label = a.robot or f"webcam {a.webcam}"
    get = (lambda: robot_frame(a.robot)) if a.robot else (lambda: webcam_frame(cap))
    try:
        next_at = time.monotonic()
        while True:
            run(label, get)
            if a.every <= 0:
                return 0
            next_at += a.every  # a fixed cadence: each frame starts `every` seconds after the last one started,
            time.sleep(max(0.0, next_at - time.monotonic()))  # or at once when an answer took longer than that
            next_at = max(next_at, time.monotonic())
    except KeyboardInterrupt:
        return 0
    finally:
        if cap is not None:
            cap.release()


if __name__ == "__main__":
    raise SystemExit(main())
