import json
import pytest
from engram.cortex.runtime import Brain
from engram.cortex.store import Store
from engram.cortex.simulation import FakeRobot
from engram.cortex.types import *

def make_task(b):return b.command('When calibration finishes, bring Sample A to bench.',principal='carther')
def fire(b,e='event-1'):return b.trigger('calibration finishes',event_id=e,principal='carther')

def test_waits_then_stages_never_auto_executes(brain):
    t=make_task(brain);assert t['kind']=='scheduled'
    assert not brain.pending() and not brain.robot.calls
    out=fire(brain)
    assert len(out)==1 and out[0]['proposal']['status']=='proposed'
    assert not brain.robot.calls
    assert brain.store.one('SELECT status FROM prospective_task WHERE id=?',(t['task_id'],))['status']=='fired'

def test_duplicate_instruction_and_trigger(brain):
    a=make_task(brain);b=make_task(brain);assert a['task_id']==b['task_id']
    x=fire(brain);y=fire(brain)
    assert x[0]['proposal']['operation_id']==y[0]['proposal']['operation_id']
    assert brain.store.one('SELECT COUNT(*) n FROM episode')['n']==1
    assert fire(brain,'other-event')==[]

def test_changed_event_payload_conflicts(brain):
    make_task(brain);fire(brain)
    with pytest.raises(Conflict):brain.trigger('different trigger',event_id='event-1',principal='carther')

def test_cancel_before_trigger(brain):
    t=make_task(brain);brain.cancel_task(t['task_id'],principal='carther')
    assert fire(brain)==[] and not brain.pending() and not brain.robot.calls

def test_cancel_after_trigger_revokes_pending(brain):
    t=make_task(brain);p=fire(brain)[0]['proposal'];brain.cancel_task(t['task_id'],principal='carther')
    with pytest.raises(Conflict):brain.approve(p['operation_id'],p['digest'],principal='carther')
    assert not brain.robot.calls

def test_prospective_owner_boundary(brain):
    t=make_task(brain)
    with pytest.raises(Denied):brain.cancel_task(t['task_id'],principal='other')
    assert brain.trigger('calibration finishes',event_id='x',principal='other')==[]

def test_restart_retains_waiting_and_cancelled(tmp_path,arena,clock):
    path=tmp_path/'state.db';s=Store(path,clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    t=make_task(b);c=b.schedule('other event','bring Sample B to bench',principal='carther');b.cancel_task(c['task_id'],principal='carther');b.close()
    b=Brain(Store(path,clock=clock),FakeRobot(arena));out=fire(b)
    assert len(out)==1 and out[0]['task_id']==t['task_id']
    assert b.trigger('other event',event_id='different',principal='carther')==[]
    b.close()

def test_wake_outbox_survives_crash_after_event_commit(tmp_path,arena,clock):
    path=tmp_path/'state.db';s=Store(path,clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    t=make_task(b)
    b.reconcile_wakes=lambda **kwargs: [] # Inject crash boundary after durable event/wake, before proposal.
    fire(b);b.close()
    b=Brain(Store(path,clock=clock),FakeRobot(arena));out=b.reconcile_wakes()
    assert len(out)==1 and out[0]['proposal']['status']=='proposed'
    assert not b.robot.calls;b.close()

def test_recovery_reuses_preparing_episode_id(tmp_path,arena,clock):
    path=tmp_path/'state.db';s=Store(path,clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    t=make_task(b);p=fire(b)[0]['proposal']
    with s.tx():
        s.conn.execute("UPDATE episode SET outcome='preparing' WHERE id=?",(p['operation_id'],))
        s.conn.execute("UPDATE cx_wake SET status='preparing',proposal_id=NULL")
        s.conn.execute('UPDATE prospective_task SET proposal_id=NULL')
    b.close();b=Brain(Store(path,clock=clock),FakeRobot(arena));out=b.reconcile_wakes()
    assert out[0]['proposal']['operation_id']==p['operation_id']
    assert b.store.one('SELECT COUNT(*) n FROM episode')['n']==1;b.close()

def test_completed_task_requires_observed_outcome(brain):
    t=make_task(brain);p=fire(brain)[0]['proposal']
    assert brain.store.one('SELECT status FROM prospective_task')['status']=='fired'
    brain.approve(p['operation_id'],p['digest'],principal='carther')
    assert brain.store.one('SELECT status FROM prospective_task')['status']=='done'

def test_expired_task_does_not_wake(brain,clock):
    brain.schedule('calibration finishes','bring Sample A to bench',principal='carther',expires_at=clock()+1)
    clock.advance(2)
    assert fire(brain)==[]

def test_revoked_source_cancels_waiting_task(brain):
    t=make_task(brain);r=brain.store.one('SELECT source_id FROM prospective_task WHERE id=?',(t['task_id'],))
    brain.store.revoke_source(r['source_id']);assert fire(brain)==[]

def test_timer_requires_explicit_trusted_clock(brain,clock):
    with pytest.raises(Denied):brain.schedule_at(clock()+10,'Bring Sample A to bench',principal='carther')

def test_timed_commitment_is_durable_and_at_most_once(tmp_path,arena,clock):
    path=tmp_path/'db';s=Store(path,clock=clock,policy=Policy(clock_trusted=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    t=b.schedule_at(clock()+10,'Bring Sample A to bench',principal='carther')
    assert b.sweep_due()==[];b.close();clock.advance(11)
    b=Brain(Store(path,clock=clock,policy=Policy(clock_trusted=True)),FakeRobot(arena))
    out=b.sweep_due();assert len(out)==1 and not b.robot.calls
    assert b.sweep_due()==[]
    b.close()

def test_clock_rollback_suspends_timer_sweep(arena,clock):
    s=Store(clock=clock,policy=Policy(clock_trusted=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    b.sweep_due();clock.advance(-10)
    with pytest.raises(Stale):b.sweep_due()
    b.close()
