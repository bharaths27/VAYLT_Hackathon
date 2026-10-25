"""Run the EC15 verified-practice cases on a disposable database and the MOCK robot.

    python scripts/engram_practice.py                       # print the table
    python scripts/engram_practice.py --out results.jsonl   # also save one record per case

Nothing here talks to a server or a robot. Every result is SIMULATED.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engram.practice import load_cases, run_case, verify  # noqa: E402


def head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "tests/fixtures/engram_practice.jsonl"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    code = head()
    records = []
    for case in load_cases(args.cases):
        if case.get("kind") == "verifier_check":
            verdict, reasons = verify(case["expected"], case["actual"])
            ok = verdict == case["expected_verdict"]
            rec = {"case_id": case["case_id"], "family": "verifier", "verdict": "PASS" if ok else "FAIL",
                   "evidence": [f"verifier said {verdict}, should say {case['expected_verdict']}"] + reasons}
        else:
            rec = run_case(case)
        rec["code_version"] = code
        records.append(rec)
        print(f"{rec['case_id']:6} {rec['family']:20} {rec['verdict']:8} {'; '.join(rec['evidence'])[:110]}")

    counts = {v: sum(r["verdict"] == v for r in records) for v in ("PASS", "FAIL", "ABSTAIN", "BLOCKED")}
    print(f"\nhead {code}, MOCK robot, SIMULATED: " + ", ".join(f"{k} {n}" for k, n in counts.items()))
    if args.out:
        Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in records))
        print(f"saved {args.out}")


if __name__ == "__main__":
    main()
