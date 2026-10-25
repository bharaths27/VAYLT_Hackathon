"""Turn a taught source into cited rules.

Very small on purpose: it only understands sentences like
"Safety goggles must be at the bench before handling Sample B."
Each rule it finds is stored as an assertion that points at the exact chunk,
so the robot can always say where it learned it.
"""

from __future__ import annotations

import re

from .memory import Memory

# How a person states a rule: "Remember (that) ...", "Note (that) ...", "From now on, ...", "Rule: ...",
# "New rule: ...", "Learn (that) ..." (the console parser's rule prefixes too). The brain recognises a rule here
# and only here: Engine.command teaches it, and parse_when refuses to schedule it, so the two cannot drift apart.
_RULE_PREFIX = re.compile(r"^\s*(?:new rule|rule|remember(?: that)?|from now on|note(?: that)?|learn(?: that)?)\b[\s:,-]*",
                          re.IGNORECASE)


def rule_body(text: str) -> str | None:
    """The rule in a statement like "Remember that X" (that is, X), or None when the text does not state a rule."""
    m = _RULE_PREFIX.match(text)
    return text[m.end():].strip() if m else None


_BEFORE = re.compile(
    r"(?P<pre>[A-Za-z0-9 ]+?)\s+(?:must|should|need to|needs to)\s+be\s+(?:at|on|in)\s+the\s+"
    r"(?P<zone>[A-Za-z0-9 ]+?)\s+before\s+(?:handling|using|moving|touching)\s+(?P<subj>[A-Za-z0-9 ]+)",
    re.IGNORECASE,
)


def extract_rules(memory: Memory, source_id: str) -> list[str]:
    """Find "X before Y" rules in a source and assert them. Returns assertion ids."""
    found: list[str] = []
    chunks = memory.conn.execute(
        "SELECT id, text FROM source_chunk WHERE source_id = ? ORDER BY start_char", (source_id,)
    ).fetchall()
    for chunk in chunks:
        for m in _BEFORE.finditer(chunk["text"]):
            pre = memory.resolve_object(m.group("pre"))
            subj = memory.resolve_object(m.group("subj"))
            if len(pre) != 1 or len(subj) != 1:
                continue
            found.append(memory.assert_fact(subj[0].object_id, "requires_before", pre[0].object_id,
                                            source_id=source_id, chunk_id=chunk["id"]))
    return found
