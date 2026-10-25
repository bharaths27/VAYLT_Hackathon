"""Speculative intent while the user is still speaking, without ever acting on a guess.

The SpeculationGate enforces these rules:

1. A partial transcript may only PREPARE: ask the intent model what it means and pre-fetch
   the memory bundle for the preset's request, the request a final would propose, so the
   proposal can use it. on_partial never returns a proposal, and the gate holds no engine.
2. Every prediction is bound to (utterance, revision, exact text, memory-context version) and
   an expiry. A result is kept only if, when it arrives, it still describes what the user has
   said: same utterance, same text with no change since its revision, same context, not
   expired. It is checked again, against a freshly read context, whenever it is used.
3. At most ONE intent request is in flight. Newer partials overwrite a single pending slot
   (newest wins); there is no queue.
4. on_final reuses a still-valid prediction whose text equals the final text, as it is: no
   second request, whether or not it was actionable (if not, the outcome is no action). The
   intent client asks the same question for a partial and a final of the same text, so that
   answer is the final's answer. A final whose text differs is asked again: more words can
   change the meaning.
5. A final that negates ("don't"), defers ("after calibration finishes") or corrects ("no,
   I mean") never becomes an immediate action. Negation means no action; a correction asks to
   clarify; a deferral becomes a "When <event>, <task>" commitment for the existing prospective
   path, or a request to rephrase. Local word checks and the model's flags are combined, and
   either one is enough to block. Every final that does not act names its cause (GateResult.cause).
   A command that was ambiguous or misheard (RULE_CAUSES: nothing caught, a correction, no answer
   from the model) is one VoiceIntegration stops a running task for and asks to clarify; its
   reason is a short statement, and the caller adds the rest. A final that memory moved past, or
   that took too long, is understood: it is asked again and stops nothing.
6. STOP words are detected locally and returned at once, for partials and finals, without
   asking the intent model. A false stop is safe; a missed one is not. A stop also voids all
   speculation: the prediction, the pending partial and any answer still in flight are
   discarded, and the utterance it was heard in (or the current one, if newer) accepts no
   further partials or finals. The next utterance starts clean. reject() does the same for an
   utterance that cannot be trusted (its audio overflowed the window), without a stop.
7. Threads: the worker thread only calls the classifier, the clock and on_prediction.
   context_version and prefetch are called only on the thread that calls on_partial, on_final,
   prepared or pump, so a database connection bound to that thread (sqlite3's default) is
   safe. When a prediction lands, on_prediction tells the owner at once, and the owner runs
   pump() on its own thread to prefetch the memory bundle, so it is ready before the final
   even with no further transcript event. (Otherwise the next partial, prepared() or the
   final fetches it.) A reject() voids that one utterance; only a stop is global.

Utterance ids and revisions are integers that only grow. Revision numbers are per utterance.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Hashable

from ..planner import FUTURE_HINTS, NEGATION
from ..prospective import is_question, normalize, parse_when
from .intent_client import IntentDecision, IntentError
from .presets import PRESETS

DEFAULT_TTL_S = 8.0          # a prediction older than this (from when it was asked) is not reused
DEFAULT_FINAL_WAIT_S = 6.0   # how long on_final waits for the request already in flight

# Exactly the pattern of STOP_WORDS in engram/engine.py and console/server/parser.py (PR #6), so a stop said
# in a partial stops here just as it would there. "hold on", "hold it" and "hang on" are phrases: "hold the
# vial" and "holding" are not stops. tests/spark/test_speculation.py pins the pattern.
STOP_WORDS = re.compile(r"\b(stop|estop|halt|freeze|abort|emergency|whoa|wait|pause|hold\s+on|hold\s+it|hang\s+on)\b",
                        re.IGNORECASE)
# Words that take back or replace part of what was just said. The intent prompt names the same
# cues; matching them here too means a confident model answer cannot talk past a correction.
CORRECTION = re.compile(
    r"\b(i mean|i meant|actually|instead|rather|scratch that|never ?mind|on second thought|no wait|wait no"
    r"|sorry)\b"
    r"|(?:^|[,;.!?]\s*)no\b",
    re.IGNORECASE,
)
# "<task> after <event>": the deferral at the end, which parse_when (deferral first) does not read.
TRAILING_WHEN = re.compile(
    r"^(?P<task>.+?)[\s,]+(?:when|whenever|once|after|as soon as)\s+(?P<event>.+?)[\s.!]*$", re.IGNORECASE
)

Classifier = Callable[[str, bool], IntentDecision]   # (text, partial) -> decision; raises on failure
log = logging.getLogger("engram.spark.voice")


@dataclass(frozen=True)
class Thresholds:
    """EXPERIMENTAL starting guesses, not measured. Tune on recorded utterances before relying on them.

    These compare against the intent model's answer probabilities, which are not transcription
    accuracy. Blocking is deliberately easier than acting: a hint of "don't", "later" or "I mean"
    stops an immediate action, while acting needs the model to be fairly sure.
    """

    preset: float = 0.6     # minimum confidence in the chosen preset
    ready: float = 0.6      # minimum P(complete enough to act on); also needed to schedule
    directed: float = 0.5   # minimum P(addressed to the robot)
    block: float = 0.35     # P(negated | future | correction) at or above this blocks immediate action


@dataclass(frozen=True)
class Key:
    """What one intent request is about. A result is only used while this still matches."""

    utterance: int
    revision: int
    text: str
    context: Hashable
    expires_at: float


@dataclass(frozen=True)
class Prediction:
    key: Key
    decision: IntentDecision
    bundle: Any = None


@dataclass(frozen=True)
class GateResult:
    # partial: stop | ignored | dispatched | queued | unchanged
    # final:   stop | ignored | propose | schedule | clarify | none
    action: str
    utterance: Any
    revision: Any
    text: str
    preset: str | None = None
    reason: str = ""
    decision: IntentDecision | None = None
    # propose: what prefetch returned for the preset's request (None if there is no prefetch or it failed), as
    # retrieved at the memory-context version on_final read after its last wait. If memory changed after a
    # partial's prefetch, the prediction was not reused and the final retrieved the bundle again.
    bundle: Any = None
    commitment: str | None = None   # "When <event>, <task>" for action == "schedule"
    reused: bool = False
    # Why a final does not act. RULE_CAUSES are Carter's rule (a running task stops, and the reply asks to
    # clarify); every other cause keeps its own reply. misheard: the words were not caught (nothing heard,
    # audio that overflowed). ambiguous: what was meant is not clear (a correction, "later" with no event to
    # wait for, no usable answer from the intent model). memory_changed / expired: understood, but memory moved
    # on, or it took too long, while it was worked out: said again, nothing stopped. negated, not_a_preset:
    # understood and declined. superseded, stopped, rejected: set aside for a newer utterance or by a stop.
    cause: str = ""


RULE_CAUSES = ("ambiguous", "misheard")


@dataclass
class GateStats:
    requests: int = 0        # intent requests started
    coalesced: int = 0       # pending partials overwritten by a newer one before being sent
    stale_dropped: int = 0   # results thrown away because utterance, revision, text or context moved on
    expired: int = 0         # predictions too old to reuse
    reused: int = 0          # finals answered by a prediction, with no new request
    errors: int = 0          # intent requests that failed
    stops: int = 0           # stop words heard
    rejected: int = 0        # utterances rejected as untrustworthy (for example, their audio overflowed)


def _clean(text: str) -> str:
    return " ".join(str(text).split())


def _valid_id(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _check_ids(utterance: int, revision: int) -> None:
    for name, v in (("utterance", utterance), ("revision", revision)):
        if not _valid_id(v):
            raise ValueError(f"{name} must be a non-negative int")


def is_stop(text: str) -> bool:
    return bool(STOP_WORDS.search(text))


def future_event(text: str) -> str | None:
    """The event in "When <event>, <task>" or "<task> after <event>", normalized; None if there is none."""
    when = parse_when(text)
    if when:
        return when[0] or None
    if is_question(text):
        return None
    m = TRAILING_WHEN.match(text)
    return (normalize(m.group("event")) or None) if m else None


def _spawn_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="spark-intent", daemon=True).start()


class SpeculationGate:
    def __init__(self, classifier: Classifier, *, prefetch: Callable[[str], Any] | None = None,
                 context_version: Callable[[], Hashable] = lambda: 0, thresholds: Thresholds | None = None,
                 ttl_s: float = DEFAULT_TTL_S, final_wait_s: float = DEFAULT_FINAL_WAIT_S,
                 clock: Callable[[], float] = time.monotonic,
                 spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
                 on_prediction: Callable[[], None] | None = None):
        if not callable(classifier):   # fail at wiring time, not as a silent error on every request
            raise TypeError("classifier must be callable as classifier(text, partial)")
        self._classifier = classifier
        self._prefetch = prefetch
        self._on_prediction = on_prediction   # called from the worker thread: keep it quick and thread safe
        self._context = context_version
        self.thresholds = thresholds or Thresholds()
        self._ttl_s = ttl_s
        self._final_wait_s = final_wait_s
        self._clock = clock
        self._spawn = spawn
        self._cv = threading.Condition()
        self._utt: int | None = None
        self._rev = -1
        self._text = ""
        self._text_since = -1          # revision at which the current text first appeared
        self._final = False
        self._stopped_utt: int | None = None   # utterances up to this id were stopped
        self._rejected: set[int] = set()       # utterances rejected one by one (reject())
        self._ctx_now: Hashable = None         # context version last read on the caller's thread
        self._in_flight: Key | None = None
        self._pending: tuple[int, int, str] | None = None
        self._pred: Prediction | None = None
        self.stats = GateStats()

    # ---- what callers may look at ------------------------------------------

    @property
    def in_flight(self) -> Key | None:
        with self._cv:
            return self._in_flight

    @property
    def pending(self) -> tuple[int, int, str] | None:
        with self._cv:
            return self._pending

    def prepared(self) -> Prediction | None:
        """The preset being prepared for what the user has said so far, if any. Never a permission.
        Owner thread only: it does pump()'s work too."""
        return self.pump()

    def wait_idle(self, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while self._in_flight is not None or self._pending is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return True

    # ---- transcript events ---------------------------------------------------

    def on_partial(self, utterance: int, revision: int, text: str) -> GateResult:
        text = _clean(text)
        if is_stop(text):  # before any bookkeeping, so nothing can get in the way of a stop
            self._latch_stop(utterance)
            return GateResult("stop", utterance, revision, text, reason="stop word heard")
        _check_ids(utterance, revision)
        ctx = self._context()
        key = None
        with self._cv:
            self._ctx_now = ctx
            why = self._observe(utterance, revision, text)
            if why:
                return GateResult("ignored", utterance, revision, text, reason=why)
            if not text or (self._pred and self._valid(self._pred.key)) or \
                    (self._in_flight and self._current(self._in_flight)):
                action = "unchanged"
            elif self._in_flight is not None:
                if self._pending is not None:
                    self.stats.coalesced += 1
                self._pending = (utterance, revision, text)
                action = "queued"
            else:
                key = self._dispatch()
                action = "dispatched"
        if key is not None:
            self._spawn(lambda: self._run(key))
        self._attach_bundle()
        return GateResult(action, utterance, revision, text)

    def on_final(self, utterance: int, revision: int, text: str) -> GateResult:
        text = _clean(text)
        if is_stop(text):  # before any bookkeeping, so nothing can get in the way of a stop
            self._latch_stop(utterance)
            return GateResult("stop", utterance, revision, text, reason="stop word heard")
        _check_ids(utterance, revision)
        deadline = time.monotonic() + self._final_wait_s
        with self._cv:
            why = self._observe(utterance, revision, text)
            if why:
                return GateResult("ignored", utterance, revision, text, reason=why)
            self._final, self._pending = True, None
            # Single flight: wait for the request already out. If it is about this same text, reuse its answer.
            while self._in_flight is not None and self._utt == utterance and not self._is_stopped(utterance):
                left = deadline - time.monotonic()
                if left <= 0:
                    return GateResult("clarify", utterance, revision, text,
                                      reason="The intent model did not answer in time.", cause="ambiguous")
                self._cv.wait(left)
        ctx = self._context()   # on the caller's thread, with the lock released
        with self._cv:
            self._ctx_now = ctx
            if self._utt != utterance or self._is_stopped(utterance):
                return GateResult("ignored", utterance, revision, text, reason="stopped or superseded")
            if not text:
                return GateResult("clarify", utterance, revision, text, reason="I didn't catch anything.",
                                  cause="misheard")
            # A still-valid prediction for exactly this text is the final's answer (the intent client asks the
            # same question for a partial and a final), so it is reused as it is, actionable or not.
            pred = self._pred if self._pred and self._current(self._pred.key) else None
            if pred and self._clock() >= pred.key.expires_at:
                self.stats.expired += 1
                pred = None
            if pred:
                self.stats.reused += 1
                key, decision, bundle, reused = pred.key, pred.decision, pred.bundle, True
            else:
                key, reused = self._dispatch(), False
        if not reused:
            decision, bundle = self._ask(key, partial=False), None
            with self._cv:
                self._in_flight = None
                nxt = self._take_pending()   # a newer utterance may have queued a partial meanwhile
                self._cv.notify_all()
            if nxt is not None:
                self._spawn(lambda: self._run(nxt))
            if decision is None:
                return GateResult("clarify", utterance, revision, text, reason="I could not work out what you meant.",
                                  cause="ambiguous")
        # After every wait (the model above, the prefetch below), go on only if nothing moved on meanwhile:
        # a final that a stop, a newer utterance or new memory has already voided prefetches nothing.
        void = self._void_final(key, utterance, revision, text)
        if void is None and bundle is None and self._actionable(text, decision):
            bundle = self._fetch_bundle(decision)
            void = self._void_final(key, utterance, revision, text)
        return void if void is not None else self._decide(key, decision, bundle, reused=reused)

    # ---- internals (lock held unless noted) ------------------------------------

    def _void_final(self, key: Key, utterance: int, revision: int, text: str) -> GateResult | None:
        """Why a final must not go on after a wait, or None if it may. Reads the context first: caller's
        thread only, lock NOT held."""
        ctx = self._context()
        with self._cv:
            self._ctx_now = ctx
            if self._is_stopped(utterance):
                return GateResult("ignored", utterance, revision, text, reason=self._void_why(utterance),
                                  cause=self._void_why(utterance))
            if not self._current(key):
                self.stats.stale_dropped += 1
                return GateResult("none", utterance, revision, text,
                                  reason="Things changed while I was listening. Please say it again.",
                                  cause="memory_changed")
            if self._clock() >= key.expires_at:   # with _current above, this is _valid(): TTL included
                self.stats.expired += 1
                return GateResult("clarify", utterance, revision, text,
                                  reason="That took too long to work out. Please say it again.", cause="expired")
            return None

    def _read_context(self) -> None:
        """Caller's thread only, lock NOT held."""
        ctx = self._context()
        with self._cv:
            self._ctx_now = ctx

    def pump(self) -> Prediction | None:
        """Owner-thread work a landed prediction is waiting for: its memory bundle. Run it on the thread that
        owns the database whenever on_prediction fires (any time is safe; it does nothing when idle).
        Returns the prediction being prepared, as prepared() does."""
        self._read_context()
        self._attach_bundle()
        with self._cv:
            p = self._pred
            if p and self._valid(p.key) and self._actionable(p.key.text, p.decision):
                return p
            return None

    def reject(self, utterance: int, reason: str = "I could not hear that properly.") -> GateResult:
        """This utterance, and only this one, cannot be trusted (for example, its audio overflowed the
        window): void its prediction, pending partial and in-flight answer, and refuse its later partials
        and final. Any other utterance, a newer one in particular, is untouched; only a stop is global."""
        with self._cv:
            self.stats.rejected += 1
            if _valid_id(utterance):
                self._rejected = {u for u in self._rejected if self._utt is None or u >= self._utt}
                self._rejected.add(utterance)
                if utterance == self._utt:
                    self._pred = None              # an in-flight answer fails _current() when it lands
                    if self._pending is not None and self._pending[0] == utterance:
                        self._pending = None
                self._cv.notify_all()
        return GateResult("clarify", utterance, -1, "", reason=reason)

    def _latch_stop(self, utterance: Any) -> None:
        """A stop: void all speculation, and refuse further transcripts of the utterance it was heard in
        (or the current one, if newer). Never raises."""
        with self._cv:
            self.stats.stops += 1
            ids = [u for u in (utterance if _valid_id(utterance) else None, self._utt, self._stopped_utt)
                   if u is not None]
            if ids:
                self._stopped_utt = max(ids)
            self._pred, self._pending = None, None   # an in-flight answer fails _current() when it lands
            self._cv.notify_all()

    def _is_stopped(self, utterance: int) -> bool:
        """Stopped (every utterance up to the latest stop) or rejected (that utterance alone)."""
        return (self._stopped_utt is not None and utterance <= self._stopped_utt) or utterance in self._rejected

    def _void_why(self, utterance: int) -> str:
        return "rejected" if utterance in self._rejected else "stopped"

    def _observe(self, utterance: int, revision: int, text: str) -> str | None:
        """Record a transcript revision. Returns why it was ignored, or None."""
        if self._utt is not None and utterance < self._utt:
            return "older utterance"
        if self._is_stopped(utterance):
            return self._void_why(utterance)
        if utterance != self._utt:
            self._utt, self._rev, self._text, self._text_since, self._final = utterance, -1, "", -1, False
            self._pending, self._pred = None, None
        if self._final:
            return "utterance already final"
        if revision < self._rev or (revision == self._rev and text != self._text):
            return "older revision"
        if text != self._text:
            self._text, self._text_since = text, revision
        self._rev = revision
        return None

    def _current(self, key: Key) -> bool:
        """Does this key still describe what the user has said, against the latest memory context read?"""
        return (key.utterance == self._utt and not self._is_stopped(key.utterance)
                and key.text == self._text
                and self._text_since <= key.revision <= self._rev
                and key.context == self._ctx_now)

    def _valid(self, key: Key) -> bool:
        return self._current(key) and self._clock() < key.expires_at

    def _dispatch(self) -> Key:
        key = Key(self._utt, self._rev, self._text, self._ctx_now, self._clock() + self._ttl_s)
        self._in_flight = key
        self.stats.requests += 1
        return key

    def _flags(self, text: str, d: IntentDecision) -> tuple[bool, bool, bool]:
        """(negated, corrected, future): the local word checks OR the model's flags."""
        low = text.lower().replace("\u2019", "'")
        th = self.thresholds
        negated = bool(NEGATION.search(low)) or d.negated >= th.block
        corrected = bool(CORRECTION.search(low)) or d.correction >= th.block
        future = bool(FUTURE_HINTS.search(low)) or parse_when(text) is not None or d.future >= th.block
        return negated, corrected, future

    def _preset_ok(self, d: IntentDecision) -> bool:
        th = self.thresholds
        return d.preset in PRESETS and d.confidence >= th.preset and d.directed >= th.directed

    def _actionable(self, text: str, d: IntentDecision) -> bool:
        """Worth preparing: a confident, complete, directed preset with nothing blocking it."""
        return self._preset_ok(d) and d.ready >= self.thresholds.ready and not any(self._flags(text, d))

    def _ask(self, key: Key, partial: bool) -> IntentDecision | None:
        """Call the intent model (lock NOT held). Touches nothing but the classifier. None on any failure."""
        try:
            decision = self._classifier(key.text, partial)
            if not isinstance(decision, IntentDecision):
                raise IntentError("classifier returned no decision")
            return decision
        except Exception as e:
            log.warning("intent request failed: %s", type(e).__name__)
            with self._cv:
                self.stats.errors += 1
            return None

    def _fetch_bundle(self, d: IntentDecision) -> Any:
        """The memory bundle for the request d's preset would propose. Caller's thread only, lock NOT held."""
        if self._prefetch is None:
            return None
        try:
            return self._prefetch(PRESETS[d.preset].request)
        except Exception as e:
            log.warning("memory prefetch failed: %s", type(e).__name__)
            return None

    def _attach_bundle(self) -> None:
        """Pre-fetch the memory bundle for a ready prediction. Caller's thread only, lock NOT held."""
        if self._prefetch is None:
            return
        with self._cv:
            p = self._pred
            if p is None or p.bundle is not None or not self._valid(p.key) or \
                    not self._actionable(p.key.text, p.decision):
                return
        bundle = self._fetch_bundle(p.decision)
        if bundle is None:
            return
        ctx = self._context()
        with self._cv:
            self._ctx_now = ctx
            if self._pred is p and self._valid(p.key):   # nothing moved on while fetching
                self._pred = Prediction(p.key, p.decision, bundle)

    def _run(self, key: Key) -> None:
        """Worker for one partial request (lock NOT held on entry). Calls only the classifier and the clock."""
        decision = self._ask(key, partial=True)
        nxt: Key | None = None
        landed = False
        with self._cv:
            self._in_flight = None
            try:
                if decision is not None:
                    if self._valid(key):
                        self._pred = Prediction(key, decision)
                        landed = True
                    else:
                        self.stats.stale_dropped += 1
            finally:
                nxt = self._take_pending()
                self._cv.notify_all()
        if landed and self._on_prediction is not None:
            try:      # tell the owner, so it can run pump() on its own thread: the bundle is fetched there
                self._on_prediction()
            except Exception as e:
                log.warning("on_prediction failed: %s", type(e).__name__)
        if nxt is not None:
            self._spawn(lambda: self._run(nxt))

    def _take_pending(self) -> Key | None:
        p, self._pending = self._pending, None
        if p is None or self._final or p[0] != self._utt or p[2] != self._text or self._is_stopped(p[0]):
            return None
        if self._pred and self._valid(self._pred.key):
            return None
        return self._dispatch()

    def _decide(self, key: Key, d: IntentDecision, bundle: Any, reused: bool) -> GateResult:
        """Turn the decision for a FINAL text into what should happen. Pure: touches no robot or engine."""
        base = {"utterance": key.utterance, "revision": key.revision, "text": key.text, "decision": d,
                "reused": reused}
        negated, corrected, future = self._flags(key.text, d)
        if negated:
            return GateResult("none", reason="You said not to, so nothing will move.", cause="negated", **base)
        if corrected:
            return GateResult("clarify", reason="You corrected yourself, so nothing will move.", cause="ambiguous",
                              **base)
        ready = d.ready >= self.thresholds.ready
        if future:
            event = future_event(key.text)
            if self._preset_ok(d) and ready and event:
                return GateResult("schedule", preset=d.preset, commitment=f"When {event}, {PRESETS[d.preset].request}",
                                  **base)
            return GateResult("clarify", reason="That sounds like something for later. "
                                                "Say it as 'When <event>, <task>'.", cause="ambiguous", **base)
        if not self._preset_ok(d) or not ready:
            return GateResult("none", reason="That is not one of the voice presets.", cause="not_a_preset", **base)
        return GateResult("propose", preset=d.preset, bundle=bundle, **base)
