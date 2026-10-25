"""The ENGRAM package stays loadable, small and true to its sources."""

import hashlib
import json

from conftest import ROOT
from engram.capabilities import CAPABILITIES
from engram.practice import VERDICTS, load_cases, run_case, verify

PKG = ROOT / "engram/package"
CASES = ROOT / "tests/fixtures/engram_practice.jsonl"


def test_contract_fits_the_token_budget():
    words = len((PKG / "ENGRAM.md").read_text().split())
    assert words * 4 // 3 <= 800  # rough estimate until measured with the real tokenizer


def test_contract_keeps_the_seven_memory_responsibilities():
    text = (PKG / "ENGRAM.md").read_text()
    for name in ("Working", "Semantic", "Episodic", "Procedural", "External", "Parametric", "Prospective"):
        assert name in text


def test_seed_sources_match_the_files_they_cite():
    seed = json.loads((PKG / "engram_seed.json").read_text())
    for src in seed["sources"]:
        assert hashlib.sha256((ROOT / src["path"]).read_bytes()).hexdigest() == src["sha256"], src["path"]


def test_seed_entities_match_the_arena():
    seed = json.loads((PKG / "engram_seed.json").read_text())
    arena = json.loads((ROOT / "fixtures/lab/arena.json").read_text())
    assert {(o["id"], o["tag"], o["fixture_zone"]) for o in seed["objects"]} == \
           {(o["id"], o["tag"], o["zone"]) for o in arena["objects"]}
    assert {str(z["tag"]): z["id"] for z in seed["zones"]} == arena["zone_tags"]


def test_seed_capabilities_match_the_code():
    caps = json.loads((PKG / "engram_seed.json").read_text())["capabilities"]
    assert set(caps["physical"]) == {n for n, c in CAPABILITIES.items() if c.physical}
    assert set(caps["non_physical"]) == {n for n, c in CAPABILITIES.items() if not c.physical}


def test_seed_rule_keeps_object_and_station():
    for rule in json.loads((PKG / "engram_seed.json").read_text())["rules"]:
        assert rule["requires_object"] and rule["required_station"] and rule["source"]


def test_cases_are_well_formed():
    cases = load_cases(CASES)
    assert len({c["case_id"] for c in cases}) == len(cases)
    families = {c["family"] for c in cases if c.get("kind") != "verifier_check"}
    assert families >= {"source_revision", "paraphrase", "negation_disposal", "future_instruction",
                        "repeated_trigger", "irrelevant_source", "stale_scene", "recall_not_commitment"}
    for c in cases:
        assert c["origin"] and c["expected"]


def test_verifier_accepts_and_rejects_what_it_should():
    for c in load_cases(CASES):
        if c.get("kind") == "verifier_check":
            assert verify(c["expected"], c["actual"])[0] == c["expected_verdict"], c["case_id"]


def test_every_case_runs_on_the_mock_and_is_simulated():
    for c in load_cases(CASES):
        if c.get("kind") != "verifier_check":
            rec = run_case(c)
            assert rec["mode"] == "SIMULATED" and rec["verdict"] in VERDICTS, c["case_id"]
