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


# P1-2 (class D): a claw primitive handles what the claw holds, so that object's prerequisite rules apply as a pickup's
# do: at planning (unmet or disputed), in the plan's basis, at approval (the same object still held) and at dispatch.
SOP='Safety goggles must be at the ppe station before handling Sample B.'
def holding(brain,oid): brain.robot.positions.pop(oid);brain.robot.holding=oid;brain.robot.open=False
def seen_at(store,oid,zone): store.observe(oid,zone,origin='test',mode='SIMULATED')

def test_closing_on_a_held_object_whose_prerequisite_is_unmet_is_refused(brain):
    brain.store.teach('SOP',SOP,owner='carther');holding(brain,'sample_b')
    with pytest.raises(Conflict,match='prerequisite'): brain.command('Close the claw',principal='carther')   # was: proposed
    seen_at(brain.store,'goggles','ppe_station')
    p=brain.command('Close the claw',principal='carther');assert p['plan']['held']==['sample_b']   # control: met
    assert any(f['subject']=='sample_b' for f in p['memory']['facts'])                          # its rule is in the evidence
    assert approve(brain,p)['status']=='success'

def test_closing_on_a_held_object_with_disputed_prerequisites_is_refused(brain):
    brain.store.teach('SOP',SOP,owner='carther');brain.store.teach('SOP 2','Safety goggles must be at shelf B before handling Sample B.',owner='carther')
    seen_at(brain.store,'goggles','ppe_station');holding(brain,'sample_b')
    with pytest.raises(Conflict,match='conflicting'): brain.command('Close the claw',principal='carther')    # was: proposed

def test_a_prerequisite_taught_after_proposing_a_close_withdraws_it(brain):
    holding(brain,'sample_b');p=brain.command('Close the claw',principal='carther');assert p['status']=='proposed'
    brain.store.teach('SOP',SOP,owner='carther')
    assert brain.operation(p['operation_id'])['status']=='invalidated'                     # was: still approvable
    with pytest.raises(CortexError): approve(brain,p)
    assert not [c for c in brain.robot.calls if c[0]=='gripper_close']

def test_a_close_proposed_for_one_held_object_never_grips_another(brain):
    brain.store.teach('SOP',SOP,owner='carther')
    holding(brain,'sample_a');p=brain.command('Close the claw',principal='carther');assert p['plan']['held']==['sample_a']
    brain.robot.positions['sample_a']='home';holding(brain,'sample_b')                    # the claw now holds Sample B
    with pytest.raises(Stale,match='holds something else'): approve(brain,p)              # was: closed on Sample B
    assert not [c for c in brain.robot.calls if c[0]=='gripper_close']

def test_the_independent_check_alone_holds_a_close_to_the_held_objects_rules(store):
    rule=lambda zone,status:{'id':'r-'+zone,'subject':'sample_b','object':'goggles','required_zone':zone,'status':status}
    world={**Compiler(store).fixture(),'holding':'sample_b','holding_known':True};close=Goal('primitive',primitive='gripper_close')
    world['positions']['goggles']='ppe_station'
    assert verify(close,[{'op':'gripper_close','args':{}}],world,[rule('ppe_station','active')],store.policy).ok   # control: met
    assert not verify(close,[{'op':'gripper_close','args':{}}],world,[rule('ppe_station','disputed')],store.policy).ok
    assert not verify(close,[{'op':'gripper_close','args':{}}],world,[rule('shelf_b','active')],store.policy).ok

def test_a_cyclic_prerequisite_graph_of_the_held_object_is_refused(brain):
    brain.store.teach('SOP',SOP,owner='carther');brain.store.teach('Loop','Sample B must be at the bench before handling Safety goggles.',owner='carther')
    holding(brain,'sample_b')
    with pytest.raises(Conflict,match='cyclic'): brain.command('Close the claw',principal='carther')

def test_guard_an_unrelated_source_leaves_a_close_on_a_held_object_pending(brain):
    brain.store.teach('SOP',SOP,owner='carther');seen_at(brain.store,'goggles','ppe_station');holding(brain,'sample_b')
    p=brain.command('Close the claw',principal='carther');brain.store.teach('Note','The tray is on shelf B.',owner='carther')
    assert brain.operation(p['operation_id'])['status']=='proposed'                         # its basis is the held object's

def test_a_close_for_a_held_object_is_composed_afresh_never_a_stored_procedure(arena,clock):
    s=Store(clock=clock,policy=Policy(auto_publish_verified=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    first=b.command('Close the claw',principal='carther');b.reject(first['operation_id'],principal='carther')   # empty claw
    assert b.skills.find(Goal('primitive',primitive='gripper_close'),'carther')                                   # published
    s.teach('SOP',SOP,owner='carther');seen_at(s,'goggles','ppe_station');holding(b,'sample_b')
    p=b.command('Close the claw',principal='carther')
    assert p['status']=='proposed' and p['source']=='composed' and not p['skill_id'] and p['plan']['held']==['sample_b']
    assert approve(b,p)['status']=='success'
    b.close()

def test_the_claw_changing_between_approval_and_dispatch_stops_the_close(brain):
    brain.store.teach('SOP',SOP,owner='carther');seen_at(brain.store,'goggles','ppe_station');holding(brain,'sample_a')
    p=brain.command('Close the claw',principal='carther');real=brain.operation
    def swapped(ep):
        out=real(ep)
        if out['status']=='running' and brain.robot.holding=='sample_a': brain.robot.positions['sample_a']='home';holding(brain,'sample_b')
        return out
    brain.operation=swapped
    assert approve(brain,p)['status']=='aborted'                                            # never closed on Sample B
    assert not [c for c in brain.robot.calls if c[0]=='gripper_close']

def test_a_prerequisite_no_longer_observed_at_dispatch_stops_the_close(brain,clock):
    brain.store.teach('SOP',SOP,owner='carther');seen_at(brain.store,'goggles','ppe_station');holding(brain,'sample_b')
    p=brain.command('Close the claw',principal='carther');real=brain.operation
    def ageing(ep):
        out=real(ep)
        if out['status']=='running': clock.advance(brain.store.policy.freshness_s+1)       # the observation ages before dispatch
        return out
    brain.operation=ageing
    assert approve(brain,p)['status']=='aborted'                                            # was: closed on a stale observation
    assert not [c for c in brain.robot.calls if c[0]=='gripper_close']


# P2-3: a model plan rests on every source-backed input of the exact context it was shown, even one it did not cite:
# revoking that source withdraws the proposal and the recipe built from it.
from engram.cortex.models import ModelResult
class Planner:
    """A local model asked first that plans from its context and cites nothing of it."""
    planning_order='model_first'
    def __init__(self,steps): self.steps=steps
    def manifest(self): return {}
    def complete(self,purpose,bundle,**kw): return ModelResult({'steps':self.steps},'stub','m',0,'h',1,'p')

def test_a_source_retrieved_for_a_model_plan_backs_it_even_uncited(store,arena):
    note=store.teach('Note','Sample A is kept in the cold room.',owner='carther')['source_id']
    steps=[{'op':'locate_tag','args':{'object_id':'sample_a'}},{'op':'pick_tagged_object','args':{'object_id':'sample_a'}},{'op':'place_at_zone','args':{'zone':'bench'}}]
    b=Brain(store,FakeRobot(arena),qwen=Planner(steps));p=b.command('Bring Sample A to the bench.',principal='carther')
    assert p['source']=='model' and any(e.get('source_id')==note for e in p['memory']['evidence'])   # retrieved, uncited
    assert 'source:'+note in p['plan']['refs']                                                     # was: missing
    store.revoke_source(note)
    assert b.operation(p['operation_id'])['status']=='invalidated'                                # was: still proposed
    assert store.derived(p['derived_memory_id'])['status']=='STALE'


# P3-4: only a waiting or fired commitment can be cancelled; a settled one keeps its outcome.
@pytest.mark.parametrize('outcome',['failed','expired'])
def test_a_settled_commitment_is_never_withdrawn_to_cancelled(brain,clock,outcome):
    if outcome=='expired':
        t=brain.schedule('the tray arrives','bring Sample A to the bench',principal='carther',expires_at=clock()+10)['task_id']
        clock.advance(11)
    else:
        brain.store.teach('Rule','Never move Sample A.',owner='carther')                       # its wake is refused: failed
        t=brain.schedule('the tray arrives','bring Sample A to the bench',principal='carther')['task_id']
    brain.trigger('the tray arrives',event_id='ev1',principal='carther')
    status=lambda:brain.store.one('SELECT status FROM prospective_task WHERE id=?',(t,))['status']
    assert status()==outcome
    with pytest.raises(Conflict,match='outcome stands'): brain.cancel_task(t,principal='carther')   # was: cancelled
    assert status()==outcome


# P2-5 (class A, in round 11's own fix): no foreground work begins between an idle answer's last check and its commit.
# Barrier-controlled: the keep's COMMIT pauses while another thread tries to begin foreground work.
from .test_fix_learn_dream import SOP as DREAM_SOP,Stub,idle_brain
class PausingAtCommit:
    """The vault's connection, pausing at its next COMMIT once armed: right then the hook runs."""
    def __init__(self,conn): self.conn,self.armed,self.hook=conn,False,None
    def execute(self,sql,*args):
        if self.armed and sql=='COMMIT': self.armed=False;self.hook()
        return self.conn.execute(sql,*args)
    def __getattr__(self,name): return getattr(self.conn,name)

def test_foreground_work_cannot_begin_between_an_idle_answers_check_and_its_commit(arena,clock,monkeypatch):
    b=idle_brain(arena,clock);b.store.teach('SOP',DREAM_SOP,owner='op')
    b.dreamer.qwen=Stub(lambda bundle:[{'text':'Goggles near the bench may help.','refs':[bundle['allowed_refs'][0]]}])
    conn=PausingAtCommit(b.store.conn);b.store.conn=conn;entered,release,began=threading.Event(),threading.Event(),[]
    def foreground():
        with b.activity.foreground(): entered.set();release.wait(5)
    worker=threading.Thread(target=foreground,daemon=True)
    def at_commit(): worker.start();began.append(entered.wait(.5))                        # does it begin before the commit?
    conn.hook=at_commit;save=b.dreamer._save_model_state
    def arm(st):
        save(st)
        if st.get('answered'): conn.armed=True                                             # recorded: the keep's commit is next
    monkeypatch.setattr(b.dreamer,'_save_model_state',arm)
    try: b.dreamer.cycle()
    finally:
        release.set();b.store.conn=conn.conn
        if worker.ident: worker.join(5)
    assert began==[False],began                                                            # was: it began, the answer kept anyway
    assert b.dreamer._model_state()['answered'] and b.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='hypothesis'")['n']==1
    b.close()

def test_guard_an_idle_answer_is_never_kept_from_inside_a_transaction(arena,clock):
    b=idle_brain(arena,clock)
    with b.store.tx():
        with pytest.raises(RuntimeError,match='lock order'): b.dreamer._keep(None,'x',[],[],[],0)
    b.close()


# P2-7: an explicit empty set of sensor credentials disables sensors; only an absent set falls back to the environment.
SENSOR='synthetic-test-sensor-token-for-the-camera-0123'
PACKET={'object_id':'sample_a','zone':'bench','captured_at':1700000000.0,'boot_id':'boot-1','sequence':1,'frame_id':'f-1','calibration_id':'cal-1'}

def test_an_explicit_empty_sensor_credential_set_refuses_the_environment_token(brain,monkeypatch):
    monkeypatch.setenv('CORTEX_SENSOR_TOKEN',SENSOR)
    with TestClient(create_app(brain,tokens={TA:'alice'},sensor_tokens={},start_idle=False)) as c:
        r=c.post('/observations/sensor',json=PACKET,headers=h(SENSOR))
        assert r.status_code==401,r.text                                                     # was: a trusted observation
    assert not brain.store.one('SELECT 1 FROM observation')
    with TestClient(create_app(brain,tokens={TA:'alice'},start_idle=False)) as c:              # control: absent, the environment's
        assert c.post('/observations/sensor',json=PACKET,headers=h(SENSOR)).status_code==200
