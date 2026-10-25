# ENGRAM package v0.1: handoff to Bharath (EC03/EC20)

Author: Alex (alex-claude). Owner of storage, API, planner wiring and the merge: Bharath.
Nothing here is wired in. This branch adds files only; no existing file changes.

## Files
| File | What it is |
|---|---|
| `engram/package/ENGRAM.md` | Boot contract for the planner, about 680 tokens by word count (not yet measured with the Qwen tokenizer) |
| `engram/package/engram_seed.json` | Confirmed objects, zones, capabilities, the SOP 004 rule (object AND station), source hashes, unresolved items |
| `engram/practice.py` | Verifier + case runner on a fresh `:memory:` DB and `MockRobot`; refuses anything else |
| `scripts/engram_practice.py` | Runs the cases, prints the table, `--out` saves one record per case |
| `tests/fixtures/engram_practice.jsonl` | 12 cases in the six #53415 families, the two #53427 M2 cases, 5 verifier checks |
| `tests/test_engram_package.py` | Seed hashes match files, seed matches arena + capabilities, budget, verifier checks |

## Proposed loading (your call)
1. Startup: read `ENGRAM.md` once; log its sha256 next to the model hashes (`GET /health` or `/metrics`).
   The deterministic RulePlanner does not need it. Only an LLM planner puts it in the system prompt.
2. Seed: optional. Today `load_arena` + teaching SOP 004 already produce the same entities.
   Use the seed's `rules[].required_station` if you fix the extractor (see finding 1).
3. Per request: keep using `retrieve_memory_bundle`. If you want the recall module
   (`alex/memory-recall`), call `Recall.recall(goal, kinds=["semantic", "negative", "prospective", "spatial"])`
   and add its answers to the bundle. Do not put the whole database in the prompt.
4. Allowed planner output stays the existing plan schema: `{"goal", "steps": [{"op", "args"}], "evidence", "rules_used"}`,
   ops only from `engram/capabilities.py`.

## Mock test command
    python scripts/engram_practice.py
    python -m pytest -q tests/test_engram_package.py

## Findings from the first practice run (head 0ceb45b + this branch, SIMULATED)
PASS 14, FAIL 3, ABSTAIN 1, BLOCKED 1 (the 5 verifier checks are among the passes).
1. p01.b FAIL (#53427 M1): a revised source that moves the goggles' required station to shelf B still plans goggles to the
   bench. `teach.extract_rules` keeps the object and drops the station.
2. p03.a FAIL: "Do not move Sample A to the bench." proposes the same plan as "Move Sample A to the bench."
   Approval still blocks motion, but negation is read as an order.
3. p05.b BLOCKED: there is no way to cancel a scheduled task, so cancel-before-trigger cannot be tested.
4. p04.b ABSTAIN: a trigger at the end of the sentence is refused with the rephrase hint (safe).
5. m2.a FAIL (#53427 M2): "After the experiment finished, what happened to Sample A?" is saved as a waiting
   task; `prospective.parse_when` treats any "After X, ..." as a commitment. Its pair m2.b PASSES: a genuine
   "When X, Y" is still waiting after the database is reopened in a new Engine (same process, not an OS restart).
