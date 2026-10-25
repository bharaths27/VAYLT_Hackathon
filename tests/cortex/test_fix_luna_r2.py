"""Regression tests for luna's round-2 review of PR 7 at d22c851 (CORE and LEARN; each regression fails on d22c851)."""
import pytest
from engram.cortex.integration import GuardedUpstream
from engram.cortex.learning import Skills
from engram.cortex.planning import Compiler,parse_goal,verify
from engram.cortex.runtime import Brain
from engram.cortex.seeding import load_seed,synthetic_seed_present
from engram.cortex.simulation import FakeRobot,Result
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_seed import SEED

RULE='Safety goggles must be at the ppe station before handling Sample B.'
def approve(b,p): return b.approve(p['operation_id'],p['digest'],principal='carther')
def move(oid,zone): return [{'op':'locate_tag','args':{'object_id':oid}},{'op':'pick_tagged_object','args':{'object_id':oid}},{'op':'place_at_zone','args':{'zone':zone}}]
class PhysicalLike(FakeRobot):
    def state(self): return {**super().state(),'adapter':'test-physical'}


# CORE P1: the safety-prose scan runs whatever `rules` says; only reviewer-structured entities clear it.
def test_safety_prose_is_scanned_even_when_structured_rules_are_supplied(store):
    x=store.teach('Lab note','Sample B must never be handled.',owner='carther',rules=[])
    assert x['unresolved_rules']
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',('sample_b',),'shelf_b'))

def test_safety_sentence_clears_only_for_what_the_reviewer_structured(store):
    store.teach('Lab note','Sample B must never be handled.',owner='carther',rules=[],prohibitions=['sample_b'])
    with pytest.raises(CortexError,match='prohibits'): Compiler(store).compile(Goal('fetch',('sample_b',),'shelf_b'))   # the structured form
    store.teach('Other note','Sample A must never be handled.',owner='carther',rules=[],prohibitions=['sample_b'])
    assert store.unresolved_for({'sample_a'})                                     # structuring Sample B does not clear Sample A


# CORE P1: no physical handling without state progress.
def test_redundant_handling_of_an_object_already_in_place_is_rejected(store):
    store.teach('Lab rule',RULE,owner='carther'); store.observe('goggles','ppe_station',origin='camera',mode='OPERATOR')
    g=Goal('fetch',('sample_b',),'bench'); c=Compiler(store); f=c.fixture(symbolic=True)
    v=verify(g,move('goggles','ppe_station')+move('sample_b','bench'),f,store.related_rules(g),store.policy)
    assert not v.ok and 'no progress' in ' '.join(v.errors)
    assert [x['args']['object_id'] for x in c.compile(g,f)['steps'] if x['op']=='pick_tagged_object']==['sample_b']
    store.observe('sample_a','bench',origin='camera',mode='OPERATOR')            # a target already at its destination
    assert not verify(Goal('fetch',('sample_a',),'bench'),move('sample_a','bench'),c.fixture(symbolic=True),[],store.policy).ok

def test_stored_skill_that_would_re_handle_an_object_in_place_is_not_reused(arena,clock):
    s=Store(clock=clock,policy=Policy(auto_publish_verified=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    s.teach('Lab rule',RULE,owner='carther')
    p=b.command('Bring Sample B to the bench.',principal='carther'); assert approve(b,p)['status']=='success'
    b.robot.positions['sample_b']='home'; s.observe('sample_b','home',origin='test',mode='SIMULATED')   # goggles stay at the ppe station
    again=b.command('Bring Sample B to the bench.',principal='carther')
    assert again['source']!='skill' and [x['args']['object_id'] for x in again['plan']['steps'] if x['op']=='pick_tagged_object']==['sample_b']
    assert approve(b,again)['status']=='success'
    b.close()


# CORE P1: opening the claw needs a reported-empty claw, at proposal and again at approval.
def test_open_claw_is_refused_when_the_held_object_is_unknown(arena,clock):
    class NoHoldReport(FakeRobot):
        def state(self): st=super().state(); st.pop('holding'); return st
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,NoHoldReport(arena))
    with pytest.raises(Conflict,match='may be holding'): b.command('Open the claw',principal='carther')
    b.close()

def test_open_claw_is_refused_when_loaded_even_after_proposal(brain):
    brain.store.teach('Restriction','Never move Sample B.',owner='carther')
    brain.robot.holding='sample_b'                                                 # the adapter reports Sample B in the claw
    with pytest.raises(Conflict,match='holding sample_b'): brain.command('Open the claw',principal='carther')
    with pytest.raises(Conflict,match='already holds sample_b'): brain.command('Bring Sample A to the bench.',principal='carther')
    brain.robot.holding=None
    p=brain.command('Open the claw',principal='carther'); assert p['status']=='proposed'   # control: reported empty
    brain.robot.holding='sample_b'                                                 # loaded between proposal and approval
    with pytest.raises(Denied): approve(brain,p)
    assert ('gripper_open',{}) not in brain.robot.calls


# CORE P1: a STOP landing between the loop-top check and dispatch never dispatches the step.
def test_stop_arriving_just_before_dispatch_never_dispatches(brain):
    p=brain.command('Go to the shelf b',principal='carther'); journal=brain.journal
    def stop_in_the_gap(ep,op,args,status,detail='',step=0):
        journal(ep,op,args,status,detail,step)
        if status=='pending': brain.stop()                                         # the concurrent STOP, deterministically
    brain.journal=stop_in_the_gap
    assert approve(brain,p)['status']=='aborted'
    assert not [c for c in brain.robot.calls if c[0]=='go_to_zone']

def test_adapter_refuses_motion_after_stop_until_reset(store):
    class Upstream:
        def __init__(self): self.calls=[]
        def execute(self,op,args): self.calls.append(op); return Result('done')
        def stop(self): pass
        def reset_stop(self): pass
        def state(self): return {}
    up=Upstream(); g=GuardedUpstream(up,store); g.stop()
    assert g.execute('go_to_zone',{'zone':'bench'}).status=='rejected' and not up.calls
    g.reset_stop(); assert g.execute('go_to_zone',{'zone':'bench'}).status=='done' and up.calls==['go_to_zone']


# CORE P2: recall is scoped to the asking principal's own episodes and commitments.
def test_recall_shows_only_the_asking_principal_s_records(brain):
    brain.schedule('the tray arrives','bring Sample A to the bench',principal='alice')
    brain.command('Bring Sample B to the bench.',principal='alice')
    bob=brain.command('What is pending?',principal='bob')
    assert not bob['memories']['prospective'] and not bob['memories']['episodic'] and 'Sample A' not in bob['reply']
    alice=brain.command('What is pending?',principal='alice')
    assert 'bring Sample A to the bench' in alice['reply'] and alice['memories']['episodic']


# CORE P2: question detection precedes the observe phrases.
def test_what_do_you_see_is_a_question_not_an_observe_proposal(brain):
    with pytest.raises(Missing,match='question'): parse_goal('What do you see?',brain.store)
    assert brain.command('What do you see?',principal='carther')['kind']=='answer'
    assert brain.command('Look around',principal='carther')['plan']['steps'][0]['op']=='observe'


# LEARN P2: the vault's actual runtime decides; a store a PHYSICAL brain runs on is never seeded.
def test_seed_is_refused_on_a_store_a_physical_brain_runs_on(arena,clock):
    s=Store(policy=Policy(live_enabled=True),clock=clock);s.register_arena(arena);b=Brain(s,PhysicalLike(arena))
    with pytest.raises(Denied,match='PHYSICAL runtime'): load_seed(s,SEED,principal='reviewer')
    assert not synthetic_seed_present(s)
    b.close()


# LEARN P2 as stated is not reproducible (an observe step is not allowed for a goto goal, whatever at_zone says);
# the related guarantee kept here: skill-test variants always need the procedure, never pre-satisfy it.
def test_goto_observe_only_plan_never_verifies_even_already_there(store):
    f={**Compiler(store).fixture(symbolic=True),'at_zone':'bench'}
    assert not verify(Goal('goto',zone='bench'),[{'op':'observe','args':{}}],f,[],store.policy).ok

def test_skill_test_variants_start_handled_objects_away_from_their_destination(store):
    g=Goal('fetch',('goggles',),'bench'); c=Compiler(store); f=c.fixture(symbolic=True)      # goggles sort first: a plain shift puts them AT the bench
    plan=c.compile(g,f); plan['refs']=['episode:anchor']
    with store.tx(): store.conn.execute("INSERT INTO episode(id,goal,request,outcome,started_at) VALUES('anchor','{}','x','proposed',1)")
    k=Skills(store); sk=k.create(plan)
    assert k.test(sk['id'],f)['ok']
