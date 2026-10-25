"""Regression tests for luna's round-6 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import json,os
import pytest
from engram.cortex.planning import Compiler
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_verifier import GOOD_B,_model_brain

RULE_TEXT='Safety goggles must be at the ppe station before handling Sample B.'
class PhysicalLike(FakeRobot):
    def state(self): return {**super().state(),'adapter':'test-physical'}
def approve(b,p): return b.approve(p['operation_id'],p['digest'],principal='carther')


# P1-1: an unparsed statement is scoped by its OWN sentence's names; one with none of its own is global.
@pytest.mark.parametrize('body,target',[('Sample A is stored at the bench. Nothing may be moved unless a supervisor approves.','sample_b'),
                                        ('Sample B is in fridge 2. It must never be handled.','sample_a'),
                                        ('The tray is on the bench. Always handle it with care.','sample_b')])
def test_a_statement_with_no_names_of_its_own_is_global(store,body,target):
    store.teach('Note',body,owner='carther')                                          # the target is named nowhere in the document
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',(target,),'shelf_b'))

def test_control_a_statement_naming_its_own_object_stays_scoped_to_it(store):
    store.teach('Note','Sample A is stored at the bench. Sample B must never be handled.',owner='carther')
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',('sample_b',),'shelf_b'))
    assert Compiler(store).compile(Goal('fetch',('sample_a',),'shelf_b'))['steps']


# P1-2: a physical runtime needs a durable vault it holds the exclusive lock of.
def test_physical_mode_refuses_a_vault_that_is_not_durable_and_locked(tmp_path,arena,clock):
    s=Store(policy=Policy(live_enabled=True),clock=clock);s.register_arena(arena)
    with pytest.raises(Denied,match='durable vault'): Brain(s,PhysicalLike(arena))
    s.close()
    u=Store(tmp_path/'u.db',policy=Policy(live_enabled=True),clock=clock,lock=False);u.register_arena(arena)
    with pytest.raises(Denied,match='durable vault'): Brain(u,PhysicalLike(arena),acquire_lock=False)
    u.close()

def test_guard_an_unverified_physical_effect_survives_a_restart(tmp_path,arena,clock):
    def open_brain():
        s=Store(tmp_path/'live.db',policy=Policy(live_enabled=True),clock=clock);s.register_arena(arena);return Brain(s,PhysicalLike(arena))
    b=open_brain();p=b.command('Open the claw',principal='carther');assert approve(b,p)['status']=='needs_verification'
    with pytest.raises(Conflict): Store(tmp_path/'live.db',clock=clock)          # a second owner is refused
    b.close();b=open_brain()                                                       # restart
    assert b.operation(p['operation_id'])['status']=='needs_verification'
    p2=b.command('Close the claw',principal='carther')
    with pytest.raises(Denied,match=p['operation_id']): approve(b,p2)
    b.close()


# P2-3: deleting a fired commitment removes its wake text too (event trigger, wake errors, woken request).
def test_deleting_a_fired_commitment_removes_its_wake_text(tmp_path,arena,clock):
    for o in arena['objects']:
        if o['id']=='sample_a':o['aliases']=['zqxsample']                         # a request word only an alias can carry
    s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    t=b.schedule('the zqxtrigger arrives','bring zqxsample to the bench',principal='carther')
    assert b.trigger('the zqxtrigger arrives',event_id='ev1',principal='carther')[0]['proposal']['status']=='proposed'
    s.revoke_source(s.one('SELECT source_id FROM prospective_task WHERE id=?',(t['task_id'],))['source_id'],delete=True)
    arena_tables={'object_binding','cx_entity','cx_station'}                      # the alias itself is arena data
    for (name,) in [tuple(r.values()) for r in s.rows("SELECT name FROM sqlite_master WHERE type='table'")]:
        if name in arena_tables:continue
        for row in s.rows(f'SELECT * FROM "{name}"'):
            dumped=json.dumps(row,default=str).lower()
            assert 'zqxtrigger' not in dumped and 'zqxsample' not in dumped,(name,row)
    b.close()


# P2-4: one vault file has one name; a hard-linked vault is refused, under either name.
def test_a_hard_linked_vault_is_refused(tmp_path):
    a=tmp_path/'v.db';Store(a).close();alias=tmp_path/'alias.db'
    try:os.link(a,alias)
    except (OSError,NotImplementedError):pytest.skip('hard links unavailable here')
    for p in (alias,a):
        with pytest.raises(Conflict,match='hard link'):Store(p)


# P2-5: prompt episodes are no part of a procedure's identity: repeats reuse one candidate and one recipe.
def test_repeated_model_first_requests_reuse_one_candidate_and_recipe(arena,clock):
    b,calls=_model_brain(arena,clock,GOOD_B,policy=Policy())                       # no auto-publish: the model is asked every time
    src=b.store.teach('Lab rule',RULE_TEXT,owner='op')
    for _ in range(3):
        p=b.command('Bring Sample B to bench',principal='op');assert p['source']=='model'
        b.reject(p['operation_id'],principal='op');clock.advance(1)
    assert sum(1 for url,_ in calls if url.endswith('/completion'))==3
    assert b.store.one('SELECT COUNT(*) n FROM skill_version')['n']==1
    assert b.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='task_recipe'")['n']==1
    k=b.store.one('SELECT definition_json FROM skill_version')['definition_json']
    assert 'source:'+src['source_id'] in k and 'episode:' not in k                # revocation still follows the source
    b.close()
