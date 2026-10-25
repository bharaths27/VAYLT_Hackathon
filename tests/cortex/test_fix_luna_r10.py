"""Regression tests for luna's round-10 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import pytest
from engram.cortex.planning import DeniedPlan
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.types import *

def approve(b,p,who='carther'): return b.approve(p['operation_id'],p['digest'],principal=who)


# P1-1: closing the claw handles what it holds: that object's constraints apply, and unreported contents are refused.
def holding(brain,oid):
    brain.robot.positions.pop(oid);brain.robot.holding=oid;brain.robot.open=False

@pytest.mark.parametrize('note,error',[('Leave Sample B alone.',Missing),('Never move Sample B.',DeniedPlan)])
def test_closing_the_claw_on_a_constrained_held_object_is_refused(brain,note,error):
    holding(brain,'sample_b');brain.store.teach('Note',note,owner='carther')
    with pytest.raises(error): brain.command('Close the claw',principal='carther')          # was: proposed

def test_a_constraint_taught_after_proposing_a_close_still_refuses_it_at_approval(brain):
    holding(brain,'sample_b');p=brain.command('Close the claw',principal='carther');assert p['status']=='proposed'
    brain.store.teach('Note','Leave Sample B alone.',owner='carther')
    with pytest.raises(Denied,match='structured review'): approve(brain,p)                  # was: closed on it
    assert not [c for c in brain.robot.calls if c[0]=='gripper_close']

def test_control_closing_an_empty_claw_is_not_held_to_another_objects_constraint(brain):
    brain.store.teach('Note','Leave Sample B alone.',owner='carther')
    assert approve(brain,brain.command('Close the claw',principal='carther'))['status']=='success'

class NoHoldingReport(FakeRobot):
    def state(self): s=super().state();s.pop('holding');return s

def test_closing_a_claw_whose_contents_are_unreported_is_refused(store,arena):
    b=Brain(store,NoHoldingReport(arena))
    with pytest.raises(Conflict,match='contents are unknown'): b.command('Close the claw',principal='carther')   # was: proposed


# P1-2: a condition of time or circumstance on motion or handling is unresolved: it blocks the unconditional plan.
@pytest.mark.parametrize('note,command',[('Go to shelf B while the floor is wet.','Go to the shelf b'),
                                         ('When the tray arrives, bring Sample A to the bench.','Bring Sample A to the bench.'),
                                         ('Take Sample B to the bench during the night shift.','Bring Sample B to the bench.')])
def test_a_condition_on_motion_or_handling_blocks_the_unconditional_plan(brain,note,command):
    assert brain.store.teach('Note',note,owner='carther')['unresolved_rules']
    with pytest.raises(Missing,match='structured review'): brain.command(command,principal='carther')   # was: proposed

def test_a_condition_taught_after_proposing_stops_its_approval(brain):
    p=brain.command('Go to the shelf b',principal='carther');assert p['status']=='proposed'
    brain.store.teach('Note','Go to shelf B while the floor is wet.',owner='carther')
    with pytest.raises(CortexError): approve(brain,p)
    assert not [c for c in brain.robot.calls if c[0]=='go_to_zone']

@pytest.mark.parametrize('note',['Safety goggles must be at the ppe station before handling Sample B.','Lena arrives when the lab opens.'])
def test_control_a_stored_rule_and_a_remark_add_no_condition(store,note):
    assert not store.teach('Note',note,owner='carther')['unresolved_rules']
