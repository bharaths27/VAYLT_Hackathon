"""Typed client for the hosted intent model (TypeSafe System One).

Given a transcript, partial or final, the intent model answers a fixed set of
questions: which preset (if any) the speaker wants, whether the words so far are
complete enough to act on, whether they are addressed to the robot, and whether
they negate, defer ("after calibration finishes") or correct ("no, I mean") the
request. The reply is turned into an IntentDecision; anything unexpected raises
IntentError, and callers treat an error as "no prediction" (fail closed).

About the numbers: every probability and the preset confidence come from the
intent model's own answer distribution over the question's options. They say how
sure the model is about the MEANING of the text it was given. They are NOT
acoustic or transcription accuracy: a misheard word is scored as if it had been
said. The thresholds that turn them into actions (voice.Thresholds) are
experimental guesses until they are measured on recorded utterances.

Configuration comes from the environment and has no defaults in code:
    TYPESAFE_API_KEY       the API key (never logged, never in repr or errors)
    TYPESAFE_INTENT_MODEL  the pinned model id to ask
The endpoint is fixed to the official URL, redirects are never followed, no proxy is used,
and timeout_s is an absolute deadline for the whole request and reply.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from . import _http
from .presets import PRESETS

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_TIMEOUT_S = 4.0
MAX_TIMEOUT_S = 10.0
MAX_TEXT = 1000          # longer transcripts are refused (IntentError), never cut short
NO_PRESET = "none"

log = logging.getLogger("engram.spark.intent")

# (url, body, headers, timeout_s) -> (status, reply body). Swapped out in tests; the URL is always ENDPOINT.
Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class IntentError(Exception):
    """The intent model gave no usable answer. Callers must treat this as 'no prediction'."""


class IntentConfigError(IntentError):
    """Required configuration is missing."""


@dataclass(frozen=True)
class IntentDecision:
    """What the intent model made of one transcript.

    preset      one of PRESETS, or None when no preset fits
    confidence  the model's confidence in that preset choice (0..1)
    ready, directed, negated, future, correction
                the model's probability (0..1) that the answer is yes to:
                complete enough to act on / addressed to the robot / says NOT to do it /
                ties it to a later event or time / corrects or takes back what was said
    None of these is transcription accuracy; see the module docstring.
    """

    preset: str | None
    confidence: float
    ready: float
    directed: float
    negated: float
    future: float
    correction: float
    model: str = ""
    latency_ms: float = 0.0


def _yes_no(instructions: str, yes: str, no: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


QUESTIONS: dict[str, Any] = {
    "preset": {
        "type": "choice",
        "instructions": (
            "Someone is speaking to a small lab robot that has a gripper and a camera. Which ONE of these "
            "robot actions does the speaker want? The transcript may be cut off mid-sentence. Answer none when "
            "no listed action fits, when the speaker is not asking the robot to do something, or when it is "
            "too early in the sentence to tell."
        ),
        "criteria": {**{p.name: p.description for p in PRESETS.values()},
                     NO_PRESET: "None of the listed actions, or not a request to the robot, or too early to tell."},
    },
    "ready": _yes_no(
        "The transcript may still be growing while the person speaks. Is what has been said so far complete "
        "enough to know exactly which action they want, so that the rest of the sentence is unlikely to "
        "change it?",
        "The request is complete; nothing more is needed to know the action.",
        "The sentence is unfinished or unclear; the action could still change or be missing.",
    ),
    "directed": _yes_no(
        "Is the speaker giving an instruction to the robot, as opposed to talking to another person, "
        "describing, quoting, or asking a question for information?",
        "The words are an instruction or request addressed to the robot.",
        "The words are addressed to someone else, describe or quote something, or only ask for information.",
    ),
    "negated": _yes_no(
        "Does the speaker say NOT to do the action (for example don't, do not, never, no need, leave it, "
        "cancel), anywhere in the transcript, including at the end?",
        "The speaker says not to do it, or cancels it.",
        "Nothing in the transcript says not to do it.",
    ),
    "future": _yes_no(
        "Does the speaker tie the action to a later time or event instead of now (for example after, when, "
        "once, as soon as, later, in five minutes), anywhere in the transcript, including at the end?",
        "The action is meant for later or after some event.",
        "The action is meant now; no later time or event is attached.",
    ),
    "correction": _yes_no(
        "Does the speaker correct or take back something they said earlier in the same transcript (for "
        "example no I mean, actually, wait, scratch that, sorry I meant)?",
        "The speaker corrects or takes back an earlier part.",
        "There is no correction.",
    ),
}


def _prob(answer: Any) -> float | None:
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        return None
    p = answer.get("noul")
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.0 <= p <= 1.0:
        return None
    return float(p)


def parse_reply(raw: Any, model: str) -> IntentDecision:
    """Turn a reply body into a decision. Raises IntentError on anything missing or out of range."""
    answers = raw.get("answers") if isinstance(raw, dict) else None
    if not isinstance(answers, dict):
        raise IntentError("malformed reply: no answers")
    probs: dict[str, float] = {}
    for name in ("ready", "directed", "negated", "future", "correction"):
        p = _prob(answers.get(name))
        if p is None:
            raise IntentError(f"malformed reply: {name}")
        probs[name] = p
    choice = answers.get("preset")
    allowed = set(PRESETS) | {NO_PRESET}
    picked = choice.get("choice") if isinstance(choice, dict) else None
    # Type first: a list or object is unhashable, and the membership test would raise TypeError.
    if not isinstance(choice, dict) or choice.get("type") != "choice" or not isinstance(picked, str) \
            or picked not in allowed:
        raise IntentError("malformed reply: preset")
    conf = choice.get("confidence")
    # A missing or odd confidence counts as zero, so the preset can never pass a threshold.
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        conf = 0.0
    preset = None if picked == NO_PRESET else picked
    echoed = raw.get("model")
    return IntentDecision(preset=preset, confidence=float(conf), model=echoed[:64] if isinstance(echoed, str) else model,
                          **probs)


def _default_transport(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    return _http.post(url, body, headers, timeout_s)


class IntentClient:
    """Ask the hosted intent model about one transcript. Thread safe; holds no per-call state."""

    def __init__(self, api_key: str, model: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                 transport: Transport | None = None):
        if not api_key or not api_key.strip():
            raise IntentConfigError("TYPESAFE_API_KEY is not set")
        if not model or not model.strip():
            raise IntentConfigError("TYPESAFE_INTENT_MODEL is not set; pin the intent model id explicitly")
        if not 0.0 < timeout_s <= MAX_TIMEOUT_S:
            raise ValueError(f"timeout_s must be in (0, {MAX_TIMEOUT_S}]")
        self.__key = api_key.strip()
        self.model = model.strip()
        self.timeout_s = timeout_s
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw: Any) -> "IntentClient":
        env = os.environ if env is None else env
        return cls(env.get("TYPESAFE_API_KEY", ""), env.get("TYPESAFE_INTENT_MODEL", ""), **kw)

    def __repr__(self) -> str:
        return f"IntentClient(model={self.model!r}, timeout_s={self.timeout_s})"

    def request_body(self, text: str, partial: bool) -> dict[str, Any]:
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            # Never classify a prefix: the words after the cut could be "no, don't" or "after ...".
            raise IntentError(f"transcript longer than {MAX_TEXT} characters; not classified")
        return {
            "model": self.model,
            "state": {
                "transcript": text,
                "transcript_is_partial": bool(partial),
                "listener": "a small lab robot with a gripper and a camera",
            },
            "questions": QUESTIONS,
        }

    def __call__(self, text: str, partial: bool) -> IntentDecision:
        """The client is itself a classifier: pass it straight to SpeculationGate or VoiceIntegration."""
        return self.classify(text, partial)

    def classify(self, text: str, partial: bool) -> IntentDecision:
        body = json.dumps(self.request_body(text, partial)).encode()   # raises before anything is sent
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.__key}"}
        t0 = time.perf_counter()
        failed = None
        try:
            status, data = self._transport(ENDPOINT, body, headers, self.timeout_s)
        except Exception as e:  # never str(e): transport errors can echo request details
            failed = type(e).__name__
        if failed is not None:  # raised outside the except block so the original is not chained as __context__
            raise IntentError(f"intent request failed: {failed}")
        ms = (time.perf_counter() - t0) * 1000
        if 300 <= status < 400:
            raise IntentError(f"redirect refused (http {status})")
        if status != 200:
            raise IntentError(f"intent model http {status}")
        try:
            raw = json.loads(data)
        except ValueError:
            raise IntentError("malformed reply: not JSON") from None
        decision = parse_reply(raw, self.model)
        log.info("intent model answered in %.0f ms (partial=%s)", ms, partial)
        return replace(decision, latency_ms=round(ms, 1))
