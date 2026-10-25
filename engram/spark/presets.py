"""The robot presets a voice request can select.

A preset is a short, fixed request that maps to steps made only of actions in
engram/capabilities.py. The intent model may pick one by name; it never writes
steps. The steps still go through Engine.propose, the checker and a person's
digest-bound approval, like every other plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..capabilities import CAPABILITIES


@dataclass(frozen=True)
class Preset:
    name: str
    request: str              # canonical request text sent to Engine.propose
    steps: tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]
    description: str          # what the intent model is told this preset means

    def plan_steps(self) -> list[dict[str, Any]]:
        return [{"op": op, "args": dict(args)} for op, args in self.steps]


PRESETS: dict[str, Preset] = {
    p.name: p
    for p in [
        Preset("open_gripper", "Open the gripper", (("gripper_open", ()),),
               "Open the robot's gripper (also called the claw, hand or grip), or release what it holds."),
        Preset("close_gripper", "Close the gripper", (("gripper_close", ()),),
               "Close the robot's gripper (also called the claw, hand or grip)."),
        Preset("look_around", "Look around", (("observe", ()),),
               "Look around with the camera and update what the robot knows about where things are."),
    ]
}


def _validate() -> None:
    """Every preset step must be an existing capability with exactly its arguments."""
    for p in PRESETS.values():
        for op, args in p.steps:
            cap = CAPABILITIES.get(op)
            if cap is None or op == "stop":
                raise ValueError(f"preset {p.name}: '{op}' is not an allowed preset action")
            if set(dict(args)) != set(cap.args):
                raise ValueError(f"preset {p.name}: {op} needs arguments {sorted(cap.args)}")


_validate()


def register_presets(engine: Any) -> None:
    """Let the engine plan each preset's canonical request ("Open the gripper") as its fixed steps.

    Uses Engine.register_fixed_request: only planning is skipped. Refusal rules, the checker and a
    person's digest-bound approval still apply to every proposal. Used by VoiceIntegration and,
    at startup, by the HTTP API (engram/api.py).
    """
    for p in PRESETS.values():
        engine.register_fixed_request(p.request, p.plan_steps())
