"""Send spoken words to the brain. Speech to text is Brian's voice server on the Pi 5 (whisper.cpp).

    python scripts/voice_bridge.py --audio clip.m4a          # Brian's server turns it into text, then the brain
    python scripts/voice_bridge.py --text "remember the goggles live at the PPE station"
    python scripts/voice_bridge.py                            # type lines; each one goes to the brain
    add --dry to print what would be sent and send nothing to the brain

Brian's server: https://localhost:8444/api/transcribe (self signed certificate on this Pi only).
Brain: POST {brain}/commands {"text": ..., "who": "voice"}. "remember ...", "when X, Y" and tasks
all work. Tasks come back as proposals that still need a person's approval; nothing moves from here.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.request
import uuid
from pathlib import Path

VOICE = "https://localhost:8444"
BRAIN = "http://127.0.0.1:8001"


def transcribe(audio: Path, voice_url: str = VOICE) -> str:
    """Multipart upload to Brian's /api/transcribe. His certificate is self signed, local only."""
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"{audio.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + audio.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(voice_url.rstrip("/") + "/api/transcribe", body,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
        return json.loads(r.read())["text"]


def clean(text: str) -> str:
    """Drop whisper noise tags like [BLANK_AUDIO] and extra spaces."""
    import re

    return re.sub(r"\s+", " ", re.sub(r"\[.*?\]|\(.*?\)", " ", text)).strip()


def send(text: str, brain_url: str = BRAIN, dry: bool = False) -> dict:
    body = {"text": text, "who": "voice"}
    if dry:
        return {"dry": True, "would_send": body, "to": brain_url.rstrip("/") + "/commands"}
    req = urllib.request.Request(brain_url.rstrip("/") + "/commands", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def summary(out: dict) -> str:
    """One line a person can read: the reply, or the plan waiting for approval."""
    if out.get("dry"):
        return f"(dry) would send {out['would_send']} to {out['to']}"
    if out.get("plan"):
        steps = ", ".join(s["op"] for s in out["plan"].get("steps", []))
        return f"{out.get('status')}: {steps} (from {out.get('source')}); waiting for approval {out.get('operation_id')}"
    for key in ("reply", "answer", "message", "status"):
        if out.get(key):
            return str(out[key])
    return json.dumps(out)[:300]


def handle(text: str, brain_url: str, dry: bool, spoken: bool = False) -> str:
    """A spoken clip with nothing in it is sent all the same: the brain answers a misheard command, and stops a
    plan that is running ("I stopped. ... Please clarify."). Only an empty typed line is skipped."""
    text = clean(text)
    if not text and not spoken:
        return "heard nothing"
    return f"heard: {text or '(nothing)'}\n{summary(send(text, brain_url, dry))}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", type=Path)
    ap.add_argument("--text")
    ap.add_argument("--brain", default=BRAIN)
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.audio:
        print(handle(transcribe(a.audio, a.voice), a.brain, a.dry, spoken=True))
    elif a.text:
        print(handle(a.text, a.brain, a.dry))
    else:
        for line in sys.stdin:
            print(handle(line, a.brain, a.dry), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
