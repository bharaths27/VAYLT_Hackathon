import copy,json
from pathlib import Path
import pytest
from engram.cortex.seeding import load_seed
from engram.cortex.types import *
SEED=json.loads((Path(__file__).resolve().parents[2]/'fixtures/cortex/memory_seed.json').read_text())
def test_seed_creates_named_task_and_cited_prerequisite(brain):
    result=load_seed(brain.store,SEED,principal='carther')
    p=brain.command('prepare the inspection',principal='carther')
    assert p['status']=='proposed' and p['derived_memory_id']
    assert 'ppe_station' in [s['args'].get('zone') for s in p['plan']['steps']]
    assert set(result['source_ids'])=={'lab-sop','named-task','operator-preference'}
def test_repeat_seed_does_not_restore_superseded_rule(brain):
    r=load_seed(brain.store,SEED,principal='carther')
    brain.store.teach('Current correction','Safety goggles must be at shelf B before handling Sample B.',owner='carther',supersedes=r['source_ids']['lab-sop'])
    count=brain.store.one('SELECT COUNT(*) n FROM source')['n']
    assert load_seed(brain.store,SEED,principal='carther')['reused']
    assert brain.store.one('SELECT COUNT(*) n FROM source')['n']==count
    assert brain.store.rules()[0]['required_zone']=='shelf_b'
def test_seed_revision_and_effect_injection_refused(store):
    load_seed(store,SEED,principal='carther');bad=copy.deepcopy(SEED);bad['version']='999'
    with pytest.raises(Conflict):load_seed(store,bad,principal='carther')
    bad=copy.deepcopy(SEED);bad['id']='other';bad['sources'][0]['approved_by']='model'
    with pytest.raises(CortexError):load_seed(store,bad,principal='carther')
def test_synthetic_seed_cannot_be_used_as_live_evidence(store):
    with pytest.raises(Denied):load_seed(store,SEED,principal='carther',runtime_mode='PHYSICAL')
