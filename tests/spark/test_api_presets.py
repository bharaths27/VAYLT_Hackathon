"""The HTTP API (the brain on port 8001) registers the voice presets at startup, behind ENGRAM_VOICE_PRESETS."""

import pytest
from fastapi.testclient import TestClient

from engram.api import create_app

OBSERVED_ONLY = [("observe", {})]   # the fixture's overhead scan


def command(client, text):
    return client.post("/commands", json={"text": text, "who": "voice"}).json()


@pytest.mark.parametrize("flag", [None, "1", "true", "on"], ids=["unset", "1", "true", "on"])
def test_presets_become_proposals_that_wait_for_approval(engine, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("ENGRAM_VOICE_PRESETS", raising=False)
    else:
        monkeypatch.setenv("ENGRAM_VOICE_PRESETS", flag)
    client = TestClient(create_app(engine))
    for text, op in [("Open the gripper", "gripper_open"), ("Close the gripper", "gripper_close"),
                     ("Look around", "observe")]:
        out = command(client, text)
        assert out["kind"] == "proposal" and out["status"] == "proposed", out
        assert out["plan"]["steps"] == [{"op": op, "args": {}}] and out["check"]["ok"]
    assert engine.robot.calls == OBSERVED_ONLY                          # proposed, not executed
    assert engine.conn.execute("SELECT COUNT(*) FROM approval").fetchone()[0] == 0


def test_a_preset_runs_only_after_a_person_approves_its_digest(engine, monkeypatch):
    monkeypatch.delenv("ENGRAM_VOICE_PRESETS", raising=False)
    client = TestClient(create_app(engine))
    out = command(client, "Close the gripper")
    bad = client.post(f"/operations/{out['operation_id']}/approve", json={"digest": "0" * 64, "approved_by": "carter"})
    assert bad.status_code >= 400 and engine.robot.calls == OBSERVED_ONLY
    ok = client.post(f"/operations/{out['operation_id']}/approve",
                     json={"digest": out["digest"], "approved_by": "carter"}).json()
    assert ok["outcome"] == "success" and engine.robot.calls[-1] == ("gripper_close", {})


def test_negated_preset_wording_is_still_refused(engine, monkeypatch):
    monkeypatch.delenv("ENGRAM_VOICE_PRESETS", raising=False)
    client = TestClient(create_app(engine))
    assert command(client, "Don't open the gripper")["status"] == "refused"
    assert engine.robot.calls == OBSERVED_ONLY


@pytest.mark.parametrize("flag", ["0", "false", "off", "no"])
def test_flag_off_refuses_presets_as_before(engine, monkeypatch, flag):
    monkeypatch.setenv("ENGRAM_VOICE_PRESETS", flag)
    client = TestClient(create_app(engine))
    out = command(client, "Open the gripper")
    assert out["kind"] == "proposal" and out["status"] == "refused"
    assert engine.robot.calls == OBSERVED_ONLY
