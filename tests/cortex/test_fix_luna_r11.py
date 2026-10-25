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


# P2-3 (class A): a STOP revokes a proposal whose request began before it, even one with no episode yet when the STOP's
# scan ran; and a proposal is published only under the checks it passed, run in the publishing transaction.
def test_a_stop_before_the_command_created_its_episode_revokes_its_proposal(brain):
    entered,go=threading.Event(),threading.Event();route=brain.intent.route;out={}
    def paused(*a,**k): entered.set();go.wait(5);return route(*a,**k)
    brain.intent.route=paused                                                                # in flight, before its episode
    t=threading.Thread(target=lambda:out.update(p=brain.command('Go to the shelf b',principal='carther')),daemon=True);t.start()
    try:
        assert entered.wait(5) and not brain.store.one('SELECT 1 FROM episode')
        brain.stop()
    finally: go.set();t.join(10)
    p=out['p'];assert p['status']=='cancelled' and p['failure']=='stop revoked pending proposal',p   # was: proposed
    brain.clear_stop(principal='carther')
    with pytest.raises(Conflict): approve(brain,p)                                           # was: approved after the clear
    assert not [c for c in brain.robot.calls if c[0]=='go_to_zone']
    assert brain.command('Go to the shelf b',principal='carther')['status']=='proposed'     # control: begun after the clear

def test_a_procedure_revoked_while_its_proposal_was_planned_is_not_published(published,monkeypatch):
    first=published.command('Go to the shelf b',principal='carther');published.reject(first['operation_id'],principal='carther')
    sid=published.skills.find(Goal('goto',zone='shelf_b'),'carther')['id']
    import engram.cortex.runtime as runtime
    real=runtime.verify;calls=[]
    def revoking(*a,**k):
        calls.append(1)
        if len(calls)==1: published.skills.revoke(sid)                                       # lands while it is planned
        return real(*a,**k)
    monkeypatch.setattr(runtime,'verify',revoking)
    with pytest.raises(Stale,match='revoked'): published.command('Go to the shelf b',principal='carther')   # was: proposed
    assert not published.pending()

@pytest.fixture
def published(arena,clock):
    s=Store(clock=clock,policy=Policy(auto_publish_verified=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    yield b
    b.close()


# P2-4 (class B): no principal means no private rows, never every owner's. No episode, commitment or procedure
# anchored on an episode is returned without one; the owner still sees hers.
def test_no_principal_means_no_private_rows(published):
    b=published;goal=Goal('goto',zone='shelf_b')
    b.schedule('the tray arrives','bring Sample A to the bench',principal='alice')
    for request in ('Go to the shelf b','Bring Sample A to the bench.'):
        p=b.command(request,principal='alice');b.reject(p['operation_id'],principal='alice')
    assert b.skills.find(goal,'alice')                                                      # hers, anchored on her episode
    bundle=b.store.bundle('Go to the shelf b when the tray arrives',goal)
    assert not bundle['related_episodes'] and not bundle['prospective'] and not bundle['applicable_skills'],bundle   # was: hers
    kinds=['episodic','prospective','procedural']
    r=b.store.recall('what happened last time',kinds)
    assert not any(r['memories'][k] for k in kinds) and 'shelf' not in r['reply'].lower(),r                       # was: hers
    assert not b.store.episodes_about({'sample_a'}) and not b.metrics()['episodes'] and b.skills.find(goal) is None
    mine=b.store.recall('what happened last time',kinds,principal='alice')
    assert all(mine['memories'][k] for k in kinds) and b.store.episodes_about({'sample_a'},principal='alice')    # control

def test_guard_an_anchor_needs_a_principal(brain):
    with pytest.raises(CortexError,match='principal'): brain.skills.provenance(Goal('goto',zone='bench'),'ep_x',principal='')


# P2-5: deleting a source redacts every free-text copy its episodes carry, including what an adapter echoed back.
from .test_fix_luna_r7 import ANNOUNCE,dump
class Echo(FakeRobot):
    """An adapter whose reply echoes what it was told to say, in its detail or (a careless one) in its status."""
    def __init__(self,arena,where): super().__init__(arena);self.where=where
    def execute(self,op,args):
        r=super().execute(op,args)
        if op=='speak': setattr(r,self.where,'said '+args['text'])
        return r

@pytest.mark.parametrize('where',['detail','status'])
def test_deleting_a_source_redacts_what_an_adapter_echoed(tmp_path,arena,clock,where):
    s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,Echo(arena,where))
    src=s.teach('Announcement','To announce the result, say the zqxmarker phrase.',owner='carther',goal_definitions=ANNOUNCE)
    p=b.command('announce the result',principal='carther');approve(b,p)
    assert any('zqxmarker' in row for name,row in dump(s) if name in ('operation_event','episode'))   # the premise: echoed
    s.revoke_source(src['source_id'],delete=True)
    for name,row in dump(s): assert 'zqxmarker' not in row,(name,row)                                # was: left in the journal
    ops=[r['op'] for r in s.rows('SELECT op FROM operation_event WHERE episode_id=?',(p['operation_id'],))]
    assert 'speak' in ops and b.operation(p['operation_id'])['status']==('success' if where=='detail' else 'unknown')   # audit kept
    b.close()


# P3-6: a restart after a woken proposal expired settles its commitment at once, not only once pending() runs.
def test_a_restart_after_a_proposal_expired_settles_its_commitment(tmp_path,arena,clock):
    def open_brain(): s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);return Brain(s,FakeRobot(arena))
    b=open_brain();t=b.schedule('the tray arrives','bring Sample A to the bench',principal='alice')['task_id']
    p=b.trigger('the tray arrives',event_id='ev1',principal='alice')[0]['proposal'];assert p['status']=='proposed'
    ttl=b.store.policy.approval_ttl_s;b.close();clock.advance(ttl+1)
    b=open_brain()
    assert b.operation(p['operation_id'])['status']=='expired'
    assert b.store.one('SELECT status FROM prospective_task WHERE id=?',(t,))['status']=='expired'      # was: still fired
    shown=b.store.recall('what is pending',['prospective'],principal='alice')['memories']['prospective']
    assert [x['status'] for x in shown]==['expired'],shown                                   # was: shown as fired, open
    b.close()

def test_a_restart_reconciles_an_expiry_an_older_version_left_unreconciled(tmp_path,arena,clock):
    def open_brain(): s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);return Brain(s,FakeRobot(arena))
    b=open_brain();t=b.schedule('the tray arrives','bring Sample A to the bench',principal='alice')['task_id']
    p=b.trigger('the tray arrives',event_id='ev1',principal='alice')[0]['proposal']
    with b.store.tx(): b.store.conn.execute("UPDATE episode SET outcome='expired' WHERE id=?",(p['operation_id'],))   # as it did
    b.close();b=open_brain()
    assert b.store.one('SELECT status FROM prospective_task WHERE id=?',(t,))['status']=='expired'      # was: still fired
    b.close()

def test_guard_an_expiry_settles_its_commitment_in_the_same_transaction(brain,clock):
    t=brain.schedule('the tray arrives','bring Sample A to the bench',principal='alice')['task_id']
    p=brain.trigger('the tray arrives',event_id='ev1',principal='alice')[0]['proposal'];clock.advance(brain.store.policy.approval_ttl_s)
    brain._expire()
    assert brain.operation(p['operation_id'])['status']=='expired'
    assert brain.store.one('SELECT status FROM prospective_task WHERE id=?',(t,))['status']=='expired'

def test_guard_an_expiry_at_the_window_edge_is_the_same_for_recovery_and_approval(brain,clock):
    p=brain.command('Go to the shelf b',principal='carther');clock.advance(brain.store.policy.approval_ttl_s)
    with pytest.raises(Stale): approve(brain,p)                                              # at the edge: expired for approval
    brain.recover();assert brain.operation(p['operation_id'])['status']=='expired'          # and for recovery


# P2-7 (class A): an idle answer is kept whole under an unchanged activity generation, or not at all and asked again.
# Barrier-controlled: foreground work begins right after the answer was recorded, before the cycle kept its hypotheses.
from .test_fix_learn_dream import SOP,Stub,idle_brain

def test_an_idle_answer_interrupted_while_it_is_kept_stays_pending_and_is_asked_again(arena,clock,monkeypatch):
    b=idle_brain(arena,clock);b.store.teach('SOP',SOP,owner='op')
    stub=Stub(lambda bundle:[{'text':'Goggles near the bench may help.','refs':[bundle['allowed_refs'][0]]}]);b.dreamer.qwen=stub
    save=b.dreamer._save_model_state;touched=[]
    def then_foreground_work(st):
        save(st)
        if st.get('answered') and not touched: touched.append(1);b.activity.touch()
    monkeypatch.setattr(b.dreamer,'_save_model_state',then_foreground_work)
    hypotheses=lambda:b.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='hypothesis'")['n']
    first=b.dreamer.cycle()
    assert touched and first['model_pending'] and not hypotheses(),first                     # nothing of it kept...
    assert b.dreamer._model_state()['answered'] is None                                      # ...nor recorded answered (was)
    second=b.dreamer.cycle(force=True)
    assert stub.calls==2 and hypotheses()==1 and not second['model_pending'],second         # asked again, kept whole
    b.close()
