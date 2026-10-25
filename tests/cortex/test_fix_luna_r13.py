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
