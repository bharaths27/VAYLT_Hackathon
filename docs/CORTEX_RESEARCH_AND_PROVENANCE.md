# Sources, implementation basis and verification limits

Source-derived principles are separated from implementation choices.

* User's seven-memory taxonomy and ENGRAM/VAYLT contracts: working, semantic, episodic, procedural, external, parametric and prospective responsibilities; observed sources versus revisable interpretations; explicit authority and outcomes.
* Original ENGRAM Cortex V2 tickets, V3 no-LiDAR/team amendment, and Senti room `55b4f40e-e6b9-4c68-94df-40bcd321e667`. These are desired contracts, not evidence that physical/software behavior has shipped.
* Original upstream release `9849350c9cebd44fd226d9eb76e1460ef395190d` and Alex integration `595f3394277e4435ce8636eb959f619203a801e7`; source read through GitHub connector. Existing schema/adapter/console contracts informed additive integration. No full clone or original complete test suite was available in this execution environment.
* Absolute Zero: https://arxiv.org/abs/2505.03335 — Zhao et al., revised 16 October 2025. The paper uses reinforcement learning with proposer/solver and executable verification. Our memory/procedure practice loop is an adaptation, not its training implementation or an equivalent result.
* TypeSafe System One: https://docs.typesafe.ai/api/system-one — official typed question/answer API, bearer authentication and response fields. https://docs.typesafe.ai/models and https://docs.typesafe.ai/confidence — model identity and score semantics. Model id read from TYPESAFE_INTENT_MODEL, not pinned in code; no live results measured here.
* llama.cpp server: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md — /apply-template, /tokenize, /completion and schema-constrained output. Exact installed Pi version must support the calls; probe fails visibly otherwise.

Design choices of this implementation: a bounded symbolic compiler/verifier, SQLite lifecycle metadata, task recipes, incremental idle worker, explicit startup auto-publication policy, strict local endpoint allowlisting, freshest-frame buffer and separate sensor credentials. These choices are not attributed as results of the above paper.

No key, robot secret, model weights or private original vault is included. The container is x86_64; host execution timings are not Pi latency measurements. Tests using a loopback HTTP stub establish request wiring, not model intelligence.
