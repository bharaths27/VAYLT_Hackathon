"""Regression tests for luna's round-11 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import json,threading,time
import pytest
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *

def approve(b,p,who='carther'): return b.approve(p['operation_id'],p['digest'],principal=who)
DRIVE=Goal('primitive',primitive='drive_bounded',args={'distance_m':0.2})


# P1-1: naming nothing, an instruction about the robot's own motion blocks every plan until it is reviewed.
@pytest.mark.parametrize('note',['Never drive.','Do not drive.','The robot must not drive.','Driving is forbidden.','Never navigate.'])
def test_an_unscoped_instruction_about_the_robots_motion_blocks_navigation(brain,note):
    x=brain.store.teach('Note',note,owner='carther')
    assert x['unresolved_rules'] and 'blocks nothing' not in x['unresolved_rules'][0],x       # was: it blocks nothing
    with pytest.raises(Missing,match='structured review'): brain.command('Go to the shelf b',principal='carther')
    with pytest.raises(Missing,match='structured review'): brain.propose(DRIVE,'Drive forward 0.2 m',principal='carther')

def test_an_unscoped_motion_prohibition_taught_after_proposing_stops_the_approval(brain):
    p=brain.command('Go to the shelf b',principal='carther');assert p['status']=='proposed'
    brain.store.teach('Note','Never drive.',owner='carther')
    with pytest.raises(CortexError): approve(brain,p)                                         # was: it drove
    assert not [c for c in brain.robot.calls if c[0]=='go_to_zone']

@pytest.mark.parametrize('note',['Lena drives to work every day.','Lena drives while it rains.','Always use gloves.','Visitors should take the stairs.'])
def test_control_a_remark_or_an_instruction_about_an_unnamed_thing_still_freezes_nothing(brain,note):
    x=brain.store.teach('Note',note,owner='carther')
    assert not x['unresolved_rules'] or 'blocks nothing' in x['unresolved_rules'][0],x
    assert brain.command('Go to the shelf b',principal='carther')['status']=='proposed'


# P1-2 (class A): a revocation never commits between a step's final check and its dispatch; it waits for the dispatched
# step (the next step's check sees it). Barrier-controlled: the adapter holds the effect while a revocation is attempted.
class Holding(FakeRobot):
    """An adapter that holds the first step reaching it, and records what backed the plan when that effect happened."""
    def __init__(self,arena,probe): super().__init__(arena);self.probe=probe;self.entered=threading.Event();self.go=threading.Event();self.backing=[]
    def execute(self,op,args):
        if not self.entered.is_set(): self.entered.set();self.go.wait(5);self.backing.append(self.probe())
        return super().execute(op,args)

def skill_backed_goto(arena,clock):
    s=Store(clock=clock,policy=Policy(auto_publish_verified=True));s.register_arena(arena)
    b=Brain(s,Holding(arena,lambda:None));first=b.command('Go to the shelf b',principal='carther');b.reject(first['operation_id'],principal='carther')
    p=b.command('Go to the shelf b',principal='carther');assert p['source']=='skill' and p['skill_id']
    sid=p['skill_id'];b.robot.probe=lambda:s.one('SELECT status FROM skill_version WHERE id=?',(sid,))['status']
    return b,p,lambda:b.skills.revoke(sid),'APPROVED'

def alias_backed_goto(arena,clock):
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,Holding(arena,lambda:None))
    src=s.teach('Routine','Going to work means the bench.',owner='carther',goal_definitions={'go to work':{'kind':'goto','zone':'bench'}})['source_id']
    p=b.command('go to work',principal='carther');assert 'source:'+src in p['plan']['refs']
    b.robot.probe=lambda:s.one('SELECT status FROM cx_source WHERE source_id=?',(src,))['status']
    return b,p,lambda:s.revoke_source(src),'accepted'

def constraint_taught(arena,clock):
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,Holding(arena,lambda:None));p=b.command('Go to the shelf b',principal='carther')
    b.robot.probe=lambda:'blocked' if s.unresolved_for({'shelf_b'},handles=False) else 'clear'
    return b,p,lambda:s.teach('Note','Do not go to shelf B.',owner='carther'),'clear'

def binding_changed(arena,clock):
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,Holding(arena,lambda:None));p=b.command('Go to the shelf b',principal='carther')
    retagged={**arena,'zone_tags':{('14' if z=='shelf_b' else t):z for t,z in arena['zone_tags'].items()}}
    b.robot.probe=lambda:s.one("SELECT version FROM cx_station WHERE id='shelf_b'")['version']
    return b,p,lambda:s.register_arena(retagged),1

def rule_adjudicated(arena,clock):
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,Holding(arena,lambda:None))
    for title in ('SOP','SOP copy'): s.teach(title,'Safety goggles must be at the ppe station before handling Sample B.',owner='carther')
    p=b.command('Bring Sample B to the bench.',principal='carther');rules=s.rules();assert len(rules)==2 and p['status']=='proposed'
    b.robot.probe=lambda:len(s.rules())
    return b,p,lambda:s.resolve_rule(rules[0]['id'],principal='carther',reason='one copy is enough'),2

@pytest.mark.parametrize('setup',[skill_backed_goto,alias_backed_goto,constraint_taught,binding_changed,rule_adjudicated])
def test_a_revocation_waits_for_the_step_being_dispatched(arena,clock,setup):
    b,p,revoke,intact=setup(arena,clock);done=[]
    runner=threading.Thread(target=lambda:approve(b,p),daemon=True);runner.start()
    try:
        assert b.robot.entered.wait(5)                                                       # the step reached the adapter
        attempting=threading.Event()
        revoker=threading.Thread(target=lambda:(attempting.set(),revoke(),done.append(time.monotonic())),daemon=True);revoker.start()
        assert attempting.wait(5);time.sleep(.3)
        assert revoker.is_alive() and not done                                               # was: it committed meanwhile
    finally:
        released=time.monotonic();b.robot.go.set();runner.join(10)
    revoker.join(10)
    assert b.robot.backing==[intact] and done and done[0]>=released                           # dispatched on intact authority
    b.close()

def test_guard_a_revocation_inside_an_open_transaction_is_refused_never_deadlocked(store):
    src=store.teach('Note','The tray is on the bench.',owner='carther')['source_id']
    with store.tx():
        with pytest.raises(RuntimeError,match='lock order'): store.revoke_source(src)
    assert store.source_state(src)['status']=='accepted'
