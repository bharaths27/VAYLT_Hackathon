"""Pins the server contract the console's push-to-talk relies on.

console/web/js/voice.js (VOICE.voiceCommand) asks POST /stt for a transcript
only (auto=0) and does NOT track that request for speech attribution: server
speech lines carry no turn id, and tracking a request that causes no speech
would only drop the next turn's reply. That is safe only while /stt with
auto=0 broadcasts nothing on the event bus. If this test fails, /stt now
emits events with auto=0; track the /stt request with its turn in voice.js.
"""

from fastapi.testclient import TestClient

from server import main


def _post_stt(monkeypatch, auto, transcript):
    events = []

    async def fake_transcribe(data, suffix, **kw):  # kw: the server may pass its queue reservation
        return (transcript, "ok") if transcript else (None, "whisper failed")

    async def spy_emit(type_, **data):
        events.append(type_)
        return {"type": type_, **data}

    monkeypatch.setattr(main.voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(main.bus, "emit", spy_emit)  # engine shares this bus object
    # Through HTTP, without entering the client (so no app lifespan and no background loop emits):
    # this works whatever the handler's Python signature is.
    r = TestClient(main.app).post("/stt", files={"audio": ("speech.m4a", bytes(2000), "audio/mp4")}, data={"auto": auto})
    return r, events


def test_stt_transcript_only_broadcasts_nothing(monkeypatch):
    r, events = _post_stt(monkeypatch, "0", "drive forward 20 cm")
    assert r.status_code == 200 and r.json() == {"ok": True, "text": "drive forward 20 cm"}
    assert events == []


def test_stt_transcript_only_failure_broadcasts_nothing(monkeypatch):
    r, events = _post_stt(monkeypatch, "0", None)
    assert r.status_code == 503
    assert events == []


def test_spy_sees_events_when_stt_runs_the_command(monkeypatch):
    # Control: with auto=1 the same harness does record the command's events,
    # so the two tests above can fail.
    r, events = _post_stt(monkeypatch, "1", "drive forward 20 cm")
    assert r.json()["ok"] is True
    assert "heard" in events
