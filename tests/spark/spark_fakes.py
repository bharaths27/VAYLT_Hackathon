"""Fakes and fixtures for the spark tests: a scripted intent model, a hand-cranked worker, a settable clock.

Test modules import the fixtures by name. (A conftest.py here would shadow tests/conftest.py, which other
tests import as the module "conftest".)
"""

from __future__ import annotations

import threading
from array import array

import pytest

from engram.spark.audio_windows import SAMPLE_RATE, SAMPLE_WIDTH
from engram.spark.intent_client import IntentDecision

SAMPLES = (1).to_bytes(2, "little") * 800        # 0.05 s of valid 16-bit mono audio


def _released_buffer():
    view = memoryview(bytes(4))
    view.release()
    return view


def _sealed_then_appended(window):
    window.seal()
    window.append(SAMPLES)


# Every way PcmWindow.append() can refuse a chunk, each applied to a window that already holds chunk 0.
REJECTIONS = {
    "not bytes": lambda w: w.append("Open the claw"),
    "float samples": lambda w: w.append(memoryview(array("f", [0.0] * 800))),
    "odd length": lambda w: w.append(bytes(1)),
    "released buffer": lambda w: w.append(_released_buffer()),
    "seq is a bool": lambda w: w.append(SAMPLES, seq=True),
    "seq is a float": lambda w: w.append(SAMPLES, seq=1.0),
    "seq skips ahead": lambda w: w.append(SAMPLES, seq=5),
    "seq repeats": lambda w: w.append(SAMPLES, seq=0),
    "overflow": lambda w: w.append(bytes(SAMPLE_WIDTH * SAMPLE_RATE * 30)),
    "after the seal": _sealed_then_appended,
}


def decision(preset=None, confidence=0.9, ready=0.9, directed=0.9, negated=0.0, future=0.0, correction=0.0):
    return IntentDecision(preset, confidence, ready, directed, negated, future, correction, model="fake")


# What a well behaved intent model might say about the phrases in the voice upgrade review.
SCRIPT = {
    "Open...": decision("open_gripper", confidence=0.45, ready=0.1),
    "Open the": decision("open_gripper", confidence=0.5, ready=0.2),
    "Open the claw": decision("open_gripper", confidence=0.93, ready=0.9),
    "Close the claw": decision("close_gripper", confidence=0.92, ready=0.9),
    "Open the claw after calibration finishes": decision("open_gripper", confidence=0.9, ready=0.8, future=0.95),
    "Open the claw, no, don't": decision("open_gripper", confidence=0.6, ready=0.7, negated=0.95, correction=0.8),
    "Where are the goggles?": decision(None, confidence=0.95, ready=0.9, directed=0.2),
}


class FakeIntent:
    """Scripted intent model. Counts calls and concurrency; can hold every call until released."""

    def __init__(self, script=None, fail=False):
        self.script = dict(SCRIPT if script is None else script)
        self.fail = fail
        self.calls: list[tuple[str, bool]] = []
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Semaphore(0)
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def hold(self):
        self.release.clear()

    def __call__(self, text, partial):
        with self._lock:
            self.calls.append((text, partial))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.release()
        try:
            assert self.release.wait(10), "test never released the fake intent model"
            if self.fail:
                raise TimeoutError("fake timeout")
            return self.script.get(text, decision(None, confidence=0.9))
        finally:
            with self._lock:
                self.active -= 1


class ManualSpawn:
    """Runs gate jobs only when the test says so, so in-flight and pending states can be inspected."""

    def __init__(self):
        self.jobs = []

    def __call__(self, job):
        self.jobs.append(job)

    def run_next(self):
        self.jobs.pop(0)()

    def run_all(self):
        while self.jobs:
            self.run_next()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def fake():
    return FakeIntent()


@pytest.fixture
def spawn():
    return ManualSpawn()


@pytest.fixture
def clock():
    return Clock()


def reply_body(preset="open_gripper", confidence=0.95, **probs):
    """A well-formed intent model reply, as bytes."""
    import json

    answers = {name: {"type": "noul", "noul": probs.get(name, 0.9 if name in ("ready", "directed") else 0.02)}
               for name in ("ready", "directed", "negated", "future", "correction")}
    answers["preset"] = {"type": "choice", "choice": preset, "confidence": confidence}
    return json.dumps({"answers": answers}).encode()


class RecordingTransport:
    """Stands in for the network under a real IntentClient; records every request it is given."""

    def __init__(self, body=None):
        self.body = body or reply_body()
        self.sent = []

    def __call__(self, url, body, headers, timeout_s):
        self.sent.append(body)
        return 200, self.body
