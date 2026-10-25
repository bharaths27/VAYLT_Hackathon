from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


class CortexError(ValueError):
    """A typed, non-secret-bearing failure suitable for an operator response."""
    code = "INVALID_INPUT"


class Conflict(CortexError):
    code = "CONFLICT"


class Stale(CortexError):
    code = "STALE_EVIDENCE"


class Missing(CortexError):
    code = "NEEDS_INPUT"


class Denied(CortexError):
    code = "DENIED"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def uid(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def finite(value: Any, name: str, minimum: float = 0, maximum: float = 1e12) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise CortexError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise CortexError(f"{name} is outside its bounds")
    return value


def text(value: Any, name: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CortexError(f"{name} must be nonempty text of at most {maximum} characters")
    return value.strip()


@dataclass(frozen=True)
class Goal:
    """A complete supported goal; constructing one does not grant authority."""
    kind: str
    targets: tuple[str, ...] = ()
    zone: str | None = None
    primitive: str | None = None
    args: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return digest(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["targets"] = list(self.targets)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Goal":
        if not isinstance(d, dict) or set(d) - {"kind", "targets", "zone", "primitive", "args"}:
            raise CortexError("invalid goal fields")
        kind = d.get("kind")
        if kind not in {"fetch", "goto", "inspect", "primitive"}:
            raise Missing("unsupported goal kind")
        targets = d.get("targets", [])
        if not isinstance(targets, (list, tuple)) or len(targets) > 8 or any(not isinstance(x, str) for x in targets):
            raise CortexError("invalid goal targets")
        if len(set(targets)) != len(targets):
            raise CortexError("duplicate goal target")
        zone, primitive, args = d.get("zone"), d.get("primitive"), d.get("args", {})
        if zone is not None and not isinstance(zone, str):
            raise CortexError("invalid goal zone")
        if primitive is not None and not isinstance(primitive, str):
            raise CortexError("invalid goal primitive")
        if not isinstance(args, dict):
            raise CortexError("invalid goal arguments")
        canonical(args)
        if kind == "fetch" and (not targets or not zone or primitive or args):
            raise Missing("fetch needs objects and a destination")
        if kind == "goto" and (not zone or targets or primitive or args):
            raise Missing("goto needs one destination")
        if kind == "inspect" and (not targets or zone or primitive or args):
            raise Missing("inspect needs objects")
        if kind == "primitive" and (not primitive or targets or zone):
            raise Missing("primitive needs a supported name and arguments")
        return cls(kind, tuple(targets), zone, primitive, dict(args))


@dataclass(frozen=True)
class Policy:
    """Trusted startup policy, never loaded from retrieved source text."""
    max_steps: int = 32
    max_nodes: int = 64
    max_depth: int = 8
    max_bundle_bytes: int = 12000
    freshness_s: float = 60.0
    approval_ttl_s: float = 120.0
    idle_after_s: float = 60.0
    dream_budget_s: float = 10.0
    dream_batch: int = 4
    max_model_calls_per_dream: int = 2
    cloud_enabled: bool = False
    # Approval of deterministic software procedures is distinct from actuation.
    auto_publish_verified: bool = False
    live_enabled: bool = False
    clock_trusted: bool = False

    def __post_init__(self):
        for n in ('cloud_enabled','auto_publish_verified','live_enabled','clock_trusted'):
            if not isinstance(getattr(self,n),bool):raise CortexError(f'invalid policy {n}')
        for n in ("max_steps", "max_nodes", "max_depth", "max_bundle_bytes", "dream_batch", "max_model_calls_per_dream"):
            v = getattr(self, n)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise CortexError(f"invalid policy {n}")
        if not 1 <= self.max_steps <= 64 or not 1 <= self.max_nodes <= 256 or not 1 <= self.max_depth <= 16:
            raise CortexError("invalid planning resource bounds")
        if not 1024 <= self.max_bundle_bytes <= 64000 or not 1 <= self.dream_batch <= 16:
            raise CortexError("invalid memory resource bounds")
        for n in ("freshness_s", "approval_ttl_s", "idle_after_s", "dream_budget_s"):
            finite(getattr(self, n), n, 0.01, 86400)

    def fingerprint(self) -> str:
        return digest(asdict(self))


# Robot operations with a dedicated control path, never a goal or a plan step. STOP is Brain.stop: the in-memory and durable
# latch, the STOP count, the revocation of every pending proposal, and the adapter's own stop. Run as an ordinary step it
# would stop the robot with none of that, leaving a pending motion usable after a reset or a restart.
CONTROL_OPS = frozenset({'stop'})


MEMORY_KINDS = ("working", "semantic", "episodic", "procedural", "external", "parametric", "prospective")
