# EC03/07/09/10/11/12/20 — combined Cortex implementation review

## Intent
A supported new task retrieves related current evidence, produces and stores a derived procedure, validates it against the goal, and can reuse an approved skill. Idle work proposes evidence-linked memories and practices without physical effects. Qwen actually receives the contract and memory context.

## Problem
The submitted release lost required stations, lacked durable future-task cancellation and did not insert ENGRAM.md into the model prompt. A successful JSON parse or actuator ACK was insufficient evidence of goal completion. The earlier speech delivery did not implement this memory combination.

## Covers
Additive `engram/cortex` runtime, source-backed tests, synthetic demonstration, Qwen/intent-model clients, scoped sensor/camera interfaces, opt-in console bridge and migration/review documentation. No changes to Warden's speech patch, the submitted branch or the robot's motor firmware. No unrestricted generated code or RL model training.

## Readiness
READY FOR SOFTWARE REVIEW AND MOCK INTEGRATION. Local test results are in the evidence pack. BLOCKED for deployment/physical acceptance until an authorized maintainer applies it, runs the original suite and completes Pi/model/console/sensor checks.

## What a top professional would add
Challenge the verifier with plausible wrong plans; test source correction and withdrawal, old observations, negated/future/retrospective requests, concurrent approval, crash/uncertain effects, and replayed events. Keep exact source/model/policy versions and separate symbolic validation from physical competence. Perform independent security and exact-head review rather than counting tests as proof of production readiness.

## Raw material
Existing release 9849350, Alex integration595f339, original SQLite/adapter/console contracts, seven-memory user brief, V2/V3 acceptance contracts, real source/rule changes, actual sensor frames/capture times and the actual Pi inference responses. No earlier narrative is hardware evidence.

## Methods and tools
SQLite WAL and transactions, FTS/exact retrieval, bounded prerequisite graph, typed goals, a separate state-transition verifier, provenance/digests, a no-motor fake adapter, an incremental idle worker, strict HTTP model clients, pytest/FastAPI/httpx, separate-process restart demonstration, newest-frame fanout. Real Qwen/intent-model probes run separately and retain failures.

## Proposed solution
Apply the additive patch on a review branch. Demonstrate locally. Review the source/adoption contract using a SQLite backup. Merge Alex's answer handling independently. Select one active backend, pair the UI, connect a real calibrated sensor producer and verify hardware stopping before a deliberate cutover. Preserve the old release for rollback.

## What done looks like
The exact received patch passes its suite and the full original repository suite, the seven-stage demo passes, the Qwen probe logs actual prompt and contract identity with correct goal results, live intent-model advice is either tested and labeled cloud-assisted or explicitly disabled, and the actual Pi/browser/controller acceptance record exists. No blanket DONE for unrelated hardware/research tickets.

## Independent proof
Warden/Bharath inspect the exact patch, Alex or another non-author runs fresh cases, Brian witnesses hardware and calibration, Verna witnesses the actual iPad. Existing local tests are supporting evidence, not substitutes for these reviewers.

## Who is suited
ChatGPT authored the additive code for Carther. Bharath owns canonical review/application/cutover; Alex owns independent memory cases; Verna owns legacy-console compatibility; Brian owns physical evidence. Warden coordinates without duplicating the memory implementation.

## Prompt
Review `docs/CORTEX_HANDOFF.md` and this contract. Verify archive and per-file hashes. Inspect/apply only on an isolated review branch with `git apply --check`. Run `python -m pytest -q tests/cortex`, `python scripts/cortex_demo.py`, then the original full suite. Run the actual no-motor Qwen/intent-model probes on the Pi. Report exact commit, commands, observed results, unverified limits and conflicts. Do not deploy, command motors, overwrite the active DB or mark hardware cards complete merely because local tests pass.

## Priority
P0 core memory lifecycle and proposal correctness; actual model/cloud/physical acceptance remains a separate gate.

## Source
User's Spark build request; Senti memory reservation #53459; inspected release/Alex commits; primary protocols and paper listed in CORTEX_RESEARCH_AND_PROVENANCE.md. This implementation is a bounded engineering adaptation, not a reproduction of the paper's training system.
