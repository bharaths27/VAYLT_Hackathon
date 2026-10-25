from conftest import REQUEST
from engram.heard import heard
from engram.recall import Recall, route


def test_every_command_is_kept_with_what_it_became(engine):
    taught = engine.command("Remember that Sample B needs goggles", who="voice")
    scheduled = engine.command("When the incubation timer goes off, take Sample B to the bench", who="voice")
    proposed = engine.command(REQUEST, who="voice")
    h = {x["observation_id"]: x for x in heard(engine.conn)}
    assert h[taught["heard_id"]]["became"] == "taught"
    assert h[taught["heard_id"]]["source_id"] == taught["source_id"]
    assert h[scheduled["heard_id"]]["task_id"] == scheduled["task_id"]
    assert h[proposed["heard_id"]]["operation_id"] == proposed["operation_id"]
    assert {x["who"] for x in h.values()} == {"voice"}


def test_heard_is_filtered_by_speaker(engine):
    engine.command(REQUEST, who="voice")
    engine.command(REQUEST, who="ipad")
    assert [x["who"] for x in heard(engine.conn, who="ipad")] == ["ipad"]


def test_recall_answers_what_was_said(engine):
    engine.command("Remember that Sample B needs goggles", who="voice")
    engine.command("Bring the beaker to the bench", who="voice")
    assert route("What did I say about Sample B?") == ["heard"]
    r = Recall(engine.memory).recall("What did I say about Sample B?")
    assert [a["text"] for a in r["answers"]] == ['voice said "Remember that Sample B needs goggles" -> taught']


def test_older_matching_utterance_is_still_found(engine):
    engine.command("Remember that Sample B needs goggles", who="voice")
    for _ in range(12):
        engine.command("Bring the beaker to the bench", who="voice")
    texts = [a["text"] for a in Recall(engine.memory).recall("What did I say about Sample B?")["answers"]]
    assert texts == ['voice said "Remember that Sample B needs goggles" -> taught']


def test_unsafe_request_is_heard_whatever_the_planner_does(engine):
    r = engine.command("Dump Sample B in the trash", who="voice")
    (h,) = heard(engine.conn)
    assert h["observation_id"] == r["heard_id"] and h["text"] == "Dump Sample B in the trash"
