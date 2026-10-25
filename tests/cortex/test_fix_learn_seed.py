"""Synthetic seed data is marked and never used by a live (PHYSICAL) runtime (finding 9)."""
import json
from pathlib import Path
import pytest
from engram.cortex.runtime import Brain
from engram.cortex.seeding import load_seed
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *

SEED=json.loads((Path(__file__).resolve().parents[2]/'fixtures/cortex/memory_seed.json').read_text())

def test_seeded_sources_are_marked_synthetic(store):
    r=load_seed(store,SEED,principal='carther')
    kinds={x['kind'] for x in store.rows('SELECT kind FROM source WHERE id IN (%s)'%','.join('?'*len(r['source_ids'])),tuple(r['source_ids'].values()))}
    assert kinds and all(k.startswith('synthetic:') for k in kinds) and r['synthetic'] is True

def test_physical_brain_refuses_a_vault_with_synthetic_seed(arena,clock):
    class PhysicalLike(FakeRobot):
        def state(self): return {**super().state(),'adapter':'test-physical'}
    clean=Store(policy=Policy(live_enabled=True),clock=clock);clean.register_arena(arena)
    Brain(clean,PhysicalLike(arena)).close()                                   # control: unseeded vault starts
    s=Store(policy=Policy(live_enabled=True),clock=clock);s.register_arena(arena);load_seed(s,SEED,principal='reviewer')
    with pytest.raises(Denied,match='separate, unseeded vault'): Brain(s,PhysicalLike(arena))
    for src in load_seed(s,SEED,principal='reviewer')['source_ids'].values(): s.revoke_source(src,delete=True)
    with pytest.raises(Denied): Brain(s,PhysicalLike(arena))                    # still a seeded vault
    Brain(s,FakeRobot(arena)).close()                                          # simulated review still works
