"""Ask the intent model about one camera frame: which hand signal, who is in the scene, where an object is.

TypeSafe's System One answers typed questions about a still image (JPEG or PNG). Everything here is
ADVICE about one frame: it never moves the robot, approves a plan or counts as a sensor reading. Callers
act only through the brain's normal paths (stop, propose, digest-bound approval).

Images are a preview feature on TypeSafe's side: fine for prototypes, but the request format, accuracy
and limits can change between releases. Measure on your own camera before relying on a threshold.

Configuration comes from the environment and has no defaults in code (as in engram/spark/intent_client.py):
    TYPESAFE_API_KEY       the API key (never logged, never in repr or errors)
    TYPESAFE_INTENT_MODEL  the model id to ask (it must accept images; Box questions need a newer model)
The endpoint is fixed, redirects are never followed, no proxy is used, and every call has a deadline.
Any failure raises VisionError; callers treat it as "no answer" (fail closed).
"""

from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
IMAGES_HEADER = "TypeSafe-Danger-Zone-Images"
DEFAULT_TIMEOUT_S = 4.0
MAX_TIMEOUT_S = 15.0
MIN_SIDE, MAX_SIDE = 280, 1568   # the API rejects images outside these (it never resizes)
DEFAULT_SIDE = 768               # smaller frames answer faster; hand signals read fine at this size

HAND_SIGNALS: dict[str, str] = {
    "open_palm": "An open hand held up with the palm toward the camera, fingers up, like a 'stop' sign.",
    "thumbs_up": "A closed hand with only the thumb raised and pointing up.",
    "thumbs_down": "A closed hand with only the thumb pointing down.",
    "pointing": "One index finger extended, pointing at something.",
    "fist": "A closed fist with no finger or thumb extended.",
    "peace": "The index and middle fingers raised in a V, other fingers folded.",
    "waving": "A raised hand waving side to side to get attention.",
    "none": "No hand signal: no hand visible, hands resting or holding something, or a gesture not listed here.",
}

POSTURES: dict[str, str] = {
    "standing": "Standing or walking.",
    "sitting": "Sitting on a chair, sofa, bed edge or wheelchair.",
    "lying_in_bed": "Lying in a bed.",
    "lying_on_floor": "Lying or collapsed on the floor or the ground, not in a bed.",
    "no_person": "No person is visible.",
}

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class VisionError(Exception):
    """No usable answer. Callers must treat this as 'no answer'."""


class VisionConfigError(VisionError):
    """TYPESAFE_API_KEY or TYPESAFE_INTENT_MODEL is missing."""


MAX_REPLY_BYTES = 1 << 20  # a typed answer is a few hundred bytes; anything this big is not one


def _default_transport(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    """POST with a wall-clock deadline for the whole exchange.

    httpx timeouts bound each phase, not the total, so a server dripping bytes could hold a plain request
    open. The request runs on a daemon thread; the caller stops waiting at the deadline, and the abandoned
    thread stops reading at its next chunk (or its own read timeout)."""
    import httpx  # in requirements.txt

    result: dict[str, Any] = {}
    finished, abandoned = threading.Event(), threading.Event()

    def work() -> None:
        try:
            with httpx.Client(follow_redirects=False, trust_env=False, timeout=httpx.Timeout(timeout_s)) as c:
                with c.stream("POST", url, content=body, headers=headers) as r:
                    parts, size = [], 0
                    for chunk in r.iter_bytes():
                        if abandoned.is_set():
                            return
                        size += len(chunk)
                        if size > MAX_REPLY_BYTES:
                            raise VisionError("reply too large")
                        parts.append(chunk)
                    result["reply"] = (r.status_code, b"".join(parts))
        except BaseException as e:  # noqa: BLE001 - handed to the caller below
            result["error"] = e
        finally:
            finished.set()

    threading.Thread(target=work, name="intent-vision-request", daemon=True).start()
    if not finished.wait(timeout_s):
        abandoned.set()
        raise VisionError("deadline exceeded")
    if "error" in result:
        err = result["error"]
        raise err if isinstance(err, VisionError) else VisionError(f"request failed: {type(err).__name__}")
    return result["reply"]


def prepare_jpeg(data: bytes, max_side: int = DEFAULT_SIDE) -> bytes:
    """Any JPEG/PNG -> an RGB JPEG the API accepts: longest side at most max_side, except that a very narrow
    image is enlarged until its short side reaches the API's 280 px minimum (never past its 1568 px limit).
    Transparent pixels become white, as an image viewer shows them: dropping the alpha alone would reveal (and
    send) whatever colour the hidden pixels hold."""
    if not MIN_SIDE <= max_side <= MAX_SIDE:
        raise ValueError(f"max_side must be in [{MIN_SIDE}, {MAX_SIDE}]")
    try:
        from PIL import Image
    except ImportError:
        return _prepare_cv2(data, max_side)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001 - any decode failure is "not an image"
        raise VisionError(f"not a readable image: {type(e).__name__}") from None
    if "transparency" in img.info or any(band in ("A", "a") for band in img.getbands()):
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    img = img.convert("RGB")
    w, h = img.size
    scale = _scale(w, h, max_side)
    if scale != 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _prepare_cv2(data: bytes, max_side: int) -> bytes:
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise VisionError("needs Pillow or OpenCV to read images: pip install pillow") from None
    try:
        img = _decode_on_white(cv2, np, data)
    except Exception as e:  # noqa: BLE001 - OpenCV raises cv2.error on some inputs (e.g. an empty buffer)
        raise VisionError(f"not a readable image: {type(e).__name__}") from None
    if img is None:
        raise VisionError("not a readable image")
    h, w = img.shape[:2]
    scale = _scale(w, h, max_side)
    try:
        if scale != 1.0:
            img = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))))
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    except Exception as e:  # noqa: BLE001 - same: an OpenCV failure is "no image to send"
        raise VisionError(f"could not prepare the image: {type(e).__name__}") from None
    if not ok:
        raise VisionError("could not encode the image")
    return jpg.tobytes()


def _decode_on_white(cv2, np, data: bytes):
    """BGR pixels, with any alpha composited onto white. A JPEG has no alpha and keeps the one fast decode (camera
    frames); anything else is read once with its alpha, if it has one, and composited."""
    buf = np.frombuffer(data, np.uint8)
    if data[:2] != b"\xff\xd8":
        raw = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
        if raw is not None and raw.ndim == 3 and raw.shape[2] == 4:
            full = float(np.iinfo(raw.dtype).max) if raw.dtype.kind == "u" else 1.0
            alpha = raw[:, :, 3:4].astype(np.float32) / full
            bgr = raw[:, :, :3].astype(np.float32) / full
            return np.clip((bgr * alpha + (1.0 - alpha)) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _scale(w: int, h: int, max_side: int) -> float:
    """Shrink so the longest side is at most max_side; grow so the shortest is at least MIN_SIDE."""
    if w <= 0 or h <= 0:
        raise VisionError("empty image")
    scale = min(1.0, max_side / max(w, h))
    if min(w, h) * scale < MIN_SIDE:
        scale = MIN_SIDE / min(w, h)
        if max(w, h) * scale > MAX_SIDE:
            raise VisionError(f"image too narrow to send ({w}x{h})")
    return scale


@dataclass
class VisionClient:
    """One frame in, typed answers out. Thread-safe: it keeps no per-request state."""

    api_key: str = field(repr=False)
    model: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_side: int = DEFAULT_SIDE
    transport: Transport = field(default=_default_transport, repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or not self.model:
            raise VisionConfigError("set TYPESAFE_API_KEY and TYPESAFE_INTENT_MODEL")
        if not 0.0 < self.timeout_s <= MAX_TIMEOUT_S:
            raise ValueError(f"timeout_s must be in (0, {MAX_TIMEOUT_S}]")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw: Any) -> "VisionClient":
        env = os.environ if env is None else env
        return cls(api_key=env.get("TYPESAFE_API_KEY", "").strip(),
                   model=env.get("TYPESAFE_INTENT_MODEL", "").strip(), **kw)

    def ask(self, image: bytes, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Ask questions about `frame` (the image). Returns {"answers", "model", "latency_ms"}."""
        jpeg = prepare_jpeg(image, self.max_side)
        ref = f"frame-{uuid.uuid4().hex}"
        body = json.dumps({
            "model": self.model,
            "state": {"frame": ref},
            "assets": {ref: {"type": "image", "format": "jpg", "data_base64": base64.b64encode(jpeg).decode()}},
            "questions": questions,
        }).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}",
                   IMAGES_HEADER: "true"}
        t0 = time.perf_counter()
        try:
            status, data = self.transport(ENDPOINT, body, headers, self.timeout_s)
        except VisionError:
            raise
        except Exception as e:  # noqa: BLE001 - network trouble of any kind is "no answer"
            raise VisionError(f"request failed: {type(e).__name__}") from None
        latency_ms = round((time.perf_counter() - t0) * 1000)
        if status != 200:
            raise VisionError(f"HTTP {status}")  # never the body: an error page could echo the request headers
        try:
            reply = json.loads(data)
            answers = reply["answers"]
            if not isinstance(answers, dict) or set(answers) != set(questions):
                raise KeyError("answers")
        except Exception:  # noqa: BLE001 - a reply we can't read is "no answer"
            raise VisionError("unreadable reply") from None
        return {"answers": answers, "model": str(reply.get("model", "")), "latency_ms": latency_ms}


def _unit(x: Any) -> float:
    """A finite number in [0, 1] (not a bool, not a string), or VisionError."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not 0.0 <= x <= 1.0:  # NaN fails the range too
        raise VisionError("unexpected answer shape")
    return float(x)


def _noul(answer: Any) -> float:
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        raise VisionError("unexpected answer shape")
    return _unit(answer.get("noul"))


def _choice(answer: Any, options: Mapping[str, str]) -> tuple[str, float, dict[str, float]]:
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise VisionError("unexpected answer shape")
    probs, choice = answer.get("probabilities"), answer.get("choice")
    if not isinstance(probs, dict) or set(probs) != set(options) or not isinstance(choice, str) or choice not in options:
        raise VisionError("unexpected answer shape")
    return choice, _unit(answer.get("confidence")), {k: _unit(v) for k, v in probs.items()}


def classify_hand_signal(image: bytes, client: VisionClient | None = None) -> dict[str, Any]:
    """Which hand signal is shown? -> {"signal", "probability", "confidence", "probs", "latency_ms", "model"}."""
    client = client or VisionClient.from_env()
    out = client.ask(image, {
        "signal": {"type": "choice",
                   "instructions": "Which hand signal is the person in `frame` making toward the camera?",
                   "criteria": HAND_SIGNALS},
    })
    signal, confidence, probs = _choice(out["answers"]["signal"], HAND_SIGNALS)
    return {"signal": signal, "probability": probs[signal], "confidence": confidence, "probs": probs,
            "latency_ms": out["latency_ms"], "model": out["model"]}


def scene_check(image: bytes, client: VisionClient | None = None) -> dict[str, Any]:
    """Care-room questions in one call: is someone there, on the floor, asking for help; their posture."""
    client = client or VisionClient.from_env()
    out = client.ask(image, {
        "person_present": {"type": "noul", "instructions": "Is at least one person visible in `frame`?"},
        "person_on_floor": {"type": "noul", "instructions":
                            "Is a person in `frame` lying or collapsed on the floor or the ground (not in a bed), "
                            "as after a fall?"},
        "asking_for_help": {"type": "noul", "instructions":
                            "Is a person in `frame` waving, raising a hand or otherwise signalling to get help or "
                            "attention?"},
        "posture": {"type": "choice", "instructions": "What is the main person in `frame` doing?",
                    "criteria": POSTURES},
    })
    a = out["answers"]
    posture, confidence, probs = _choice(a["posture"], POSTURES)
    return {"person_present": _noul(a["person_present"]), "person_on_floor": _noul(a["person_on_floor"]),
            "asking_for_help": _noul(a["asking_for_help"]), "posture": posture,
            "posture_confidence": confidence, "posture_probs": probs,
            "latency_ms": out["latency_ms"], "model": out["model"]}


def find_object(image: bytes, what: str, client: VisionClient | None = None,
                min_present: float = 0.5) -> dict[str, Any]:
    """Is `what` in the frame, and where? -> {"present", "box" (fractions of width/height) or None, ...}.

    A Box question always draws a box, even around nothing, so the box is returned only when the separate
    presence question says the object is there (present >= min_present)."""
    if isinstance(min_present, bool) or not isinstance(min_present, (int, float)) or not 0.0 <= min_present <= 1.0:
        raise ValueError("min_present must be a number in [0, 1]")
    what = " ".join(what.split())
    if not what or len(what) > 80:
        raise ValueError("describe the object in 1 to 80 characters")
    client = client or VisionClient.from_env()
    out = client.ask(image, {
        "present": {"type": "noul", "instructions": f"Is there a {what} visible in `frame`?"},
        "where": {"type": "box", "instructions": f"the {what} in `frame`"},
    })
    present = _noul(out["answers"]["present"])
    box_answer = out["answers"]["where"]
    box = box_answer.get("box") if isinstance(box_answer, dict) and box_answer.get("type") == "box" else None
    edges = ("top", "bottom", "left", "right")
    if not isinstance(box, dict):
        raise VisionError("unexpected answer shape")
    box = {k: _unit(box.get(k)) for k in edges}
    if box["top"] > box["bottom"] or box["left"] > box["right"]:
        raise VisionError("unexpected answer shape")
    box_confidence = _unit(box_answer.get("confidence", 0.0))
    found = present >= min_present
    return {"present": present, "box": box if found else None, "box_confidence": box_confidence if found else 0.0,
            "latency_ms": out["latency_ms"], "model": out["model"]}
