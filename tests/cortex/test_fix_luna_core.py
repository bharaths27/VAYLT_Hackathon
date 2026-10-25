"""Regression tests for the luna CORE review of PR 7 (each fails on 3cd807c)."""
import os
import pytest
from engram.cortex.planning import Compiler
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *

class PhysicalLike(FakeRobot):
    def state(self): return {**super().state(),'adapter':'test-physical'}

def approve(b,p): return b.approve(p['operation_id'],p['digest'],principal='carther')

def physical(arena,clock,path):     # a physical runtime needs a durable vault
    s=Store(path,policy=Policy(live_enabled=True),clock=clock);s.register_arena(arena)
    return Brain(s,PhysicalLike(arena))


# P1 runtime: a dispatched motion step that then failed is an unverified partial effect, not a settled failure.
def test_partial_physical_effect_blocks_rearm_until_reconciled(tmp_path,arena,clock):
    b=physical(arena,clock,tmp_path/'live.db')
    p=b.command('Go to the shelf b',principal='carther'); b.robot.faults['go_to_zone']='failed'
    r=approve(b,p)
    assert r['status']=='unknown' and 'motion step was dispatched' in r['failure']
    with pytest.raises(Denied,match=p['operation_id']): b.clear_stop(principal='carther')     # was: rearmed, next motion approvable
    assert b.verify_outcome(p['operation_id'],principal='carther',ok=False,note='Test-only: robot seen at home')['status']=='failed'
    b.clear_stop(principal='carther')
    p2=b.command('Open the claw',principal='carther')
    assert approve(b,p2)['status']=='needs_verification'
    b.close()


# P1 runtime: the stop latch and proposal revocation are durable BEFORE the robot call.
def test_stop_latch_survives_a_crash_during_the_robot_call(tmp_path,arena,clock):
    pth=tmp_path/'db'
    def open_brain():
        s=Store(pth,clock=clock);s.register_arena(arena);return Brain(s,FakeRobot(arena))
    b=open_brain(); p=b.command('Go to the shelf b',principal='carther')
    def killed(): raise SystemExit('process killed inside the robot stop call')
    b.robot.stop=killed
    with pytest.raises(SystemExit): b.stop()
    b.close(); b=open_brain()                                          # restart after the crash
    assert b._stopped.is_set() and b.operation(p['operation_id'])['status']=='cancelled'
    with pytest.raises(Conflict): approve(b,p)
    assert not b.robot.calls
    b.close()


# P1 store: a clock behind the capture time never makes an old observation fresh.
def test_backward_clock_makes_observation_not_fresh_and_flags_it(store,clock):
    store.observe('sample_a','bench',origin='camera',mode='OPERATOR')
    clock.advance(-120)                                                # clock rolled back two minutes
    a=next(o for o in store.objects() if o['id']=='sample_a')
    assert not a['fresh'] and a['age_s'] is None and a['clock_behind'] and 'age unknown' in a['statement']
    assert Compiler(store).fixture()['positions']['sample_a'] is None
    clock.advance(130)                                                 # past the capture again: an honest 10 s age
    a=next(o for o in store.objects() if o['id']=='sample_a')
    assert a['fresh'] and a['age_s']==10 and not a['clock_behind']


# P1 store: safety prose outside the two templates is an unresolved constraint, never silently accepted.
@pytest.mark.parametrize('body',['Sample B must never be handled.',
    'After you bring Sample B to the bench, always put the goggles back home.'])
def test_unparsed_safety_statement_blocks_planning_for_what_it_names(brain,body):
    x=brain.store.teach('Lab note',body,owner='carther')
    assert x['unresolved_rules'] and not x['rules']
    with pytest.raises(Missing,match='structured review'): Compiler(brain.store).compile(Goal('fetch',('sample_b',),'shelf_b'))
    with pytest.raises(Missing): brain.command('Bring Sample B to the shelf b.',principal='carther')
    assert Compiler(brain.store).compile(Goal('fetch',('sample_a',),'shelf_b'))['steps']   # an object it does not name stays plannable

def test_person_resolves_unparsed_safety_statement_by_structured_revision(store):
    x=store.teach('Lab note','Sample B must never be handled.',owner='carther')
    store.teach('Lab note (reviewed)','Sample B must never be handled.',owner='carther',supersedes=x['source_id'],rules=[],prohibitions=['sample_b'])
    with pytest.raises(CortexError,match='prohibits'): Compiler(store).compile(Goal('fetch',('sample_b',),'shelf_b'))   # now the structured prohibition
    assert not store.unresolved_for({'sample_b'})

def test_unparsed_safety_statement_naming_a_station_blocks_goals_there(store):
    store.teach('Lab note','The shelf b is unstable and must not be loaded.',owner='carther')
    with pytest.raises(Missing): Compiler(store).compile(Goal('fetch',('sample_a',),'shelf_b'))
    assert Compiler(store).compile(Goal('fetch',('sample_a',),'bench'))['steps']

def test_plain_notes_and_template_rules_add_no_unresolved_constraint(store):
    for body in ('The lunch menu is on the wall.','Safety goggles must be at the ppe station before handling Sample B.',
                 'Never move Sample A.','Sample A and Sample B are stored on shelf b. Goggles are kept at the ppe station.'):
        assert not store.teach('Note',body,owner='carther')['unresolved_rules'],body


# P1 vault lock: an alias of the database contends for the same lock ('dir' is a control: a directory alias
# already reached the same physical lock file; a file symlink did not).
@pytest.mark.parametrize('alias',['file','dir'])
def test_vault_lock_is_shared_through_a_symlink(tmp_path,arena,alias):
    real=tmp_path/'real'; real.mkdir(); target=real/'vault.db'
    owner=Store(target); owner.register_arena(arena)
    try:
        if alias=='file': link=tmp_path/'alias.db'; os.symlink(target,link)
        else: (tmp_path/'dirlink').symlink_to(real,target_is_directory=True); link=tmp_path/'dirlink'/'vault.db'
    except (OSError,NotImplementedError): owner.close(); pytest.skip('symlinks unavailable')
    with pytest.raises(Conflict): Store(link)
    owner.close()
    Store(link).close()                                                # released: the alias may open it now


# P2 recall: search the whole history for the named object before limiting to the newest records.
def test_retrospective_recall_finds_a_record_older_than_the_recent_window(brain,clock):
    p=brain.command('Bring Sample A to the bench.',principal='carther'); approve(brain,p)
    for i in range(9):
        clock.advance(1); brain.command('Go to the shelf b',principal='carther',idempotency_key=f'k{i}')
    r=brain.command('What happened to Sample A last time?',principal='carther')
    assert r['reply'].startswith('Recorded task: Bring Sample A to the bench.') and r['evidence']==['episode:'+p['operation_id']]
    assert brain.command('What happened to the tray last time?',principal='carther')['reply'].startswith('No matching execution record')
