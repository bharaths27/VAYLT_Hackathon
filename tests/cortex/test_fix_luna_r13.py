"""Regression tests for luna's round-13 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import asyncio,json,threading,time
import pytest
from fastapi.testclient import TestClient
from engram.cortex import api
from engram.cortex.api import create_app
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_luna_r7 import TA,TB

def approve(b,p,who='carther'): return b.approve(p['operation_id'],p['digest'],principal=who)
def h(token): return {'Authorization':'Bearer '+token}
LATCH="SELECT 1 FROM meta WHERE key='cortex_stop'"
LAST_STOP="SELECT status,detail FROM operation_event WHERE op='stop' ORDER BY seq DESC LIMIT 1"


# P1-1: on restart, an interrupted execution or a latched stop sends STOP to the adapter again, confirmed, and the latch
# stays until an operator clears it: the latch blocks new approvals, the adapter's stop ends motion still under way.
class Controller(FakeRobot):
    """The adapter a restarted process gets: it counts the STOPs it receives."""
    def __init__(self,arena): super().__init__(arena);self.stops=0
    def stop(self): self.stops+=1;super().stop()

def vault(tmp_path,arena,clock):
    def open_brain(robot=None): s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);return Brain(s,robot or Controller(arena))
    return open_brain

def test_a_restart_after_an_interrupted_execution_stops_the_adapter(tmp_path,arena,clock):
    open_brain=vault(tmp_path,arena,clock);b=open_brain();p=b.command('Go to the shelf b',principal='carther')
    with b.store.tx(): b.store.conn.execute("UPDATE episode SET outcome='running' WHERE id=?",(p['operation_id'],))   # died mid-step
    b.close();robot=Controller(arena);b=open_brain(robot)
    assert robot.stops==1 and robot.stopped                                                 # was: the adapter never told
    assert b.store.one(LATCH) and b.operation(p['operation_id'])['status']=='unknown'
    ev=b.store.one(LAST_STOP);assert ev['status']=='confirmed' and 'restart' in ev['detail'],ev
    b.close()

def test_a_restart_with_a_latched_stop_stops_the_adapter_again_until_it_is_cleared(tmp_path,arena,clock):
    open_brain=vault(tmp_path,arena,clock);b=open_brain();b.stop();b.close()
    robot=Controller(arena);b=open_brain(robot)
    assert robot.stops==1 and robot.stopped and b.store.one(LATCH)                          # was: the new adapter not stopped
    b.clear_stop(principal='carther');assert not b.store.one(LATCH) and not robot.stopped   # an explicit reset ends it
    b.close()

def test_control_a_clean_restart_sends_no_stop(tmp_path,arena,clock):
    open_brain=vault(tmp_path,arena,clock);b=open_brain();b.close()
    robot=Controller(arena);b=open_brain(robot);assert robot.stops==0 and not robot.stopped
    b.close()

class Unconfirmed(Controller):
    def stop(self): self.stops+=1                                       # accepted, but the adapter does not report it
    def state(self): return {**super().state(),'stopped':False}

def test_guard_a_stop_the_adapter_accepts_but_does_not_report_is_recorded_unconfirmed(tmp_path,arena,clock):
    open_brain=vault(tmp_path,arena,clock);b=open_brain();b.stop();b.close()
    robot=Unconfirmed(arena);b=open_brain(robot)
    assert robot.stops==1 and b.store.one(LATCH) and b.store.one(LAST_STOP)['status']=='requested'
    b.close()

class Unreachable(Controller):
    def stop(self): self.stops+=1;raise ConnectionError('controller unreachable')

def test_guard_a_stop_the_adapter_does_not_confirm_on_restart_keeps_the_latch(tmp_path,arena,clock):
    open_brain=vault(tmp_path,arena,clock);b=open_brain();b.stop();b.close()
    robot=Unreachable(arena);b=open_brain(robot)
    assert robot.stops==1 and b.store.one(LATCH) and b._stopped.is_set()
    assert b.store.one(LAST_STOP)['status']=='requested'                                    # recorded as unconfirmed
    b.close()


# P1-2 (class B): a procedure keeps the owner-scoped episodes it rests on. Alice's model plan was shown her private
# episode and a shared SOP: Bob can never find or be offered the procedure, and purging her episode revokes it.
from .test_fix_learn_verifier import GOOD_B,_model_brain
SOP='Safety goggles must be at the ppe station before handling Sample B.'

def test_a_procedure_resting_on_her_episode_and_a_shared_rule_stays_hers(arena,clock):
    b,_=_model_brain(arena,clock,GOOD_B);s=b.store                                         # model first, auto-publish
    s.teach('SOP',SOP,owner='alice')
    routine=s.teach('Routine','Visiting the shelf is part of the round.',owner='alice',goal_definitions={'visit the shelf':{'kind':'goto','zone':'shelf_b'}})['source_id']
    first=b.command('visit the shelf',principal='alice');b.reject(first['operation_id'],principal='alice')   # her own episode
    p=b.command('Bring Sample B to the bench.',principal='alice')
    assert p['source']=='model' and 'episode:'+first['operation_id'] in p['plan']['refs']   # it was in her prompt
    goal=Goal('fetch',('sample_b',),'bench');skill=b.skills.find(goal,'alice')
    assert skill and 'episode:'+first['operation_id'] in skill['definition']['refs']      # was: only the SOP's refs
    assert b.skills.find(goal,'bob') is None                                               # was: Bob found and reused it
    assert not s.bundle('Bring Sample B to the bench',goal,principal='bob')['applicable_skills']
    s.revoke_source(routine,delete=True)                                                   # her episode is purged
    assert b.skills.get(skill['id'])['status']=='REVOKED'                                  # was: still approved
    b.close()

def test_guard_a_requests_own_episode_is_never_among_its_related_episodes(store):
    with store.tx():
        for ep in ('ep_old','ep_now'):
            store.conn.execute('INSERT INTO episode(id,goal,request,outcome,started_at) VALUES(?,?,?,?,?)',(ep,'{}','Bring Sample B to bench','rejected',1))
            store.conn.execute('INSERT INTO cx_episode VALUES(?,?,?,?,?,?,?,?,?,?)',(ep,'SIMULATED','{}','','','{}',None,'',0,'op'))
    related=store.bundle('Bring Sample B to bench',Goal('fetch',('sample_b',),'bench'),principal='op',episode='ep_now')['related_episodes']
    assert [e['id'] for e in related]==['ep_old']


# P2-3: shutdown is cooperative. It signals the timed-task sweeper and waits for a sweep in flight in its worker thread,
# then closes the store; cancelling the await would leave that sweep running against a closed store.
def owned_app(arena,clock,monkeypatch):
    """An app that owns its brain (it closes it at shutdown), whose sweep blocks until released."""
    s=Store(clock=clock,policy=Policy(clock_trusted=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    entered,release,after=threading.Event(),threading.Event(),[]
    def blocked():
        entered.set();release.wait(5)
        try:b.store.one('SELECT 1');after.append('open')
        except Exception as e:after.append(type(e).__name__)
        return []
    b.sweep_due=blocked;monkeypatch.setattr(api,'_configured_brain',lambda:b)
    client=TestClient(create_app(tokens={TA:'alice'},start_idle=False));client.__enter__();assert entered.wait(5)
    return b,client,release,after

def test_shutdown_waits_for_a_sweep_in_flight_before_closing_the_store(arena,clock,monkeypatch):
    b,client,release,after=owned_app(arena,clock,monkeypatch)
    closing=threading.Thread(target=client.__exit__,args=(None,None,None),daemon=True);closing.start()
    time.sleep(.3);waited=closing.is_alive()
    release.set();closing.join(10)
    assert waited and after==['open'],(waited,after)                  # was: shut down at once; the sweep then hit a closed store
    with pytest.raises(Exception):b.store.one('SELECT 1')            # closed once the sweep was done

def test_guard_a_sweep_that_outlasts_the_shutdown_wait_leaves_the_store_open(arena,clock,monkeypatch):
    monkeypatch.setattr(api,'SHUTDOWN_WAIT_S',.2,raising=False)
    b,client,release,after=owned_app(arena,clock,monkeypatch)
    threading.Timer(.6,release.set).start()                           # it ends after the shutdown gave up waiting for it
    client.__exit__(None,None,None)
    assert after==['open'] and b.store.one('SELECT 1')                # it ran on an open store, and the app left that open
    b.close()
