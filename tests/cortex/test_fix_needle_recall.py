"""Needle-benchmark fix (H:/_scratch/needle-bench): a location is answered from object state only while that state is
current; an unobserved or stale object falls through to the cited evidence. The first test fails on d22c851."""
import pytest
from engram.cortex.types import *


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
