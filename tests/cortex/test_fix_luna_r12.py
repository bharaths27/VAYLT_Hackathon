"""Regression tests for luna's round-12 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import json,os,threading,time
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.planning import Compiler,goal_ops,validate_step,verify
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_luna_r7 import TA

def approve(b,p,who='carther'): return b.approve(p['operation_id'],p['digest'],principal=who)
def h(token): return {'Authorization':'Bearer '+token}
LATCH="SELECT 1 FROM meta WHERE key='cortex_stop'"


# P1-1 (class C): STOP has its own path (latch, STOP count, revocation of pending proposals); it is never a goal, a task
# alias or a plan step, so nothing can stop the robot past it and leave a pending motion usable.
STOP={'kind':'primitive','primitive':'stop'}
def older_alias(store,name,goal):
    """A task alias as an older version stored it, before task definitions were checked."""
    sid=store.teach('Routine','A routine for the end of the day.',owner='carther')['source_id']
    with store.tx(): store.conn.execute('INSERT INTO cx_goal VALUES(?,?,?,1)',(name,canonical(goal),sid))

def test_a_stop_alias_never_leaves_a_pending_motion_usable_after_a_restart(tmp_path,arena,clock):
    def open_brain(): s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);return Brain(s,FakeRobot(arena))
    b=open_brain();p=b.command('Go to the shelf b',principal='carther');assert p['status']=='proposed'
    with pytest.raises(Denied,match='cannot be the stop control'):                               # was: taught
        b.store.teach('Parking','The last task of the day.',owner='carther',goal_definitions={'park safely':STOP})
    older_alias(b.store,'park safely',STOP)
    with pytest.raises(Denied,match='not a goal'): b.command('park safely',principal='carther')  # was: a stop plan step
    assert not [c for c in b.robot.calls if c[0]=='stop']
    b.stop();b.close();b=open_brain()                                                           # STOP by its own path, a restart
    assert b.operation(p['operation_id'])['status']=='cancelled' and b.store.one(LATCH)
    with pytest.raises(CortexError): approve(b,p)
    b.close()

def test_a_stop_goal_is_refused_from_the_api_and_by_the_compiler(brain):
    with TestClient(create_app(brain,tokens={TA:'alice'},start_idle=False)) as c:
        r=c.post('/goals/propose',json={'goal':STOP,'request':'Park safely'},headers=h(TA))
        assert r.status_code==403 and 'not a goal' in r.json()['detail'],r.json()                 # was: proposed
    with pytest.raises(Denied,match='not a goal'): Compiler(brain.store).compile(Goal.from_dict(STOP))

def test_no_plan_may_carry_a_stop_step_and_the_model_is_never_offered_one(store):
    with pytest.raises(Denied,match='never a plan step'): validate_step({'op':'stop','args':{}},set(),store.zones())   # was: valid
    assert not verify(Goal.from_dict(STOP),[{'op':'stop','args':{}}],Compiler(store).fixture(symbolic=True),[],store.policy).ok
    assert goal_ops(STOP)==() and not any('stop' in goal_ops({'kind':k}) for k in ('fetch','goto','inspect'))           # was: ('stop',)
