import copy,json,threading,time
from concurrent.futures import ThreadPoolExecutor
import pytest
from engram.cortex.runtime import Brain
from engram.cortex.store import Store
from engram.cortex.simulation import FakeRobot
from engram.cortex.types import *
from engram.cortex.planning import Compiler,verify

def propose(b,key=None): return b.command('Bring Sample A to the bench.',principal='carther',idempotency_key=key)
def approve(b,p): return b.approve(p['operation_id'],p['digest'],principal='carther')

def test_new_task_creates_memory_then_executes_from_it(brain):
    brain.store.teach('SOP','Safety goggles must be at ppe station before handling Sample B.',owner='carther')
    p=brain.command('Bring Sample B to bench.',principal='carther')
    assert p['status']=='proposed' and p['derived_memory_id']
    assert not brain.robot.calls
    recipe=brain.store.derived(p['derived_memory_id'])
    assert recipe['payload']['steps']==p['plan']['steps'] and recipe['status']=='VERIFIED'
    result=approve(brain,p)
    assert result['status']=='success'
    assert brain.robot.state()['objects']['sample_b']=='bench'
    assert brain.robot.state()['objects']['goggles']=='ppe_station'
    assert brain.store.one('SELECT COUNT(*) n FROM approval')['n']==1

def test_recipe_tamper_blocks_execution(brain):
    p=propose(brain)
    with brain.store.tx():
        brain.store.conn.execute("UPDATE cx_derived SET payload_json='{}' WHERE id=?",(p['derived_memory_id'],))
    with pytest.raises(CortexError): approve(brain,p)
    assert not brain.robot.calls

@pytest.mark.parametrize('utterance',["Do not move Sample A to the bench.","Don't move sample A to bench.","Never pick Sample A.","Move sample A to bench tomorrow.","Dump Sample A in the trash."])
def test_unsafe_or_future_wording_does_not_propose(brain,utterance):
    result=brain.command(utterance,principal='carther')
    assert result['status']=='refused'
    assert not brain.robot.calls

def test_retrospective_question_does_not_schedule_or_plan(brain):
    r=brain.command('After the experiment finished, what happened to Sample A?',principal='carther')
    assert r['kind']=='answer'
    assert brain.store.one('SELECT COUNT(*) n FROM prospective_task')['n']==0
    assert brain.store.one('SELECT COUNT(*) n FROM episode')['n']==0

def test_duplicate_key_reuses_same_proposal(brain):
    a=propose(brain,'same'); b=propose(brain,'same')
    assert a['operation_id']==b['operation_id']
    assert brain.store.one('SELECT COUNT(*) n FROM episode')['n']==1
    with pytest.raises(Conflict): brain.command('Bring Sample B to bench.',principal='carther',idempotency_key='same')

def test_duplicate_approval_dispatches_once(brain):
    p=propose(brain); approve(brain,p); calls=len(brain.robot.calls)
    with pytest.raises(Conflict): approve(brain,p)
    assert len(brain.robot.calls)==calls

def test_concurrent_approval_one_effect(brain):
    p=propose(brain)
    def attempt(_):
        try: return approve(brain,p)['status']
        except CortexError:return 'denied'
    with ThreadPoolExecutor(2) as pool: results=list(pool.map(attempt,range(2)))
    assert results.count('success')==1 and results.count('denied')==1
    assert sum(op=='pick_tagged_object' for op,args in brain.robot.calls)==1

@pytest.mark.parametrize('wrong', ['identity','digest','expiry'])
def test_approval_is_bound(brain,clock,wrong):
    p=propose(brain)
    if wrong=='expiry': clock.advance(200)
    with pytest.raises(CortexError): brain.approve(p['operation_id'],'0'*64 if wrong=='digest' else p['digest'],principal='other' if wrong=='identity' else 'carther')
    assert not brain.robot.calls

def test_rule_change_invalidates_pending_plan(brain):
    source=brain.store.teach('rule','Safety goggles must be at ppe station before handling Sample B.',owner='carther')
    p=brain.command('Bring Sample B to bench',principal='carther')
    brain.store.teach('revision','Safety goggles must be at shelf B before handling Sample B.',owner='carther',supersedes=source['source_id'])
    with pytest.raises(CortexError): approve(brain,p)
    assert not brain.robot.calls

def test_server_rejection_not_merely_ui_dismissal(brain):
    p=propose(brain); brain.reject(p['operation_id'],principal='carther')
    assert not brain.pending()
    with pytest.raises(Conflict): approve(brain,p)

def test_unknown_effect_is_journaled_no_retry(brain):
    p=propose(brain)
    brain.robot.faults['pick_tagged_object']='unknown'
    r=approve(brain,p)
    assert r['status']=='unknown'
    assert brain.robot.state()['stopped']
    count=len(brain.robot.calls)
    with pytest.raises(CortexError): approve(brain,p)
    assert len(brain.robot.calls)==count

def test_adapter_exception_is_unknown(brain):
    p=propose(brain)
    brain.robot.faults['pick_tagged_object']=RuntimeError('test adapter fault')
    r=approve(brain,p)
    assert r['status']=='unknown' and brain.robot.state()['stopped']

def test_stop_revokes_pending_and_does_not_resume(brain):
    p=propose(brain); assert brain.stop()['stop_confirmed']
    brain.clear_stop(principal='carther')
    with pytest.raises(Conflict): approve(brain,p)
    assert not brain.robot.calls

def test_physical_ack_requires_independent_observation(tmp_path,arena,clock):
    class PhysicalLike(FakeRobot):
        def state(self): return {**super().state(),'adapter':'test-physical'}
    s=Store(tmp_path/'live.db',policy=Policy(live_enabled=True),clock=clock); s.register_arena(arena)
    b=Brain(s,PhysicalLike(arena))
    p=b.command('Open the claw',principal='carther'); r=approve(b,p)
    assert r['status']=='needs_verification'
    with pytest.raises(Denied): b.verify_outcome(p['operation_id'],principal='stranger',ok=True,note='saw it')
    b.verify_outcome(p['operation_id'],principal='carther',ok=True,note='Test-only operator observation, not actual hardware')
    assert b.operation(p['operation_id'])['status']=='success'
    b.close()

def test_physical_adapter_disabled_by_default(arena,store):
    class PhysicalLike(FakeRobot):
        def state(self): return {**super().state(),'adapter':'test-physical'}
    with pytest.raises(Denied): Brain(store,PhysicalLike(arena))

def test_symbolic_oracle_rejects_wrong_goal_and_extras(store):
    g=Goal('fetch',('sample_a',),'bench'); c=Compiler(store); plan=c.compile(g); f=c.fixture(symbolic=True)
    assert verify(g,plan['steps'],f,[],store.policy).ok
    bad=copy.deepcopy(plan['steps']); bad[-1]['args']['zone']='shelf_b'
    assert not verify(g,bad,f,[],store.policy).ok
    assert not verify(g,[{'op':'observe','args':{}}],f,[],store.policy).ok
    assert not verify(g,plan['steps']+[{'op':'drive_bounded','args':{'distance_m':0.1}}],f,[],store.policy).ok

@pytest.mark.parametrize('value',[float('nan'),float('inf'),True,100,-100,'0.1'])
def test_primitive_bounds_and_types(store,value):
    goal=Goal('primitive',primitive='drive_bounded',args={'distance_m':0.1})
    assert not verify(goal,[{'op':'drive_bounded','args':{'distance_m':value}}],Compiler(store).fixture(symbolic=True),[],store.policy).ok

def test_new_derived_recipe_cannot_skip_real_observation(brain):
    p=propose(brain)
    original=brain.robot.execute
    def execute(op,args):
        r=original(op,args)
        if op=='locate_tag': r.observations=[]
        return r
    brain.robot.execute=execute
    r=approve(brain,p)
    assert r['status']=='aborted'
    assert not any(op=='pick_tagged_object' for op,args in brain.robot.calls)

def test_restart_does_not_replay_unknown_effect(tmp_path,arena,clock):
    pth=tmp_path/'db'; s=Store(pth,clock=clock); s.register_arena(arena); b=Brain(s,FakeRobot(arena))
    p=propose(b)
    with s.tx():s.conn.execute("UPDATE episode SET outcome='running' WHERE id=?",(p['operation_id'],))
    b.close(); s=Store(pth,clock=clock); b=Brain(s,FakeRobot(arena))
    assert b.operation(p['operation_id'])['status']=='unknown'
    assert not b.robot.calls
    with pytest.raises(Denied): b.clear_stop(principal='carther')
    b.close()

def test_only_one_process_owner(tmp_path,arena):
    pth=tmp_path/'db'; s=Store(pth); s.register_arena(arena); b=Brain(s,FakeRobot(arena))
    with pytest.raises(Conflict): Store(pth)  # refused before it can migrate or register an arena
    b.close()
