# ENGRAM Cortex — memory combination handoff

## Release decision
**Ready for independent code review and mock integration. Not a Pi/hardware release certificate.**

Supported integration for the demo: the **sidecar** (Integration, section 1). The existing brain stays the only executor; Cortex runs beside it, simulated, on loopback. The console bridge is blocked and Cortex as the executor comes later.

This is executable additive software, not only a design packet. It implements seven-memory lifecycle services, task-conditioned memory and skill synthesis, independent symbolic checking, idle consolidation/practice, Qwen context loading, bounded hosted intent-model advice, durable commitments, current approval, and a memory inspector/API.

It is NOT a claim that every original EC ticket is complete, every instruction is understood, or every robotic skill can be learned without motor training. It does not update model weights or reproduce Absolute Zero's reinforcement-learning training. It does not invent physical observations.

## Source and compatibility boundary
Inspected upstream: `bharaths27/VAYLT_Hackathon`, submitted release `9849350c9cebd44fd226d9eb76e1460ef395190d`; Alex's recall/package/integration `595f3394277e4435ce8636eb959f619203a801e7` remains his work. Preserve it. Upstream source was read through the connector; a complete clone was not available here. The fork `mrrCarter/VAYLT_Hackathon` reports write metadata but the app's actual branch-create returned 403. No remote branch, commit, PR, merge or deployment was created by this package.

The patch adds `engram/cortex/`, tests, scripts, a synthetic arena and these docs. It does NOT overwrite `engram/engine.py`, Alex's recall/package, Brian's driver, the submitted UI, or Warden's speech changes under `engram/spark/`. This is a new opt-in runtime using the existing table shapes plus `cx_*` lifecycle metadata and the existing Brian adapter behind a stricter wrapper. It is NOT already merged into the old engine. Select one canonical executor at deployment; never run both against the controller or same active DB.

Existing original source/assertion/skill records are not silently assumed to meet the new provenance contract. Test a consistent backup. `Store.adopt_legacy_source(...)` is explicit review/reingestion; inspect the method before use. Legacy episodes may be recalled with unknown evidence class but are not automatically executed or approved by Cortex. Alex's original ENGRAM contract is preferred for Qwen when present; otherwise the additive contract is used. His existing recall code remains intact, not secretly replaced in the release.

## Start in mock mode
From a reviewed repository worktree with the additive files applied:

```bash
python -m pytest -q tests/cortex
python scripts/cortex_demo.py --out cortex-demo-results.json
```

Requirements: Python 3.11+; FastAPI/Pydantic 2/httpx/uvicorn and pytest. This branch was tested with Python 3.11, the Pi's version (development also used 3.13). Validate the actual Pi environment's installed dependencies; no provisioning or upgrades are performed by these commands.

A reviewed bootstrap fixture is included. It teaches a prerequisite, a new named task (`prepare the inspection`) and a declared preference; it never invents observations or authorizations. Load it only into an inactive mock review DB:

```bash
python scripts/cortex_seed.py --db /private/cortex-review.db --reviewed-by carther
```

The loader is idempotent, refuses a changed seed under the same identity, and does not restore a source after you supersede or revoke it. It refuses known physical vaults. The synthetic arena/seed is not a real-world calibration or a claim about the user's actual preferences.

Use the existing environment or an isolated venv. The dependency declaration is `requirements-cortex.txt`. Do not replace the existing requirements lock without review.

```bash
# Secret in process environment; do not paste the resulting value into room/logs.
export CORTEX_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export CORTEX_OPERATOR=carther
export CORTEX_DB=/absolute/private/path/cortex-review.db
export ENGRAM_ARENA=fixtures/cortex/arena.json
export CORTEX_LIVE=0
export CORTEX_CLOUD=0
export CORTEX_QWEN=0
export CORTEX_IDLE=1
uvicorn engram.cortex.api:create_app --factory --host 127.0.0.1 --port 8010
```

This is the workstation review path. On the Pi use `scripts/cortex_sidecar.sh` (see Integration), which sets all of this, keeps the token in a 0600 file and refuses a non-loopback bind. Do not print the token. `/cortex` is an optional inspection page; its token stays in page memory, not localStorage or URLs. Keep Cortex on loopback and reach it with `ssh -L`; no LAN exposure has been reviewed. Camera and memory reads also require authentication. Mutations are refused before any body byte is read: a cross-site browser request (Sec-Fetch-Site: cross-site) gets 403 first, then a missing or wrong credential gets 401. A body over 300 KB, declared or chunked, gets 413 (a malformed Content-Length, 400). Cortex never touches the existing brain's stop path.

The included arena is SYNTHETIC and intentionally not a physical calibration. For live acceptance set the actual reviewed `ENGRAM_ARENA`; do not use synthetic positions/tag dimensions as physical facts.

## What to demonstrate
`cortex_demo.py` actually exercises this sequence:
1. Ingest a source requiring goggles at the PPE station before handling Sample B.
2. Request a previously uncompiled task; derive a source-backed prerequisite-aware task recipe, persist it, read it back and use its steps.
3. Independently verify a candidate skill in multiple symbolic layouts. The demo explicitly enables deterministic verified-skill publication through startup policy. This does not approve physical actions.
4. Approve the exact mock operation and verify its final simulated state.
5. Reuse the approved skill; no new model call is needed. The demo resets the target in its fake world for a controlled repeat; this is not a robot measurement.
6. Accept a source correction to the required station. The old pending plan becomes invalid and the old skill is revoked; the same task gets the changed requirement.
7. Let the real background worker wake while idle, create evidence-linked summaries/relationships and simulated practice, with zero added motor calls.
8. Start two separate Python processes to show a future task persists, its cancelled sibling stays cancelled, and duplicate trigger delivery creates one proposal without dispatch.

The demo uses no actual language-model inference. That makes it a test of the memory/control logic, not proof that Qwen understands every sentence.

## Enable and prove Qwen memory access
Keep the existing Qwen3-1.7B server; do not install another model. First run the read-only/no-motor synthetic diagnostic:

```bash
python scripts/cortex_model_probe.py --qwen \
  --url http://127.0.0.1:8080 \
  --model-file /home/starrasp/offline/models/Qwen3-1.7B-Q8_0.gguf \
  --out qwen-memory-proof.json
```

The script records the actual loaded contract hash, exact templated prompt hash/token count, model response and independent goal-check results for two source revisions. A failure remains a failure; it does not substitute the expected answer. Optional model-file fingerprints only prove those bytes stayed unchanged, not that an arbitrary process loaded them.

After acceptance, set `CORTEX_QWEN=1`, `CORTEX_LLM_URL=http://127.0.0.1:8080`, and a measured context budget (`CORTEX_INPUT_TOKENS`, default 2048). `/apply-template` applies Qwen's actual chat template with thinking disabled, `/tokenize` counts the exact prompt, and `/completion` uses a JSON schema. Retrieved source text is data. Mandatory constraints are not silently removed to fit. A model problem or invalid plan returns to the explicit local compiler and is labeled fallback. The runtime first resolves a supported typed goal (grammar or taught named task); it is not universal free-language understanding.

The present client accepts loopback only. It does not give Qwen unrestricted internet/shell access. Qwen's own model weights are not modified.

## Hosted intent-model integration
Supported advisory stages: `route`, `relevance`, `skill_selection`, `dream_priority`, `candidate_review`, `turn`. Responses are typed and validated; the endpoint is fixed and the model id comes only from `TYPESAFE_INTENT_MODEL` (no default in code; unset means the advisor is unavailable); calls have time and count budgets, no redirects and no credential logging. Relevance currently records sufficiency advice rather than replacing lexical/graph retrieval. Cloud advice may request clarification or suppress a candidate; it cannot create permission, waive a failed verifier, or control motors.

```bash
# Bash: enter the key without printing it or putting its value in command history.
read -rsp 'TypeSafe key: ' TYPESAFE_API_KEY; echo
export TYPESAFE_API_KEY
export TYPESAFE_INTENT_MODEL=<model id from the TypeSafe console>
python scripts/cortex_model_probe.py --intent --out intent-synthetic-proof.json
```

Runtime cloud mode additionally needs `CORTEX_CLOUD=1` and explicit per-request/source disclosure flags. Default is local-only. Only relevant opted-in data is sent, not the whole vault. A simulated or cloud-produced hypothesis stays a candidate. Use the server process, never iPad JavaScript, for the key. Rotate the temporary key after testing. The earlier speech package is separate; Warden owns its integration and it can call `/turn/advice` through the authenticated boundary.

## Idle dreaming and autonomous skill creation
`CORTEX_IDLE=1` starts a bounded background worker. `CORTEX_IDLE_SECONDS` defaults to 60. It processes an incremental episode backlog, constructs dependency relationships and practice goals, verifies procedures, and optionally asks Qwen for evidence-linked candidate hypotheses. It yields/discards stale results when foreground activity or dependency changes intervene. The worker never opens a robot handle. It does not continuously enumerate the full vault on every request.

Default publication is human-reviewed. `CORTEX_AUTO_PUBLISH=1` is a trusted startup policy allowing software to publish only procedures that pass the independent symbolic verifier under current source/policy digests. It never grants a physical operation approval. Use this flag in the mock demonstration, then decide separately whether its bounded scope is acceptable for the real installation.

A generated task recipe is derived procedural knowledge, not a new observation. Hypotheses and failure lessons stay candidates. Verified summaries/relationships cite their sources; source withdrawal invalidates descendants and dependent skills/plans. Generalization is bounded composition over registered actions and exact-goal skill reuse, not unconstrained code invention or new grasp physics.

## Durable tasks and permissions
Event commitments use persisted inbox/wake records, task generation, owner, expiry and proposal reference. Duplicate event IDs with changed payload conflict. Cancellation before dispatch prevents the pending effect; cancelling an uncertain/running effect is not falsely reported as complete. Restart changes interrupted execution to unknown; it never automatically retries it.

Timed UTC tasks additionally require `CORTEX_CLOCK_TRUSTED=1`; clock rollback blocks the sweep until an operator re-establishes trusted time. The sweep never stops on an error: failures are logged, retried with backoff up to 60 s, and recorded as a decision once per 10 minutes while the same failure repeats. Due events produce proposals, not motion. An operator saying "finished" is recorded as an operator-confirmed trigger, not instrument evidence.

API identity comes from the server credential mapping, not caller `who` or `approved_by`. Plan and skill digests, expiry, owner, current source dependencies and single-use state are rechecked. A known plan digest alone is not authority.

## Integration
Three ways to connect Cortex to the robot stack, in the order they become safe. **Only the first is supported for the demo.**

### 1. Sidecar: supported for the demo
The existing brain (`engram/api.py` on port 8001, `engram.db`) stays the ONLY executor, and the console (8000/8443) keeps pointing at it. The old brain, the console, the robot server and the voice stack are not changed. Cortex runs beside them as one separate process:

| | Sidecar |
|---|---|
| Start | `sh scripts/cortex_sidecar.sh start` in the repo checkout on star-pi; also `status`, `stop` |
| Database | its own, `~/.cortex-sidecar/cortex.db`; a path named, resolving to, or hard-linked to the brain's `engram.db` is refused; the state dir is resolved before the outside-the-repository check |
| Arena | the real lab arena, `fixtures/lab/arena.json` |
| Mode | `CORTEX_LIVE=0 CORTEX_CLOUD=0 CORTEX_QWEN=0 CORTEX_IDLE=1`: the fake adapter, every run labeled SIMULATED, no model calls |
| Network | loopback only: `127.0.0.1:8010` by default (`::1` also allowed); any other bind address is refused |
| Camera | none; `CORTEX_CAMERA_URL` is refused |
| Token | generated once into `~/.cortex-sidecar/token` (mode 0600, outside the repo); never printed |

The script refuses `CORTEX_LIVE`, `CORTEX_CLOUD` or `CORTEX_QWEN` other than 0, and starts the server under `env -i` with exactly these variables, so nothing else in the caller's shell (for example a hosted-model key) reaches it. It writes a pid file and checks it against the process command line before `stop`.

Reach the inspector from a laptop: `ssh -L 8010:127.0.0.1:8010 starrasp@star-pi`, then open `http://127.0.0.1:8010/cortex`. Read the token from the token file over SSH and paste it into the page; never post it. The page keeps it in memory only.

**Rollback = `sh scripts/cortex_sidecar.sh stop`.** It kills the one Cortex process. Nothing else was touched, so there is nothing else to restore; the state directory can be deleted afterwards.

The sidecar shows teaching, task-conditioned memory, plans and approvals over the real lab's objects and zones, source correction and idle dreaming, all simulated. It moves nothing, has no camera and does not call Qwen. The inspector has approve/reject, STOP, **Find operations to verify** with a per-operation Verify (a written note is required), and **Clear stop**, so a stop or an unknown outcome can be reconciled from the screen.

### 2. Console bridge: blocked follow-up
`python scripts/cortex_console_patch.py --repo .` prints the bridge diff (`console/server/brain.py`, `console/server/adapters/memory.py`) for review. **`--apply` refuses** (exit 2, nothing written). The bridge never added auth to the console: it attaches the Cortex operator token, server side, to every request the console makes, while the console's own routes accept anyone on the LAN. All of these must be closed before it can be applied:
1. **Console authentication (P1).** The console's mutating routes (`/commands`, `/operations/{id}/approve|reject|verify`, `/rules`, `/stop`, `/stop/clear`, `/skills/{id}/{decision}`, `/stt`) take no credential on 8000/8443. Through the bridge, an unauthenticated LAN client taught sources and approved and executed operations as the Cortex operator. The console must authenticate (pair) its mutating routes first (CXV6).
2. **Typed or spoken "stop".** Cortex latches its stop, but the console's `engine.stopped` stays false: `_brain_command` in `console/server/main.py` has no `kind == "stop"` branch that calls `engine.stop`. The generator does not touch `main.py`.
3. **Verify and rearm are console-local.** Console Verify and `/stop/clear` change only the console's state. In live mode Cortex keeps refusing clear-stop until each unresolved operation is verified on Cortex. Relay console Verify, with its note, to `POST /operations/{id}/verify`, or use the inspector.
4. **Approve blocks for the whole run.** Cortex's approve call returns only when execution ends. The bridged client times out after 60 s, so a physical run longer than that shows "I could not confirm what the robot did" on the console while Cortex is still executing. Live use needs an asynchronous approve (accept, then poll the operation).
5. **Skill approval needs the inspected digest.** Console skill Approve sends no digest, so the bridge answers 409 and points to the inspector. Rules with zone/tag/trigger fields answer 422. Both used to be 500.
6. **Two brains.** PR #5's voice presets are wired to the OLD engine. Switching the console to Cortex would leave voice on the old brain and the console on Cortex. Rewire them in the same change.

Already fixed in the diff: the generated `brain.py` imports Cortex lazily (only when `CORTEX_BRIDGE=1`), so a patched console still starts from `console/` without the repo root on PYTHONPATH; Cortex's `mode` is carried onto the card, and a simulated success is labeled SIMULATED, never VERIFIED; `/evidence/<console ledger id>` falls back to the console's ledger.

### 3. Cortex as the executor: later
Only after the sidecar has been demonstrated, and only when all of these hold:
- a calibrated sensor producer posts to `/observations/sensor` with its own `CORTEX_SENSOR_TOKEN` (Brian). Without it a live fetch aborts at pickup ("no fresh in-operation observation");
- verify and clear-stop are on the screen the operator actually uses. The inspector has them; the console needs bridge blockers 1, 3 and 4 closed;
- the old brain on 8001 is restarted WITHOUT a robot URL, so it cannot also drive the robot (one executor);
- `CORTEX_LIVE=1` with `CORTEX_LIVE_ACK=WITNESSED_STOP_AND_SINGLE_EXECUTOR`, after Brian's witnessed stop test (CXV7, CXV8).

In live mode every successful run ends `needs_verification`, because a controller ACK is not proof. It needs an operator observation or sensor evidence before clear-stop is allowed.

### Notes for the integrator
- **Qwen contract hash.** `QwenClient` prefers `engram/package/ENGRAM.md` when that file exists. When Alex's package lands, the contract hash changes, so earlier `cortex_model_probe.py --qwen` evidence (CXV3) no longer matches; re-run it after the merge.
- **PR #5 voice presets** call the old engine; see bridge blocker 6.

## Camera and physical adapter (executor path only; the sidecar has neither)
Set `CORTEX_CAMERA_URL` to an existing local JPEG route, for example `http://127.0.0.1:8200/robot/frame` after confirming that route. The producer polls that route only while a viewer is attached: an open `/video.mjpg` stream holds it, each `/frame.jpg` request keeps it for 5 s, and it stops 5 s after the last viewer leaves (`/camera/status` reports `polling` and `viewers`). `CORTEX_CAMERA_FPS` bounds the rate while polling (default 10, configurable 1–30). One producer, one newest-frame buffer, sequence IDs and explicit capture/receive timestamps; no new camera opener. The inspector allows one request in flight. `/video.mjpg` is an authenticated MJPEG stream. A delivery timestamp is not proof of fresh capture when the camera doesn't supply capture time. These are tested transport mechanisms, not measured Pi FPS or Safari performance.

Live startup (executor path 3 above) requires `CORTEX_LIVE=1`, `CORTEX_LIVE_ACK=WITNESSED_STOP_AND_SINGLE_EXECUTOR`, actual arena, existing adapter/secret-file configuration and Brian's witnessed stop. This acknowledgement is not hardware evidence by itself. Stop the competing old executor before moving; do not stop Brian's actual controller or change its watchdog casually.

The wrapper imports the existing Brian adapter. Its `E_UNVERIFIED`/unsensed effects stay UNKNOWN here rather than becoming confirmed success. A controller ACK alone never verifies a pickup. Physical task outcomes require current sensor evidence or an explicitly recorded human observation. Unknown outcomes latch stop and do not retry.

`/observations/sensor` accepts calibrated object/zone observations from a separately configured `CORTEX_SENSOR_TOKEN`, not the browser operator token. Required fields: object_id, zone, original captured_at UTC, boot_id, sequence, frame_id, calibration_id. Replay with changed data conflicts; old timestamps stay old. **Brian must connect the actual calibrated perception producer to this endpoint. A bare detected tag does not establish a zone or successful grasp.** No physical sensor evidence was fabricated or collected here.

## Persistence / rollout
The sidecar uses its own fresh DB and never opens `engram.db`. Test a fresh mock DB first. To inspect existing data, use the SQLite backup API, not a raw copy of a live WAL database:

```bash
python scripts/cortex_backup.py /private/current.db /private/cortex-review-copy.db
```

Never point both old/new engines at the same active database or controller. The new runtime enforces its own process lock, but it cannot constrain an unmodified old engine that ignores that lock. For the executor path: stop/reconcile prior running effects, review legacy source adoption, then perform a deliberate one-writer cutover. No rollout, destructive migration, record deletion or auto-enable of hardware is performed by the package.

## Remaining release evidence
Run the full original repository suite on the eventual combined exact commit; repeat on the Pi; rehearse the sidecar start, inspector tunnel and stop on star-pi; run real Qwen/intent-model probes and human speech tests; validate actual iPad audio/video, calibration and independent stop; review dependency and security settings. This local test suite is not independent human review, multi-tenant security certification, or proof of every EC ticket.
