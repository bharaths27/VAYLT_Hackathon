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
