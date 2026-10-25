"""Regression tests: revoking a procedure blocks proposals copied from it."""
import pytest
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *

@pytest.fixture
def skill_brain(arena, clock):
    s = Store(clock=clock, policy=Policy(auto_publish_verified=True)); s.register_arena(arena)
    b = Brain(s, FakeRobot(arena))
    b.command('Go to the bench', principal='carther')                 # publishes a verified procedure
    p = b.command('Go to the bench', principal='carther', idempotency_key='k2')
    assert p['source'] == 'skill' and p['skill_id']
    yield b, p
    b.close()


def test_fix_core_approval_refused_after_skill_revoked(skill_brain):
    b, p = skill_brain
    b.skills.revoke(p['skill_id'])                                    # as POST /skills/{id}/revoke does
    with pytest.raises(Stale, match='revoked'):
        b.approve(p['operation_id'], p['digest'], principal='carther')
    assert not b.robot.calls and b.operation(p['operation_id'])['status'] == 'proposed'


def test_fix_core_invalidation_withdraws_proposal_from_revoked_skill(skill_brain):
    b, p = skill_brain
    b.skills.revoke(p['skill_id'])
    b.store.invalidate()
    assert b.operation(p['operation_id'])['status'] == 'invalidated'
    assert p['operation_id'] not in [o['operation_id'] for o in b.pending()]
