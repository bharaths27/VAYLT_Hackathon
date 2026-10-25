"""Voice presets into the existing Engine, and nothing more.

- A partial transcript only prepares, through the SpeculationGate. The only thing a partial
  can do to the engine is STOP, which calls Engine.stop() exactly like the stop button.
- A final transcript that selects a preset becomes an ordinary Engine proposal: Engine.propose
  with the preset's canonical request text, so the refusal rules, the checker and the
  digest-bound approval all apply. This module never approves and never runs a plan.
- The memory bundle a partial prefetched for that request is the proposal's bundle: the final
  does not retrieve it again while the memory-context version is unchanged (the gate re-reads it
  after every wait), and retrieves it once if memory changed. Only object freshness in it, which
  is computed from the clock, can be older, by less than the gate's TTL; the checker reads memory
  itself, at the proposal and again at approval.
- A deferred preset ("open the claw after calibration finishes") becomes a "When <event>,
  <task>" commitment through Engine.command, the existing prospective path. When the trigger
  fires, the engine proposes it and a person still approves it.
- A final the gate could not act on because it was ambiguous or misheard (a "clarify" result:
  nothing caught, a correction, audio that overflowed, no answer from the intent model) follows
  Carter's rule: if a plan is running (an episode whose outcome is 'running'), Engine.stop()
  stops it with the latching stop a stop word uses, and the reply is "I stopped. <reason> Please
  clarify."; with nothing running nothing is stopped and the reply is "<reason> Please
  clarify.". "You said not to" and "not one of the voice presets" are understood: unchanged.

Call on_partial and on_final from the thread that owns the engine's database connection. The
gate's worker thread only talks to the intent model; every database read (the memory-context
version and the memory bundle) happens on the calling thread.
"""

from __future__ import annotations

from typing import Any, Hashable

from ..engine import Engine, clarify_reply
from ..memory import MemoryBundle
from ..planner import refusal
from ..skills import APPROVED
from .audio_windows import AudioOverflow, LiveTranscriber
from .intent_client import IntentDecision
from .presets import PRESETS, register_presets, voice_presets_enabled
from .voice import Classifier, GateResult, Prediction, SpeculationGate

ACTING = ("propose", "schedule")   # gate actions that lead to an engine proposal, now or when a trigger fires


def _presets_off(text: str, partial: bool) -> IntentDecision:
    """The classifier while voice presets are turned off: nothing is a preset, and no model is asked."""
    return IntentDecision(None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, model="presets-off")


def _memory(prepared: Prediction | None) -> dict[str, Any] | None:
    """The memory bundle prefetched for what is being prepared, if it is there yet."""
    return prepared.bundle.to_dict() if prepared is not None and prepared.bundle is not None else None


def memory_version(engine: Engine) -> Hashable:
    """Changes whenever anything a memory bundle is built from changes.

    Sources, assertions, observations, bindings and lessons only ever gain rows, so their counts
    move on every change; approved skills are listed by id because approval is an update.
    """
    row = engine.conn.execute(
        "SELECT (SELECT COUNT(*) FROM source_chunk), (SELECT COUNT(*) FROM assertion), "
        "(SELECT COUNT(*) FROM observation), (SELECT COUNT(*) FROM object_binding), "
        "(SELECT COUNT(*) FROM negative_memory), "
        "(SELECT group_concat(id) FROM (SELECT id FROM skill_version WHERE status = ? ORDER BY id))",
        (APPROVED,),
    ).fetchone()
    return tuple(row)


class VoiceIntegration:
    def __init__(self, engine: Engine, classifier: Classifier, **gate_options: Any):
        """With ENGRAM_VOICE_PRESETS off (read as engram/api.py reads it), no preset is registered, the
        intent model is never asked, and nothing but a stop is acted on.

        Pass on_prediction=... (a quick, thread-safe callable, for example one that queues a call to
        pump() on the owner's event loop) to hear, from the gate's worker thread, the moment a
        prediction lands; then call pump() on the thread that owns the database to prefetch its
        memory bundle before the speaker has finished."""
        self.engine = engine
        self.presets_enabled = voice_presets_enabled()
        if self.presets_enabled:
            register_presets(engine)
        self.gate = SpeculationGate(classifier if self.presets_enabled else _presets_off, prefetch=self._prefetch,
                                    context_version=lambda: memory_version(engine), **gate_options)

    @staticmethod
    def _precheck(r: GateResult) -> str | None:
        """The one check every branch that acts on a preset, now or later, goes through first.

        The engine's own refusal rules applied to what was actually SAID: the preset's canonical
        text ("Open the gripper") would hide a refused or negated part of the transcript. The
        only difference between branches is that a scheduled task may contain its deferral.
        """
        return refusal(r.text, future_ok=(r.action == "schedule"))

    def _prefetch(self, request: str) -> MemoryBundle:
        """Exactly the retrieval Engine.propose(request) would make, so the proposal can use it."""
        return self.engine.memory.retrieve_memory_bundle(request, self.engine.skills.approved_names())

    def pump(self) -> dict[str, Any]:
        """On the owner thread, after on_prediction fired: prefetch the memory bundle of the prediction that
        just landed. Returns what is being prepared, as on_partial does."""
        prepared = self.gate.pump()
        return {"kind": "partial", "status": "pumped", "preparing": prepared.decision.preset if prepared else None,
                "memory": _memory(prepared)}

    def on_partial(self, utterance: int, revision: int, text: str) -> dict[str, Any]:
        r = self.gate.on_partial(utterance, revision, text)
        if r.action == "stop":
            return {"kind": "stop", "heard": r.text, "state": self.engine.stop()}
        prepared = self.gate.prepared()
        return {"kind": "partial", "status": r.action, "heard": r.text,
                "preparing": prepared.decision.preset if prepared else None, "memory": _memory(prepared)}

    def on_final(self, utterance: int, revision: int, text: str) -> dict[str, Any]:
        r = self.gate.on_final(utterance, revision, text)
        base = {"heard": r.text, "utterance": r.utterance, "revision": r.revision}
        if r.action == "stop":
            return {"kind": "stop", **base, "state": self.engine.stop()}
        if not self.presets_enabled:
            return {"kind": "none", **base, "reason": "Voice presets are turned off (ENGRAM_VOICE_PRESETS)."}
        if r.action in ACTING:
            why = self._precheck(r)
            if why:
                return {"kind": "none", **base, "reason": why}
        if r.action == "propose":
            preset = PRESETS[r.preset]
            # r.bundle was retrieved for preset.request at the memory version the gate has just re-read, so the
            # proposal uses it instead of retrieving it again.
            out = self.engine.command(preset.request, who="voice", bundle=r.bundle)
            return {**out, **base, "preset": preset.name, "reused_prediction": r.reused}
        if r.action == "schedule":
            out = self.engine.command(r.commitment, who="voice")
            if out.get("kind") != "scheduled":
                return {"kind": "none", **base, "reason": "That could not be saved as a future task."}
            return {**out, **base, "preset": r.preset}
        if r.action == "clarify":
            return self._clarify(base, r.reason)
        return {"kind": r.action, **base, "reason": r.reason}

    def _clarify(self, base: dict[str, Any], reason: str) -> dict[str, Any]:
        """Carter's rule: stop the plan that is running, if one is, then ask the person to clarify."""
        stopped = bool(self.engine.running())
        out = {"kind": "clarify", **base, "reason": reason, "reply": clarify_reply(reason, stopped), "stopped": stopped}
        if stopped:
            out["state"] = self.engine.stop()
        return out

    def finish_live(self, utterance: int, revision: int, transcriber: LiveTranscriber) -> dict[str, Any]:
        """End of speech for audio fed through a LiveTranscriber: transcribe, then on_final.

        An utterance whose audio overflowed the window is refused: no transcript, no proposal, and the
        gate voids whatever the partials prepared for it, so nothing of it can be reused."""
        try:
            text = transcriber.finish()
        except AudioOverflow:
            r = self.gate.reject(utterance, "That was too long for me to take in at once.")
            return self._clarify({"heard": "", "utterance": utterance, "revision": revision}, r.reason)
        return self.on_final(utterance, revision, text)
