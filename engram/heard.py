"""Heard memory: every typed or spoken command, kept as an immutable observation.

The voice client turns speech into text and sends it to POST /commands. Before
this, only what the words turned into was kept (a source, a scheduled task or
an episode). Now the words themselves are stored as an observation of kind
"heard", with who said them and what they led to, so the Memory OS can answer
"what did I say?" and trace any rule, task or plan back to the utterance.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import now
from .ids import new_id


def record_heard(conn: sqlite3.Connection, text: str, who: str, result: dict[str, Any],
                 at: float | None = None) -> str:
    """Store what was heard and what it became. Never updated afterwards."""
    links = {k: result[k] for k in ("source_id", "task_id", "operation_id") if result.get(k)}
    payload = {"text": text, "who": who, "became": result.get("kind"), "status": result.get("status"), **links}
    obs_id = new_id("obs")
    conn.execute(
        "INSERT INTO observation(id, kind, payload, source_id, observed_at) VALUES (?,?,?,?,?)",
        (obs_id, "heard", json.dumps(payload), result.get("source_id"), now() if at is None else at),
    )
    conn.commit()
    return obs_id


def heard(conn: sqlite3.Connection, who: str | None = None, limit: int | None = 10) -> list[dict[str, Any]]:
    """Newest first. limit=None returns everything."""
    rows = conn.execute(
        "SELECT id, payload, observed_at FROM observation WHERE kind = 'heard' ORDER BY observed_at DESC")
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        if who is None or p.get("who") == who:
            out.append({"observation_id": r["id"], "at": r["observed_at"], **p})
        if limit is not None and len(out) >= limit:
            break
    return out
