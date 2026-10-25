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
import http.client
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engram.vision.intent_vision import (MAX_TIMEOUT_S, VisionClient, VisionError,  # noqa: E402
                                         classify_hand_signal, find_object, scene_check)


def _seconds(low: float, high: float, low_ok: bool):
    """An argparse type for a finite number of seconds in range: `--every nan` would otherwise turn the live loop into
    back-to-back calls to the paid API (nan <= 0 is false, and sleep(max(0.0, nan)) sleeps 0 s)."""
    def parse(text: str) -> float:
        v = float(text)
        if not math.isfinite(v) or v > high or v < low or (v == low and not low_ok):
            raise argparse.ArgumentTypeError(f"must be a number of seconds from {low:g} ({'inclusive' if low_ok else 'exclusive'}) to {high:g}")
        return v
    return parse


def _robot_url(text: str) -> str:
    """An argparse type: the robot server's http(s) URL. An empty --robot satisfied the required source group but read
    as false below, so the CLI fell through to the webcam path with no camera (luna round 8)."""
    u = urllib.parse.urlsplit(text)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise argparse.ArgumentTypeError("must be the robot server's http:// or https:// URL")
    return text


def robot_frame(base: str) -> bytes:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/robot/frame?annotate=false&quality=85", timeout=5) as r:
            return r.read()
    except http.client.HTTPException as e:  # e.g. IncompleteRead: a truncated reply is a failed frame, not a crash
        raise VisionError(f"robot frame: {type(e).__name__}") from None


def open_webcam(index: int):
    """The opened camera. No OpenCV, or a camera that does not open, is a VisionError: a JSON line, not a traceback."""
    try:
        import cv2
    except ImportError:
        raise VisionError("--webcam needs OpenCV: pip install opencv-python") from None
    try:
        cap = cv2.VideoCapture(index)
        opened = cap.isOpened()
    except cv2.error as e:
        raise VisionError(f"webcam {index}: {type(e).__name__}") from None
    if not opened:
        cap.release()
        raise VisionError(f"webcam {index} did not open")
    return cap


def webcam_frame(cap) -> bytes:
    import cv2

    try:
        ok, img = cap.read()
        if not ok or img is None:
            raise VisionError("webcam gave no frame")
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    except cv2.error as e:  # OpenCV's own error is not an OSError: a bad frame is a failed frame, not a crash
        raise VisionError(f"webcam frame: {type(e).__name__}") from None
    if not ok or jpg is None or not len(jpg):
        raise VisionError("webcam frame could not be encoded as JPEG")
    return jpg.tobytes()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["hand", "scene", "find"])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", nargs="+", help="image file(s)")
    src.add_argument("--robot", type=_robot_url, help="robot server base URL, e.g. http://star-pi.local:8200")
    src.add_argument("--webcam", type=int, help="local camera index")
    ap.add_argument("--what", help="object to find (find mode)")
    ap.add_argument("--every", type=_seconds(0.0, 3600.0, low_ok=True), default=0.0,
                    help="start a live frame every N seconds, or as soon as the last answer is back if that is "
                         "slower (0 = one frame)")
    ap.add_argument("--timeout", type=_seconds(0.0, MAX_TIMEOUT_S, low_ok=False), default=4.0)
    a = ap.parse_args()
    if a.mode == "find":
        a.what = " ".join((a.what or "").split())
        if not a.what or len(a.what) > 80:
            ap.error("find needs --what with 1 to 80 characters")

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

    def run(label: str, get) -> bool:
        try:
            out = ask(get())
            out.pop("probs", None)
            out.pop("posture_probs", None)
            print(json.dumps({"source": label, **out}), flush=True)
            return True
        except (VisionError, OSError, ValueError) as e:
            print(json.dumps({"source": label, "error": str(e)}), flush=True)
            return False

    if a.image:  # exit 1 if any file failed, so scripts can tell
        results = [run(p, lambda p=p: Path(p).read_bytes()) for p in a.image]
        return 0 if all(results) else 1
    cap = None
    label = a.robot if a.robot is not None else f"webcam {a.webcam}"
    if a.webcam is not None:
        try:
            cap = open_webcam(a.webcam)
        except VisionError as e:
            print(json.dumps({"source": label, "error": str(e)}), flush=True)
            return 1
    get = (lambda: robot_frame(a.robot)) if a.robot is not None else (lambda: webcam_frame(cap))
    try:
        next_at = time.monotonic()
        while True:
            ok = run(label, get)
            if a.every <= 0:
                return 0 if ok else 1
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
