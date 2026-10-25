"""The console's bridge to the brain (EC08): memory items the brain sends as strings, and how long it waits."""
import asyncio

import httpx

from server import brain, config


def test_constraints_sent_as_strings_do_not_crash_the_plan_card():
    # The brain sends constraints as plain strings (always after a failed step); .get on one crashed every plan.
    mem = {"goal": "Bring Sample B to the bench",
           "facts": [{"chunk_id": "c1", "statement": "Goggles must be at the bench first."}, "a plain fact"],
           "evidence": ["SOP 004 line 9"],
           "objects": [{"id": "sample_b", "name": "Sample B", "tag": 2}],
           "constraints": ["Do not retry pick_tagged_object for sample_b: E_GRASP_FAILED"],
           "missing": []}
    b = brain.to_bundle(mem)
    assert [c["text"] for c in b["constraints"]] == ["Do not retry pick_tagged_object for sample_b: E_GRASP_FAILED"]
    assert {f["text"] for f in b["facts"]} >= {"Goggles must be at the bench first.", "a plain fact", "SOP 004 line 9"}
    assert b["bindings"][0]["name"] == "Sample B"


def test_plan_and_run_wait_longer_than_quick_calls(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={"ok": True, "kind": "proposal", "outcome": "success", "steps": []})

    monkeypatch.setattr(brain, "URL", "http://brain.test")
    monkeypatch.setattr(brain, "_c", httpx.AsyncClient(timeout=20.0, transport=httpx.MockTransport(handler)))
    asyncio.run(brain.command("Bring Sample B to the bench"))
    asyncio.run(brain.approve("op1", "digest1"))
    asyncio.run(brain.stop())
    assert seen["/commands"] == config.BRAIN_PLAN_TIMEOUT_S == 90.0
    assert seen["/operations/op1/approve"] == config.BRAIN_RUN_TIMEOUT_S == 600.0
    assert seen["/stop"] == 20.0  # a stop must never wait on a long timeout
