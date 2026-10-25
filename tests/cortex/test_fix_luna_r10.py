"""Regression tests for luna's round-10 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import pytest
from engram.cortex.planning import DeniedPlan
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.types import *

def approve(b,p,who='carther'): return b.approve(p['operation_id'],p['digest'],principal=who)


# P1-1: closing the claw handles what it holds: that object's constraints apply, and unreported contents are refused.
def holding(brain,oid):
    brain.robot.positions.pop(oid);brain.robot.holding=oid;brain.robot.open=False

@pytest.mark.parametrize('note,error',[('Leave Sample B alone.',Missing),('Never move Sample B.',DeniedPlan)])
def test_closing_the_claw_on_a_constrained_held_object_is_refused(brain,note,error):
    holding(brain,'sample_b');brain.store.teach('Note',note,owner='carther')
    with pytest.raises(error): brain.command('Close the claw',principal='carther')          # was: proposed

def test_a_constraint_taught_after_proposing_a_close_still_refuses_it_at_approval(brain):
    holding(brain,'sample_b');p=brain.command('Close the claw',principal='carther');assert p['status']=='proposed'
    brain.store.teach('Note','Leave Sample B alone.',owner='carther')
    with pytest.raises(Denied,match='structured review'): approve(brain,p)                  # was: closed on it
    assert not [c for c in brain.robot.calls if c[0]=='gripper_close']

def test_control_closing_an_empty_claw_is_not_held_to_another_objects_constraint(brain):
    brain.store.teach('Note','Leave Sample B alone.',owner='carther')
    assert approve(brain,brain.command('Close the claw',principal='carther'))['status']=='success'

class NoHoldingReport(FakeRobot):
    def state(self): s=super().state();s.pop('holding');return s

def test_closing_a_claw_whose_contents_are_unreported_is_refused(store,arena):
    b=Brain(store,NoHoldingReport(arena))
    with pytest.raises(Conflict,match='contents are unknown'): b.command('Close the claw',principal='carther')   # was: proposed


# P1-2: a condition of time or circumstance on motion or handling is unresolved: it blocks the unconditional plan.
@pytest.mark.parametrize('note,command',[('Go to shelf B while the floor is wet.','Go to the shelf b'),
                                         ('When the tray arrives, bring Sample A to the bench.','Bring Sample A to the bench.'),
                                         ('Take Sample B to the bench during the night shift.','Bring Sample B to the bench.')])
def test_a_condition_on_motion_or_handling_blocks_the_unconditional_plan(brain,note,command):
    assert brain.store.teach('Note',note,owner='carther')['unresolved_rules']
    with pytest.raises(Missing,match='structured review'): brain.command(command,principal='carther')   # was: proposed

def test_a_condition_taught_after_proposing_stops_its_approval(brain):
    p=brain.command('Go to the shelf b',principal='carther');assert p['status']=='proposed'
    brain.store.teach('Note','Go to shelf B while the floor is wet.',owner='carther')
    with pytest.raises(CortexError): approve(brain,p)
    assert not [c for c in brain.robot.calls if c[0]=='go_to_zone']

@pytest.mark.parametrize('note',['Safety goggles must be at the ppe station before handling Sample B.','Lena arrives when the lab opens.'])
def test_control_a_stored_rule_and_a_remark_add_no_condition(store,note):
    assert not store.teach('Note',note,owner='carther')['unresolved_rules']


# P2-3, P1-4 and the privacy class: a procedure built on one operator's commitment or episode is theirs alone. It is not
# listed in another's bundle or recall (nor with no principal), and never selected for another's command.
from engram.cortex.store import Store

@pytest.fixture
def published(arena,clock):
    s=Store(clock=clock,policy=Policy(auto_publish_verified=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    yield b
    b.close()

def commitment_skill(b):
    """Alice's commitment wakes into a proposal; with auto-publish its procedure is approved, citing her commitment."""
    b.schedule('the tray arrives','bring Sample A to the bench',principal='alice')
    assert b.trigger('the tray arrives',event_id='ev1',principal='alice')[0]['proposal']['status']=='proposed'
    fsid=b.store.one("SELECT id FROM source WHERE kind='future'")['id']
    skill=next(s for s in b.skills.list() if 'source:'+fsid in s['definition']['refs'])
    assert skill['status']=='APPROVED'
    return skill,fsid

def test_a_procedure_built_on_a_commitment_stays_out_of_others_bundles_and_recall(published):
    skill,_=commitment_skill(published);s=published.store;goal=Goal('fetch',('sample_a',),'bench')
    listed=lambda who:[x['id'] for x in s.bundle('Bring Sample A to the bench',goal,principal=who)['applicable_skills']]
    assert skill['id'] in listed('alice') and skill['id'] not in listed('bob')                  # was: listed to bob
    recalled=lambda who:[x['id'] for x in s.recall('what do you remember',['procedural'],principal=who)['memories']['procedural']]
    assert skill['id'] in recalled('alice') and skill['id'] not in recalled('bob') and skill['id'] not in recalled(None)

def test_another_operators_command_never_selects_a_procedure_built_on_her_commitment(published):
    skill,fsid=commitment_skill(published)
    p=published.command('Bring Sample A to the bench.',principal='bob')
    assert p['status']=='proposed' and p['skill_id']!=skill['id'] and fsid not in str(p['plan'])   # was: her procedure
    assert published.command('Bring Sample A to the bench.',principal='alice')['skill_id']==skill['id']   # hers to reuse

def test_no_plan_cites_another_operators_episode(brain):
    a=brain.command('Go to the shelf b',principal='alice');b=brain.command('Go to the shelf b',principal='bob')
    assert 'episode:'+a['operation_id'] in a['plan']['refs'] and 'episode:'+b['operation_id'] in b['plan']['refs']
    assert 'episode:'+a['operation_id'] not in b['plan']['refs']                               # was: her first request

def test_a_commitment_never_becomes_a_shared_source_by_adoption(brain):
    brain.schedule('the tray arrives','bring Sample A to the bench',principal='alice')
    fsid=brain.store.one("SELECT id FROM source WHERE kind='future'")['id']
    with pytest.raises(Conflict,match='not a legacy source'): brain.store.adopt_legacy_source(fsid,owner='bob')
