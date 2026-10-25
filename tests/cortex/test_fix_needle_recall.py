"""Needle-benchmark fixes (H:/_scratch/needle-bench): every question reaches memory on the command path, and a
location is answered from object state only while that state is current. The question tests fail on d22c851."""
import pytest
from engram.cortex.types import *

NOTE='Spare goggles are kept in drawer 3 of the cabinet.'

@pytest.mark.parametrize('q',['Where can I grab a backup pair of goggles?','Which drawer are the extra goggles in?',
    'When is the robot supposed to restock the goggles?','how do I find the spare goggles','Can the goggles go in drawer 3?',
    'Is there a drawer for the spare goggles?','My goggles broke. Where can I grab a backup pair?'])
def test_every_question_is_answered_from_memory_on_the_command_path(brain,q):
    brain.store.teach('Lab note',NOTE,owner='carther')
    r=brain.command(q,principal='carther')
    assert r['kind']=='answer' and NOTE in r['reply'],r                  # was: a refused proposal ("reads as a question" / future intent)
    assert not brain.store.rows('SELECT id FROM episode')                # an answer, never a proposal

def test_instructions_commitments_and_observation_keep_their_paths(brain):
    c=lambda u:brain.command(u,principal='carther')
    assert c('When the tray arrives, bring Sample A to the bench.')['kind']=='scheduled'     # a commitment, not a question
    seen=c('What do you see?'); assert seen['kind']=='proposal' and seen['plan']['steps'][0]['op']=='observe'
    assert c('Could you bring Sample A to the bench')['status']=='proposed'                   # a polite request is an instruction
    for u in ('Can you bring Sample A to the bench?','Please bring Sample A to the bench?'):
        assert c(u)['kind']=='proposal',u                                                      # still refused as an instruction, not answered
    for u,why in (('Do not move Sample B','Negative'),('When the tray arrives bring Sample A to the bench','Future intent')):
        r=c(u); assert r['kind']=='proposal' and r['status']=='refused' and why in r['reason'],r


def test_location_answers_from_object_state_only_while_current(store,clock):
    v1=store.teach('Storage','Sample A is stored in fridge 1.',owner='carther')
    store.teach('Storage','Sample A is stored in fridge 2 now.',owner='carther',supersedes=v1['source_id'])
    q='Where is Sample A stored now?'
    assert store.recall(q)['reply']=='Source says: Sample A is stored in fridge 2 now.'      # never observed: was "Sample A: location unknown"
    store.observe('sample_a','bench',origin='camera',mode='OPERATOR')
    assert store.recall(q)['reply']=='Sample A is at bench'                                   # a current observation answers
    clock.advance(store.policy.freshness_s+1)
    assert store.recall(q)['reply']=='Source says: Sample A is stored in fridge 2 now.'      # stale again: back to the evidence

def test_location_without_any_evidence_still_reports_the_object_state(store,clock):
    store.observe('sample_a','bench',origin='camera',mode='OPERATOR'); clock.advance(store.policy.freshness_s+1)
    assert store.recall('Where is Sample A?')['reply']=='Sample A was last observed at bench'
