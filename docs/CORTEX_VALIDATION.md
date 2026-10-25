# Cortex validation tickets

These tickets take Cortex from "tests pass on a workstation" to "a judge can watch the robot use what it learned".
Run them in order (CXV10 is blocked). Each ticket names one owner, the exact commands, what counts as a pass, and the evidence to post.
Post evidence as a reply under the ticket's card in Senti. Include the exact commit, the command and the raw output file. A summary on its own does not count.

General rules for every ticket:
- **Exact head.** Record `git rev-parse HEAD` first. Evidence from another commit does not count.
- **Separate checkout.** On the Pi, test in a new directory (for example `~/cortex-review`), never inside the checkout that the live brain runs from.
- **No motors** until CXV8. CXV1 through CXV7 never open the robot adapter.
- **Write the expected result first.** For challenge sets, write the expected outcome before running, so the code under test is not its own oracle.
- **Never paste keys.** Read them with `read -rsp`. A key that lands in a log, a room message or a commit gets rotated.
- **A failure stays a failure.** Post the raw output, mark the card blocked and say what failed. Do not rerun until it passes and post only the pass.

## CXV1: Code review gate on the exact head
- **Owner:** warden (review), then Bharath (merge decision)
- **Evidences:** EC01, EC20
- **Do:** independent review, plus luna's review of the PR at its head SHA. Run the full original suite, not only `tests/cortex`:
  ```bash
  python -m pytest -q tests            # whole repo, Cortex included
  (cd console && python -m pytest -q tests)
  node --test "tests/spark/test_*.mjs"
  python scripts/cortex_demo.py --out cortex-demo-results.json
  ```
- **Pass:** every suite green at the PR head SHA; demo `all_passed: true`; luna APPROVE at that SHA; no secret and no hard-coded hosted-model id in the diff.
- **Evidence:** test counts, demo JSON, luna verdict line with the SHA.

## CXV2: Install and run the suite on the Pi
- **Owner:** Brian (Pi access), with Bharath
- **Evidences:** EC01, EC19
- **Do:** on star-pi, in a separate checkout of the PR head:
  ```bash
  python3 --version                     # record it; the Pi runs 3.11, development used 3.11 and 3.13
  python3 -m venv ~/cortex-venv && . ~/cortex-venv/bin/activate
  pip install -r requirements.txt -r requirements-cortex.txt && pip check
  python -m pytest -q tests && python scripts/cortex_demo.py --out ~/cortex-demo-pi.json
  ```
- **Pass:** the same counts as CXV1 (minus tests that skip for a missing tool, each named: node-only inspector tests skip without node), `pip check` clean, demo passes, the Linux-only sidecar start/stop test runs and passes, and the live brain on port 8001 is still serving the whole time.
- **Evidence:** Python version, `pip freeze | grep -iE 'fastapi|pydantic|httpx|uvicorn'`, pytest summary line, demo JSON.

## CXV3: Real Qwen probe on the Pi (no motors)
- **Owner:** Brian
- **Evidences:** EC19, EC20 (parametric memory), EC07
- **Do:** with the existing llama-server already running on 127.0.0.1:8080:
  ```bash
  ~/llama-server --version              # record it; builds without --jinja ignore "thinking off"
  python scripts/cortex_model_probe.py --qwen --url http://127.0.0.1:8080 \
    --model-file /home/starrasp/offline/models/Qwen3-1.7B-Q8_0.gguf --out ~/qwen-memory-proof.json
  ```
- **Pass:**
  - Every probe case returns a plan that passes the independent goal check, *or* fails and is labeled fallback.
  - Both source revisions produce the plan their rule requires.
  - The applied template has thinking disabled (the probe records it).
  - The prompt token count is under `CORTEX_INPUT_TOKENS`.
  - Latency is recorded per case, and `CORTEX_LLM_TIMEOUT` is set from the measured p95.
- **Evidence:** the whole `qwen-memory-proof.json`, including the contract hash, prompt token count, thinking-off check, per-case goal check and latency.
- **Rerun when** Alex's `engram/package/ENGRAM.md` lands: Cortex prefers that contract, so the contract hash changes.
- **Workstation result (x86 Ryzen 5950X, 3 threads, llama.cpp b11552, the same Qwen3-1.7B-Q8_0 file with sha256 `061b54da...6590cb1a`, server flags copied from `ai_module/start_model.sh`):**
  - The wire protocol works.
  - Two mismatches the author's stub hid were fixed: schema key order, and a plan schema that didn't restrict ops and arguments.
  - Qwen still failed every fetch that has a prerequisite; the local compiler planned those correctly.
  - Single-step plans took 15–37 s; multi-step plans took 20–60 s.
  - Expect the Pi to be slower. Plan the demo with the compiler first and Qwen only for tasks the compiler cannot plan.

## CXV4: Live hosted intent-model probe
- **Owner:** warden (done once on the workstation; repeat from the Pi network when Carter enables cloud mode)
- **Evidences:** EC13, EC19
- **Do:**
  ```bash
  read -rsp 'TypeSafe key: ' TYPESAFE_API_KEY; echo; export TYPESAFE_API_KEY
  export TYPESAFE_INTENT_MODEL=<model id from the TypeSafe console>
  python scripts/cortex_model_probe.py --intent --out intent-synthetic-proof.json
  ```
- **Pass:** every call returns `status: ok` with a score for every question; the key appears in no output (`grep -c` on the key prefix returns 0); "Do not ..." scores `negated` at or above 0.5.
- **Evidence:** the JSON file and the grep count.
- **Warning:** the thresholds (negated 0.5, ambiguous 0.8) are not calibrated. In the first run, "Open the claw." scored ambiguous 0.73, just under the 0.8 cut. Calibrate on a labeled phrase set before trusting cloud mode in the demo.

## CXV5: Independent memory challenge set
- **Owner:** Alex
- **Evidences:** EC15, EC03, EC12
- **Do:** write at least 20 held-out cases in a JSONL file *before* running them: `{request, taught_sources, expected}`. Include at least two of each:
  - a source correction that must change the plan;
  - a withdrawn source whose derived recipe and skill must go stale;
  - a negation;
  - a future task ("after X");
  - a recall question that must not act;
  - a stale observation;
  - two contradictory sources;
  - a cancelled future task followed by its trigger.

  Run them against a fresh mock vault, then post expected versus actual.
- **Pass:** every safety case fails closed, meaning no proposal for negation or recall, no execution for a cancelled task, and stale descendants actually go stale. Report the accuracy of the rest; it is not a gate.
- **Evidence:** the JSONL file, the runner, and the results table.

## CXV6: Sidecar and inspector on the iPad
- **Owner:** Verna
- **Evidences:** EC04
- **Do:** on the Pi, from the PR checkout: `sh scripts/cortex_sidecar.sh start` (Cortex beside the existing brain: own database, real lab arena, no robot, loopback only). From the iPad or a laptop: `ssh -L 8010:127.0.0.1:8010 starrasp@star-pi`, then open `http://127.0.0.1:8010/cortex` and paste the token from `~/.cortex-sidecar/token`. Never post the token.
- **Pass:**
  - all seven memory kinds are listed;
  - rejecting a proposal shows as rejected after a page reload;
  - STOP, then "Find operations to verify", verify one with a note, "Clear stop", and the next approve works;
  - the token is never in a URL or in localStorage (check Web Inspector);
  - port 8010 listens on loopback only (`ss -ltnp | grep 8010`), and 8001 serves throughout;
  - the existing console (speech, push-to-talk, STOP) still works and still talks to 8001.
- **Evidence:** screenshots of the inspector, Web Inspector storage view, the `ss` line, and a screen recording of one reject and one verify/clear-stop.
- **Rollback:** `sh scripts/cortex_sidecar.sh stop`. Nothing else on the Pi changed.

## CXV7: Sensor ingest and the one-writer cutover rehearsal (no motors)
- **Owner:** Brian (sensor), Bharath (cutover)
- **Evidences:** EC06, EC09, EC02
- **Do:**
  1. `python scripts/cortex_backup.py <live.db> <backup.db>` and verify the backup opens.
  2. Start Cortex on a *copy*. Start a second Cortex process on the same vault; it must refuse to start ("another Cortex process owns this vault").
  3. Post one calibrated tag observation to `/observations/sensor` with the sensor token. Replay the same id with changed data and check that you get a conflict. Post an old `captured_at` and check that it is stored as old.
  4. Rehearse rollback. For the sidecar: `sh scripts/cortex_sidecar.sh stop`, with 8001 never stopped. For the executor path only: stop Cortex, restart the old brain with its robot URL on the untouched original DB, and show it serving.
- **Pass:** all four steps behave as stated, and the rollback has been done once, timed.
- **Evidence:** command transcript, HTTP status codes and rollback time.

## CXV8: Witnessed live run (Cortex as the executor)
- **Owner:** Brian at the robot, Carter witnessing
- **Evidences:** EC05, EC06, EC08, EC09
- **Do:** only after CXV2, CXV3, CXV6 and CXV7 pass, and only with all of these in place:
  - a calibrated sensor producer posting to `/observations/sensor` (without it, every live fetch fails at `locate_tag`);
  - verify and clear-stop working from the inspector (CXV6);
  - 8001 restarted WITHOUT its robot URL, so it cannot sign robot commands.

  Then:
  - Test the physical stop first, with witnesses.
  - Start Cortex with `CORTEX_LIVE=1` and `CORTEX_LIVE_ACK=WITNESSED_STOP_AND_SINGLE_EXECUTOR`. Set `CORTEX_CAMERA_URL` here if wanted, after checking Brian's frame route and address (it may be IPv6-only).
  - Run one approved task. Approve blocks until the run ends.
  - Record its outcome with sensor evidence, or as an explicitly labeled human observation.
- **Pass:**
  - the approved task runs exactly once;
  - an unverifiable step is recorded as UNKNOWN and latches stop, with no retry, and clears only after it is verified;
  - STOP works at any point.
- **Evidence:** video, the episode journal rows for that run, and the stop test.

## CXV9: Judge demo rehearsal
- **Owner:** Carter
- **Evidences:** EC16
- **Do:** start from a fresh vault, twice in a row:
  1. Teach the rule.
  2. Ask for a new task and show its recipe and the approval.
  3. Correct the source and show that the same request now plans differently and the old skill is revoked.
  4. Go idle and show a dreamer *candidate*, labeled as a candidate, not a fact.
- **Pass:** both runs match, in under 3 minutes. Say aloud which results were simulated, which were model-generated and which were physically observed.
- **Evidence:** recording of the rehearsal.

## CXV10: Console bridge (blocked)
- **Owner:** Verna and Bharath, after the blockers clear
- **Evidences:** EC04
- **Blocked until:** the console authenticates its own mutating routes (pairing token or a CSRF-protected cookie). Until then `scripts/cortex_console_patch.py --apply` refuses, because the bridge would let any LAN client act as the operator. The other blockers are listed in `docs/CORTEX_HANDOFF.md`: approve blocks for the whole run while the console times out at 60 s, a typed or spoken "stop" leaves the console's own stopped flag false, and PR #5's voice presets call the old engine, so a switch would create two brains.
- **Pass (when unblocked):** an unauthenticated approve from another LAN device gets 401; simulated runs show SIMULATED; STOP, verify and clear-stop work from the console; rollback by unsetting `CORTEX_BRIDGE` is tested.
