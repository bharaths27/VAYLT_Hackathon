"""Push-to-talk voice client (the "voice in and out" in the README).

Press Enter, speak, press Enter again. The recording is turned into text on this
computer (no cloud), shown on screen, and sent to the ENGRAM server with
POST /commands, the same entry point as typed commands. The reply is shown and,
with --speak, read out loud.

Spoken commands only ever PROPOSE a plan. A person still approves it before the
robot moves, so a misheard word cannot move the robot on its own.

Run from the repo root (the ENGRAM server must be running):

    python -m perception.voice                  # talk
    python -m perception.voice --typed          # type instead (always works)
    python -m perception.voice --speak          # also say replies out loud
    python -m perception.voice --confirm        # check the transcript before sending

Speech engines:
    --engine faster-whisper   (default) pip install faster-whisper
                              The model downloads once on first use, then works offline.
    --engine whisper-cpp      brew install whisper-cpp, plus a ggml model file:
                              --model models/ggml-base.en.bin

Needs: pip install sounddevice numpy
On a Mac the first run asks for microphone permission for Terminal (or VS Code).
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARENA = REPO / "fixtures" / "lab" / "arena.json"
SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
# Talking to the server
# ---------------------------------------------------------------------------
def send_command(server: str, text: str, who: str) -> dict:
    body = json.dumps({"text": text, "who": who}).encode()
    req = urllib.request.Request(server + "/commands", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:  # the model on the Pi can take about a minute
        return json.loads(resp.read())


def describe_reply(r: dict) -> tuple[str, str]:
    """(text for the screen, short sentence to say out loud)."""
    kind = r.get("kind")
    if kind == "clarify" or r.get("needs_clarification"):  # unclear or misheard: the reply asks, and may say it stopped
        reply = str(r.get("reply") or r.get("reason"))
        return f"{'STOPPED; ' if r.get('stopped') else ''}PLEASE CLARIFY  {reply}", reply
    if kind == "answer":
        lines = [f"FROM MEMORY  {a['kind']}: {a['text']}" for a in r.get("answers", [])[:5]]
        lines += [f"    unknown: {m}" for m in r.get("missing", [])]
        return "\n".join(lines) or "FROM MEMORY  nothing recorded", str(r.get("reply"))
    if kind == "taught":
        n = len(r.get("rules", []))
        return f"LEARNED  saved as a source, {n} rule(s) found", f"Got it. I saved that{' and found a rule' if n else ''}."
    if kind == "scheduled":
        new = "saved" if r.get("created") else "already saved"
        return (f"SCHEDULED  when '{r.get('trigger')}': {r.get('request')}  ({new})",
                "Okay. I'll do that when it happens.")
    status = r.get("status")
    if status == "refused":
        return f"NOT PLANNED  {r.get('reason')}", str(r.get("reason"))
    plan = r.get("plan") or {}
    steps = [f"{s['op']}({', '.join(str(v) for v in (s.get('args') or {}).values())})"
             for s in plan.get("steps", []) if s.get("op") != "speak"]
    check = r.get("check") or {}
    lines = [f"PLAN ({status}, checker {'ok' if check.get('ok') else 'rejected'}, "
             f"source: {r.get('source')}, planner calls: {r.get('planner_calls')})"]
    lines += [f"    {i}. {s}" for i, s in enumerate(steps, 1)]
    lines += [f"    ! {e}" for e in check.get("errors", [])]
    if status == "proposed":
        lines.append(f"    waiting for approval (operation {r.get('operation_id')})")
        return "\n".join(lines), "I have a plan. Please check it and approve it."
    return "\n".join(lines), "I made a plan, but the safety checker rejected it."


# ---------------------------------------------------------------------------
# Recording and transcription
# ---------------------------------------------------------------------------
def record_until_enter(max_seconds: float = 15.0):
    """Record from the default mic until Enter is pressed (or max_seconds)."""
    import numpy as np
    import sounddevice as sd

    chunks: queue.Queue = queue.Queue()
    stop = threading.Event()

    def on_audio(indata, frames, time_info, status):
        chunks.put(indata.copy())

    def wait_enter():
        sys.stdin.readline()
        stop.set()

    threading.Thread(target=wait_enter, daemon=True).start()
    start = time.time()
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=on_audio):
        while not stop.is_set() and time.time() - start < max_seconds:
            time.sleep(0.05)
    parts = []
    while not chunks.empty():
        parts.append(chunks.get())
    return np.concatenate(parts).flatten() if parts else np.zeros(0, dtype="int16")


def save_wav(audio, path: Path) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())


def vocabulary_hint() -> str:
    """Names the recognizer should expect, from the arena file. Helps with 'Sample B' vs 'sample be'."""
    try:
        arena = json.loads(ARENA.read_text())
        objects = ", ".join(o["name"] for o in arena["objects"])
        zones = ", ".join(z.replace("_", " ") for z in arena["zones"])
        return f"Lab robot commands. Objects: {objects}. Places: {zones}."
    except (OSError, KeyError, ValueError):
        return ""


class Transcriber:
    def __init__(self, engine: str, model: str, hint: str):
        self.engine, self.model_name, self.hint = engine, model, hint
        if engine == "faster-whisper":
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise SystemExit("faster-whisper is not installed: pip install faster-whisper "
                                 "(or use --engine whisper-cpp, or --typed)")
            print(f"Loading speech model '{model}' (the first time it downloads, then it works offline)...")
            self.model = WhisperModel(model, device="cpu", compute_type="int8")
        elif engine == "whisper-cpp":
            self.binary = next((shutil.which(b) for b in ("whisper-cli", "whisper-cpp", "main") if shutil.which(b)), None)
            if not self.binary:
                raise SystemExit("whisper.cpp not found. Install it (brew install whisper-cpp) or use --engine faster-whisper")
            if not Path(model).exists():
                raise SystemExit(f"whisper.cpp needs a model file; '{model}' does not exist. Pass --model path/to/ggml-base.en.bin")
        else:
            raise SystemExit(f"unknown engine {engine}")

    def transcribe(self, audio) -> str:
        if self.engine == "faster-whisper":
            segments, _ = self.model.transcribe(audio.astype("float32") / 32768.0, language="en",
                                                initial_prompt=self.hint or None, beam_size=5,
                                                vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "clip.wav"
            save_wav(audio, wav)
            cmd = [self.binary, "-m", self.model_name, "-f", str(wav), "-nt", "-np", "-l", "en"]
            if self.hint:
                cmd += ["--prompt", self.hint]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip()[-300:])
            return " ".join(line.strip() for line in out.stdout.splitlines() if line.strip())


_DEICTIC = [
    re.compile(r"\b(that|this) (one|thing|object|sample|item)\b", re.IGNORECASE),
    re.compile(r"\b(that|this)\b", re.IGNORECASE),
    re.compile(r"\bit\b", re.IGNORECASE),
]


def object_names() -> list[str]:
    try:
        return [o["name"] for o in json.loads(ARENA.read_text())["objects"]]
    except (OSError, KeyError, ValueError):
        return []


def fill_pointing(text: str, pointing: dict | None, names: list[str] | None = None) -> str:
    """Turn "bring me that one" into "bring me the Beaker" when someone just pointed at the beaker.

    Left alone when nobody pointed recently, when the sentence already names an object,
    or when it is a "remember ..." note (there "that" is just grammar).
    """
    if not pointing or text.strip().lower().startswith("remember"):
        return text
    low = text.lower()
    if any(n.lower() in low for n in (names if names is not None else object_names())):
        return text
    for pattern in _DEICTIC:
        if pattern.search(text):
            return pattern.sub(f"the {pointing['name']}", text, count=1)
    return text


def say(text: str) -> None:
    """Speak with the system voice (macOS 'say', or 'espeak' on Linux/Jetson). Piper can replace this later."""
    for cmd in (["say", text], ["espeak", text]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=False)
            return


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Push-to-talk voice client for ENGRAM Cortex")
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--who", default="voice", help="name stored with each command")
    ap.add_argument("--typed", action="store_true", help="type commands instead of speaking")
    ap.add_argument("--engine", default="faster-whisper", choices=["faster-whisper", "whisper-cpp"])
    ap.add_argument("--model", default="base.en", help="faster-whisper size (tiny.en, base.en) or whisper.cpp model file")
    ap.add_argument("--speak", action="store_true", help="say replies out loud")
    ap.add_argument("--confirm", action="store_true", help="ask before sending what was heard")
    ap.add_argument("--max-seconds", type=float, default=15.0)
    ap.add_argument("--no-pointing", action="store_true", help="don't fill in 'that one' from pointing")
    ap.add_argument("--pointing-age", type=float, default=8.0, help="seconds a pointing gesture stays valid")
    args = ap.parse_args()

    try:
        urllib.request.urlopen(args.server + "/health", timeout=5).read()
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"ENGRAM server not reachable at {args.server} ({e}). Start it first.")

    transcriber = None if args.typed else Transcriber(args.engine, args.model, vocabulary_hint())
    print("\nReady. " + ("Type a command and press Enter." if args.typed
                         else "Press Enter to start talking, Enter again to stop.") + " Ctrl+C to quit.\n")
    try:
        while True:
            if args.typed:
                text = input("you> ").strip()
            else:
                input("[Enter to talk] ")
                print("  listening... (Enter to stop)")
                audio = record_until_enter(args.max_seconds)
                if len(audio) < SAMPLE_RATE * 0.3:
                    print("  too short, try again\n")
                    continue
                t0 = time.perf_counter()
                text = transcriber.transcribe(audio)
                print(f"  heard: \"{text}\"  ({time.perf_counter() - t0:.2f}s)")
            if not text and args.typed:
                continue  # an empty line: nothing was said
            if not text:  # a recording with no words in it: misheard. The server answers it and stops a running plan.
                print("  (nothing heard)")
            if not args.no_pointing:
                from engram.vision.gestures import recent_pointing
                filled = fill_pointing(text, recent_pointing(args.pointing_age))
                if filled != text:
                    print(f"  pointing: \"{text}\" -> \"{filled}\"")
                    text = filled
            if args.confirm and input("  send this? [Y/n] ").strip().lower().startswith("n"):
                print("  not sent\n")
                continue
            try:
                screen, spoken = describe_reply(send_command(args.server, text, args.who))
            except (urllib.error.URLError, OSError) as e:
                print(f"  could not reach the server: {e}\n")
                continue
            print("  " + screen.replace("\n", "\n  ") + "\n")
            if args.speak:
                say(spoken)
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")


if __name__ == "__main__":
    main()
