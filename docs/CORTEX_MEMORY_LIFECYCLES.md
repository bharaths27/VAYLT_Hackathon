# Seven responsibilities and the learning loop

| Memory | Implemented home | Lifecycle and authority |
|---|---|---|
| Working | request-scoped bundle and model_context | Bounded exact/lexical/graph retrieval; mandatory context overflow is explicit. Stale predictions are discarded. |
| Semantic | source/chunk/assertion, cx_source/rule/fact/constraint/goal, object bindings | Original source, current/superseded/disputed/deleted status; required station preserved. Declared facts differ from observations. Unsupported exceptions become unresolved gates. |
| Episodic | observation, episode, operation_event, cx_episode | Requests, proposed/dispatched/reported/verified/unknown effects; SIMULATED/PHYSICAL classes persist. Immutable original evidence is retained except authorized deletion/redaction. |
| Procedural | skill_version + cx_skill; cx_derived task_recipe | Candidate → tested → approved → revoked; current digests and source dependencies checked. Exact-goal reuse; new goals can compose new procedures from supported primitives. |
| External/retrieval | source FTS, exact SQL, bounded prerequisite edges, sensor ingest, existing camera proxy | No fictitious embedding model. Authorization/current source filtering precedes model serialization; retrieval failure is not absence. |
| Parametric | selected Qwen runtime and cx_manifest | Contract, model/provider and prompt identities recorded; optional artifact hashing in probe. No model-weight updates. The hosted intent model remains optional cloud advice. |
| Prospective | prospective_task + cx_commitment/event/wake/due | Persist before acknowledgment; cancellation generations, expiry, event idempotency, restart reconciliation; current approval is still required. |

## A new task creates usable derived memory
Resolve a supported goal → collect current relevant evidence and prerequisite closure → optionally request a Qwen proposal → independent symbolic goal verifier → bounded deterministic composer fallback → persist an evidence-linked task recipe → re-read that exact recipe as the operation's steps → generate and test a candidate skill → publish by explicit review or configured deterministic publication policy → current human action approval → actual observed outcome.

Do not confuse a well-formed plan with a correct goal. The verifier walks state transitions and checks the requested destination, all relevant prerequisite locations, permitted object/effect scope, finite bounds and supported capabilities. Model-produced extra moves are rejected. Tests deliberately feed wrong plans and always-refuse alternatives to the verifier.

## Idle growth
An actual idle worker processes episode summaries and failures incrementally, derives supported dependency relationships, generates new simulated practice goals and candidate procedures, and optionally obtains Qwen hypotheses. It records original evidence, dependency versions, verifier and origin. Repeated cycles deduplicate. A user turn advances an activity generation, making late dream results ineligible. Qwen calls are serialized; stop/control is not waiting for cloud advice. Model timeouts are finite but a low-level inference cannot always be instantly interrupted; stale results are discarded instead.

## What is and is not learned
Implemented: evidence-backed derived recipes, code-verified symbolic dependency relationships, episode summaries, candidate lessons/hypotheses, self-proposed practice and versioned skills.
Not implemented or claimed: neural weight training, unrestricted code generation, arbitrary motor-policy acquisition, universal natural-language understanding, permanent truth from model confidence, or online auto-download of arbitrary training material. The hypothesis generator proposes knowledge; only appropriate evidence/review can establish new world facts.

## Absolute Zero adaptation
The paper's actual system proposes and solves verifiable reasoning tasks and performs RL weight updates. This implementation borrows the proposer/solver/verifier/counterexample pattern at the memory/procedure layer. It does not claim equivalent training or benchmark results. Software-generated practice remains explicitly simulated. Its oracle is separate code and fixtures, not the same model repeating its guess.
