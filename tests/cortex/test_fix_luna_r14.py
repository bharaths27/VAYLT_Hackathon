"""Regression tests for luna's round-14 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import asyncio,json,threading,time
import pytest
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot,Result
from engram.cortex.store import Store
from engram.cortex.types import *

def approve(b,p,who='carther'): return b.approve(p['operation_id'],p['digest'],principal=who)


# P1-1 (class D, the empty case): a plan approved with an empty claw dispatches no step that carries, needs, puts down or
# grips what the claw holds once something is in it; the expected claw state follows the plan's own pickups and placements.
def loaded_once_running(brain,oid,after_calls=0):
    """Something is put in the claw once the operation runs (after `after_calls` dispatched steps)."""
    real,execute=brain.operation,brain.robot.execute
    def load():
        if brain.robot.holding is None: brain.robot.positions.pop(oid);brain.robot.holding=oid;brain.robot.open=False
    def loading(ep):
        out=real(ep)
        if out['status']=='running' and not after_calls: load()
        return out
    def counting(op,args):
        r=execute(op,args)
        if after_calls and len(brain.robot.calls)==after_calls: load()
        return r
    brain.operation=loading;brain.robot.execute=counting

@pytest.mark.parametrize('request_,op,oid',[('Go to the shelf b','go_to_zone','sample_a'),('Close the claw','gripper_close','sample_b'),
                                            ('Open the claw','gripper_open','sample_b'),('Bring Sample A to the bench.','pick_tagged_object','sample_b')])
def test_a_claw_loaded_after_an_approval_with_it_empty_dispatches_nothing_that_uses_it(brain,request_,op,oid):
    p=brain.command(request_,principal='carther');assert p['status']=='proposed' and not p['plan'].get('held')
    loaded_once_running(brain,oid)
    assert approve(brain,p)['status']=='aborted'
    assert not [c for c in brain.robot.calls if c[0]==op]                    # was: carried it, closed on it, released it, picked over it

def test_a_claw_loaded_between_steps_stops_the_plan_before_its_next_pickup(brain):
    brain.store.teach('SOP','Safety goggles must be at the ppe station before handling Sample B.',owner='carther')
    p=brain.command('Bring Sample B to the bench.',principal='carther');assert [s['op'] for s in p['plan']['steps']].count('pick_tagged_object')==2
    loaded_once_running(brain,'sample_a',after_calls=3)                    # after the goggles were placed
    assert approve(brain,p)['status']=='aborted'
    assert [c[0] for c in brain.robot.calls].count('pick_tagged_object')==1 # was: picked Sample B over Sample A

class StopsReporting(FakeRobot):
    """An adapter that stops reporting its claw once the operation runs."""
    reporting=True
    def state(self):
        s=super().state()
        if not self.reporting: s.pop('holding')
        return s

def test_guard_a_claw_no_longer_reported_dispatches_nothing_that_uses_it(store,arena):
    b=Brain(store,StopsReporting(arena));p=b.command('Go to the shelf b',principal='carther');real=b.operation
    def running(ep):
        out=real(ep)
        if out['status']=='running': b.robot.reporting=False
        return out
    b.operation=running
    assert approve(b,p)['status']=='aborted' and not [c for c in b.robot.calls if c[0]=='go_to_zone']

def test_guard_a_claw_swapped_after_its_pickup_puts_nothing_down(brain):
    p=brain.command('Bring Sample A to the bench.',principal='carther');robot=brain.robot;execute=robot.execute
    def swapping(op,args):                                                  # Sample B ends up in the claw instead of Sample A
        r=execute(op,args)
        if op=='pick_tagged_object': robot.positions['sample_a']=robot.at;robot.positions.pop('sample_b');robot.holding='sample_b'
        return r
    robot.execute=swapping
    assert approve(brain,p)['status']=='aborted' and not [c for c in robot.calls if c[0]=='place_at_zone']   # was: put it down

def test_control_the_plans_own_pickups_and_placements_are_what_it_expects(brain):
    brain.store.teach('SOP','Safety goggles must be at the ppe station before handling Sample B.',owner='carther')
    assert approve(brain,brain.command('Bring Sample B to the bench.',principal='carther'))['status']=='success'
