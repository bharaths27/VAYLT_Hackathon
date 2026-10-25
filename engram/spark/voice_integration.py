"""Voice presets into the existing Engine, and nothing more.

- A partial transcript only prepares, through the SpeculationGate. The only thing a partial
  can do to the engine is STOP, which calls Engine.stop() exactly like the stop button.
- A final transcript that selects a preset becomes an ordinary Engine proposal: Engine.propose
  with the preset's canonical request text, so the refusal rules, the checker and the
  digest-bound approval all apply. This module never approves and never runs a plan.
- A deferred preset ("open the claw after calibration finishes") becomes a "When <event>,
  <task>" commitment through Engine.command, the existing prospective path. When the trigger
  fires, the engine proposes it and a person still approves it.

Call on_partial and on_final from the thread that owns the engine's database connection. The
gate's worker thread only talks to the intent model; every database read (the memory-context
version and the memory bundle) happens on the calling thread.
"""

from __future__ import annotations

from typing import Any, Hashable

from ..engine import Engine
from ..planner import refusal
from ..skills import APPROVED
from .audio_windows import AudioOverflow, LiveTranscriber
from .intent_client import IntentDecision
from .presets import PRESETS, register_presets, voice_presets_enabled
from .voice import Classifier, GateResult, SpeculationGate

ACTING = ("propose", "schedule")   # gate actions that lead to an engine proposal, now or when a trigger fires


def _presets_off(text: str, partial: bool) -> IntentDecision:
    """The classifier while voice presets are turned off: nothing is a preset, and no model is asked."""
    return IntentDecision(None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, model="presets-off")


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
        intent model is never asked, and nothing but a stop is acted on."""
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

    def _prefetch(self, text: str) -> dict[str, Any]:
        return self.engine.memory.retrieve_memory_bundle(text, self.engine.skills.approved_names()).to_dict()

    def on_partial(self, utterance: int, revision: int, text: str) -> dict[str, Any]:
        r = self.gate.on_partial(utterance, revision, text)
        if r.action == "stop":
            return {"kind": "stop", "heard": r.text, "state": self.engine.stop()}
        prepared = self.gate.prepared()
        return {"kind": "partial", "status": r.action, "heard": r.text,
                "preparing": prepared.decision.preset if prepared else None,
                "memory": prepared.bundle if prepared else None}

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
            return {**self.engine.command(preset.request, who="voice"), **base, "preset": preset.name,
                    "reused_prediction": r.reused}
        if r.action == "schedule":
            out = self.engine.command(r.commitment, who="voice")
            if out.get("kind") != "scheduled":
                return {"kind": "none", **base, "reason": "That could not be saved as a future task."}
            return {**out, **base, "preset": r.preset}
        return {"kind": r.action, **base, "reason": r.reason}

    def finish_live(self, utterance: int, revision: int, transcriber: LiveTranscriber) -> dict[str, Any]:
        """End of speech for audio fed through a LiveTranscriber: transcribe, then on_final.

        An utterance whose audio overflowed the window is refused: no transcript, no proposal, and the
        gate voids whatever the partials prepared for it, so nothing of it can be reused."""
        try:
            text = transcriber.finish()
        except AudioOverflow:
            r = self.gate.reject(utterance, "That was too long for me to take in at once. Please say it again.")
            return {"kind": "clarify", "heard": "", "utterance": utterance, "revision": revision, "reason": r.reason}
        return self.on_final(utterance, revision, text)
