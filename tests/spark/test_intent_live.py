"""Opt-in live smoke test against the hosted intent model.

Skipped unless TYPESAFE_API_KEY and TYPESAFE_INTENT_MODEL are both set. Run with -s to see the
per-phrase table. It asserts only what the gate guarantees whatever the model says (stop is
local, "don't" and "after ..." never become an immediate action); the model's accuracy and
latency are printed for a person to judge, not asserted. The numbers are from the machine
that ran the test, not from the robot's computer.
"""

import os

import pytest

from engram.spark.intent_client import IntentClient
from engram.spark.voice import SpeculationGate

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TYPESAFE_API_KEY") and os.environ.get("TYPESAFE_INTENT_MODEL")),
    reason="live intent model test: set TYPESAFE_API_KEY and TYPESAFE_INTENT_MODEL to run",
)


class Recording:
    def __init__(self, client):
        self.client = client
        self.decisions = []

    def __call__(self, text, partial):
        d = self.client(text, partial)       # the client is itself the gate's classifier
        self.decisions.append((text, partial, d))
        return d


def row(label, d, action, calls):
    if d is None:
        return f"{label:<44} {'-':>13} {'':>5} {'':>5} {'':>5} {'':>5} {'':>5} {'':>5}  {action:<9} {'-':>7}  calls={calls}"
    return (f"{label:<44} {str(d.preset):>13} {d.confidence:5.2f} {d.ready:5.2f} {d.directed:5.2f} {d.negated:5.2f} "
            f"{d.future:5.2f} {d.correction:5.2f}  {action:<9} {d.latency_ms:7.0f}  calls={calls}")


def test_live_phrases():
    client = IntentClient.from_env(timeout_s=10.0)
    lines = [f"{'phrase':<44} {'preset':>13} {'conf':>5} {'ready':>5} {'dir':>5} {'neg':>5} {'fut':>5} {'corr':>5}  "
             f"{'action':<9} {'ms':>7}"]

    # A partial that has not said enough yet: it may only prepare, never act.
    rec = Recording(client)
    gate = SpeculationGate(rec, final_wait_s=12.0)
    gate.on_partial(1, 0, "Open...")
    assert gate.wait_idle(12)
    prepared = gate.prepared()
    lines.append(row("Open... (partial)", rec.decisions[-1][2] if rec.decisions else None,
                     f"prep:{prepared.decision.preset if prepared else None}", len(rec.decisions)))

    # Partial then the same final: the final should reuse the partial's answer.
    rec = Recording(client)
    gate = SpeculationGate(rec, final_wait_s=12.0)
    gate.on_partial(1, 0, "Open the claw")
    assert gate.wait_idle(12)
    prepared = gate.prepared()
    lines.append(row("Open the claw (partial)", rec.decisions[-1][2] if rec.decisions else None,
                     f"prep:{prepared.decision.preset if prepared else None}", len(rec.decisions)))
    r = gate.on_final(1, 1, "Open the claw")
    lines.append(row("Open the claw (final, same text)", r.decision, r.action + (" reused" if r.reused else ""),
                     len(rec.decisions)))

    for text, must_not in [("Open the claw after calibration finishes", {"propose"}),
                           ("Open the claw, no, don't", {"propose", "schedule"}),
                           ("Where are the goggles?", set())]:
        rec = Recording(client)
        r = SpeculationGate(rec, final_wait_s=12.0).on_final(1, 0, text)
        lines.append(row(f"{text} (final)", r.decision, r.action, len(rec.decisions)))
        assert r.action not in must_not, (text, r.action)

    rec = Recording(client)
    r = SpeculationGate(rec).on_final(1, 0, "Stop")
    lines.append(row("Stop (final)", None, r.action, len(rec.decisions)))
    assert r.action == "stop" and rec.decisions == []

    print("\nLIVE intent model, measured on the machine running this test (not the robot's computer):")
    print("\n".join(lines))
