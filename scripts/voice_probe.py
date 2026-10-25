"""Speech-to-text probe. Nothing moves: no robot, no brain, no commands are sent.

    python scripts/voice_probe.py clips/                 # every clips/*.wav through the console's STT
    python scripts/voice_probe.py clips/ --out probe.json

Each WAV goes through console.server.voice.transcribe, the same path the console's /stt uses
(whisper.cpp server on loopback when ENGRAM_WHISPER_SERVER_URL is set, else whisper-cli; ffmpeg
only when the clip is not already 16 kHz mono 16-bit PCM). Optional reference text sits next to
the clip with the same name and .txt (clips/pick.wav -> clips/pick.txt).

Per clip it records the ACTUAL transcript (null when STT failed or raised, e.g. a whisper timeout;
the reference is never used in its place), the word error rate against the reference when there
is one, and the STT latency. One failing clip, or an unreadable reference, does not stop the run. Prints one JSON summary.
Exit code 0 when every clip was transcribed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console.server import voice  # noqa: E402


def words(text: str) -> list[str]:
    """Lowercase words for scoring. Drops whisper tags like [BLANK_AUDIO] and punctuation."""
    text = re.sub(r"\[.*?\]|\(.*?\)", " ", text.lower())
    return re.findall(r"[a-z0-9']+", text)


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    """(substitutions + deletions + insertions) / reference words. None for an empty reference."""
    ref, hyp = words(reference), words(hypothesis)
    if not ref:
        return None
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / len(ref)


def probe_clip(path: Path) -> dict:
    ref_path = path.with_suffix(".txt")
    reference, reference_error = None, None
    try:
        if ref_path.exists():
            reference = ref_path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as e:  # unreadable or not UTF-8: no WER for this clip; go on
        reference_error = f"{type(e).__name__}: {str(e)[:200]}"
    native = voice.is_native_pcm(path)
    started = time.perf_counter()
    try:
        text, reason = asyncio.run(voice.transcribe(path.read_bytes(), ".wav"))
    except Exception as e:  # noqa: BLE001 - record it against this clip and go on to the next
        text, reason = None, f"error: {type(e).__name__}: {str(e)[:200]}"
    latency = time.perf_counter() - started
    ok = text is not None
    return {"file": path.name, "transcript": text if ok else None, "ok": ok, "reason": reason,
            "reference": reference, "reference_error": reference_error,
            "wer": word_error_rate(reference, text) if ok and reference is not None else None,
            "latency_s": round(latency, 3), "native_pcm": native}


def probe(folder: Path) -> dict:
    clips = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".wav")
    results = [probe_clip(p) for p in clips]
    done = [r for r in results if r["ok"]]
    scored = [r["wer"] for r in results if r["wer"] is not None]
    latencies = [r["latency_s"] for r in done]
    return {
        "folder": str(folder),
        "stt": voice.stt_status(),
        "whisper_server": voice.whisper_server_endpoint(),
        "clips": results,
        "summary": {
            "clips": len(results), "transcribed": len(done), "failed": len(results) - len(done),
            "scored": len(scored), "mean_wer": round(statistics.fmean(scored), 4) if scored else None,
            "reference_errors": sum(1 for r in results if r["reference_error"]),
            "latency_s": {"mean": round(statistics.fmean(latencies), 3), "median": round(statistics.median(latencies), 3),
                          "max": max(latencies)} if latencies else None,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", type=Path, help="folder of .wav clips (optional .txt reference beside each)")
    ap.add_argument("--out", type=Path, help="also write the JSON summary to this file")
    args = ap.parse_args(argv)
    if not args.folder.is_dir():
        print(f"not a folder: {args.folder}", file=sys.stderr)
        return 2
    report = probe(args.folder)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    s = report["summary"]
    return 0 if s["clips"] and s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
