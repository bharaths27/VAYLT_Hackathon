"""Tests for how the voice client describes the server's replies."""

from perception.voice import describe_reply


def test_reply_descriptions():
    screen, _ = describe_reply({"kind": "proposal", "status": "refused", "reason": "No."})
    assert screen.startswith("NOT PLANNED")
    screen, spoken = describe_reply({"kind": "proposal", "status": "proposed", "operation_id": "ep_1",
                                     "check": {"ok": True, "errors": []}, "source": "rules", "planner_calls": 1,
                                     "plan": {"steps": [{"op": "locate_tag", "args": {"object_id": "beaker"}},
                                                        {"op": "speak", "args": {"text": "hi"}}]}})
    assert "locate_tag(beaker)" in screen and "speak" not in screen and "approve" in spoken
    screen, _ = describe_reply({"kind": "scheduled", "trigger": "timer ends", "request": "x", "created": True})
    assert screen.startswith("SCHEDULED")


def test_a_clarification_is_said_as_the_reply():
    screen, spoken = describe_reply({"kind": "clarify", "reason": "I didn't catch that.", "stopped": True,
                                     "reply": "I stopped. I didn't catch that. Please clarify."})
    assert screen.startswith("STOPPED; PLEASE CLARIFY") and spoken == "I stopped. I didn't catch that. Please clarify."
    screen, spoken = describe_reply({"kind": "proposal", "status": "refused", "needs_clarification": True,
                                     "reason": "I don't know which object you mean.", "stopped": False,
                                     "reply": "I don't know which object you mean. Please clarify."})
    assert screen.startswith("PLEASE CLARIFY") and spoken.endswith("Please clarify.")


def test_the_voice_loop_sends_a_clip_with_nothing_in_it(monkeypatch):
    """A recording whose transcript is empty is sent as it is, so the server can apply Carter's rule."""
    from perception import voice

    sent, prompts = [], iter(["", KeyboardInterrupt])

    def fake_input(prompt=""):
        nxt = next(prompts)
        if nxt is KeyboardInterrupt:
            raise KeyboardInterrupt
        return nxt

    class Silent:
        def __init__(self, *a):
            pass

        def transcribe(self, audio):
            return ""                                           # heard nothing that whisper could write down

    monkeypatch.setattr("sys.argv", ["voice", "--server", "http://server.test", "--no-pointing"])
    monkeypatch.setattr(voice.urllib.request, "urlopen", lambda *a, **k: type("R", (), {"read": lambda self: b"{}"})())
    monkeypatch.setattr(voice, "Transcriber", Silent)
    monkeypatch.setattr(voice, "vocabulary_hint", lambda: "")
    monkeypatch.setattr(voice, "record_until_enter", lambda max_seconds: [0] * voice.SAMPLE_RATE)   # 1 s of audio
    monkeypatch.setattr(voice, "send_command", lambda server, text, who: sent.append(text) or
                        {"kind": "clarify", "reply": "I didn't catch that. Please clarify."})
    monkeypatch.setattr("builtins.input", fake_input)
    voice.main()
    assert sent == [""]
