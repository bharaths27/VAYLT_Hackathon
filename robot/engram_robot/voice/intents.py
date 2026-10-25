"""Spoken text -> ONE allowlisted robot command (deterministic, no LLM).

Returns {"op", "args", "say"} or {"op": None, "say": "..."} when nothing matched. It never guesses:
an unrecognized request is refused, not approximated. "stop" is flagged immediate (no approval).
"""
import re

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90}

# whisper.cpp's non-speech markers: exactly the lists in console/server/voice.py (tests/test_voice_bridge.py checks
# it). A bracketed group is a marker only when all of it is one; other words in brackets are speech.
NON_SPEECH = ("blank_audio", "blank audio", "music", "noise", "silence", "inaudible", "applause", "laughter",
              "laughing", "laughs", "cough", "coughing", "coughs", "sigh", "sighs", "breathing", "wind", "static",
              "beep", "beeping", "beeps", "clicking", "typing", "footsteps", "no speech", "foreign language",
              "speaking in foreign language", "sound", "sounds")
NON_SPEECH_DESCRIBERS = ("soft", "upbeat", "gentle", "light", "loud", "quiet", "faint", "distant", "background")
_SOUNDS_RX = "|".join(r"\s+".join(re.escape(part) for part in w.split()) for w in NON_SPEECH)
_MARK_BODY = rf"\s*(?:(?:{'|'.join(NON_SPEECH_DESCRIBERS)})\s+)?(?:{_SOUNDS_RX})(?:\s+(?:playing|blowing))?\s*"
_MARKER = re.compile(rf"\[{_MARK_BODY}\]|\({_MARK_BODY}\)", re.IGNORECASE)


def no_speech(text: str) -> bool:
    """No words in it: empty, punctuation, or only whisper's non-speech markers. "(don't open the claw)" is speech."""
    return re.search(r"\w", _MARKER.sub(" ", text or "")) is None


def words_to_digits(text: str) -> str:
    """'forty five' -> '45', 'one hundred eighty' -> '180', 'seven' -> '7'."""
    out, cur, active = [], 0, False
    for w in text.split():
        if w in UNITS:
            cur, active = cur + UNITS[w], True
        elif w in TENS:
            cur, active = cur + TENS[w], True
        elif w == "hundred" and active:
            cur *= 100
        else:
            if active:
                out.append(str(cur))
                cur, active = 0, False
            out.append(w)
    if active:
        out.append(str(cur))
    return " ".join(out)


def normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", t)  # whisper noise tags like [BLANK_AUDIO]
    t = t.replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return words_to_digits(re.sub(r"\s+", " ", t).strip())


def _distance_m(t: str, default_cm=20):
    m = re.search(r"\b(\d+)\s*(cm|centimeters?|centimetres?|inch|inches|meters?|metres?|m)?\b", t)
    if not m:
        return default_cm / 100
    n, unit = int(m.group(1)), (m.group(2) or "cm")
    if unit.startswith("c"):
        return n / 100
    if unit.startswith("inch"):
        return n * 0.0254
    return float(n)


def parse(text: str, arm_poses=()) -> dict:
    t = normalize(text)
    if not t:
        return {"op": None, "say": "I didn't catch anything."}

    if re.search(r"\b(stop|halt|freeze|emergency)\b", t):
        return {"op": "stop", "args": {}, "immediate": True, "say": "Stopping."}

    m = re.search(r"\b(pick up|pick|grab|fetch|get)\b.*?(\d+)", t)
    if m:
        return {"op": "pick_tagged", "args": {"tag_id": int(m.group(2))}, "say": f"Pick up object {m.group(2)}."}

    m = re.search(r"\b(put|place|drop|leave)\b.*?\b(zone|tag|station|area)\s*(\d+)", t)
    if m:
        return {"op": "place_at", "args": {"zone_tag_id": int(m.group(3))},
                "say": f"Place it at zone {m.group(3)}."}

    m = re.search(r"\b(find|search for|look for|where is)\b.*?(\d+)", t)
    if m:
        return {"op": "search_tag", "args": {"tag_id": int(m.group(2))}, "say": f"Search for tag {m.group(2)}."}

    m = re.search(r"\b(go to|approach|drive to|move to)\b.*?(\d+)", t)
    if m:
        return {"op": "approach_tag", "args": {"tag_id": int(m.group(2))}, "say": f"Drive to tag {m.group(2)}."}

    if re.search(r"\bopen\b.*\b(claw|gripper|hand|grip)\b|\b(claw|gripper|hand) open\b|\brelease\b", t):
        return {"op": "gripper_open", "args": {}, "say": "Open the claw."}
    if re.search(r"\bclose\b.*\b(claw|gripper|hand|grip)\b|\b(claw|gripper|hand) close\b", t):
        return {"op": "gripper_close", "args": {}, "say": "Close the claw."}

    m = re.search(r"\b(turn|rotate|spin)\b.*?\b(left|right|around)\b", t)
    if m:
        if m.group(2) == "around":
            deg = 180
        else:
            n = re.search(r"\b(\d+)\b", t)
            deg = min(int(n.group(1)), 180) if n else 90
        sign = -1 if m.group(2) == "right" else 1
        return {"op": "turn_bounded", "args": {"angle_deg": float(sign * deg)},
                "say": f"Turn {m.group(2)} {deg} degrees."}

    if re.search(r"\b(back up|reverse|backward|backwards|go back|move back|drive back)\b", t):
        d = min(_distance_m(t, 10), 0.2)  # rear is unsensed: the robot itself caps blind reverse at 0.2 m
        return {"op": "drive_bounded", "args": {"distance_m": -round(d, 3)},
                "say": f"Back up {round(d * 100)} centimeters."}
    if re.search(r"\b(go|move|drive|roll)\b.*\b(forward|forwards|ahead|straight)\b|\b(forward|ahead)\b", t):
        d = min(_distance_m(t), 1.5)
        return {"op": "drive_bounded", "args": {"distance_m": round(d, 3)},
                "say": f"Drive forward {round(d * 100)} centimeters."}

    for pose in arm_poses:
        p = re.escape(pose)
        if re.search(rf"\barm\b.*\b{p}\b|\b{p}\b.*\barm\b|\b{p} (pose|position)\b", t):
            return {"op": "arm_pose", "args": {"pose": pose}, "say": f"Arm to {pose}."}

    if re.search(r"\b(look|see|observe|camera)\b", t):
        return {"op": "observe", "args": {}, "say": "Looking."}

    return {"op": None, "say": f"Sorry, I don't know how to do: \"{text.strip()}\"."}
