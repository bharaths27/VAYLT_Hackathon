"""Regression tests: the stop latch is durable and unresolved effects block new motion."""
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *

def approve(b, p): return b.approve(p['operation_id'], p['digest'], principal='carther')

@pytest.fixture
def vault(tmp_path, arena, clock):
    pth = tmp_path/'db'
    def open_brain():
        s = Store(pth, clock=clock); s.register_arena(arena)
        return Brain(s, FakeRobot(arena))
    return open_brain


def test_fix_core_unknown_effect_keeps_motion_refused_after_restart(vault):
    b = vault()
    p1 = b.command('Bring Sample A to the bench.', principal='carther')
    b.robot.faults['pick_tagged_object'] = RuntimeError('link dropped')
    assert approve(b, p1)['status'] == 'unknown'
    b.close(); b = vault()                                  # process restart
    assert b.operation(p1['operation_id'])['status'] == 'unknown' and b._stopped.is_set()
    p2 = b.command('Go to the shelf b', principal='carther')
    with pytest.raises(Denied): approve(b, p2)
    assert not b.robot.calls
    b.close()


def test_fix_core_failed_effect_latch_survives_restart(vault):
    b = vault()
    p1 = b.command('Bring Sample A to the bench.', principal='carther')
    b.robot.faults['pick_tagged_object'] = 'failed'
    assert approve(b, p1)['status'] == 'failed'             # settled, but the latch it set must persist
    b.close(); b = vault()
    p2 = b.command('Go to the shelf b', principal='carther')
    with pytest.raises(Denied, match='stop is latched'): approve(b, p2)
    assert not b.robot.calls
    b.clear_stop(principal='carther')
    assert approve(b, p2)['status'] == 'success'
    b.close()


def test_fix_core_operator_stop_survives_restart(vault):
    b = vault(); b.stop(); b.close(); b = vault()
    assert b._stopped.is_set()
    p = b.command('Go to the shelf b', principal='carther')
    with pytest.raises(Denied, match='stop is latched'): approve(b, p)
    assert not b.robot.calls
    b.clear_stop(principal='carther')
    assert approve(b, p)['status'] == 'success'
    assert b.store.one("SELECT detail FROM operation_event WHERE op='clear_stop'")['detail'] == 'carther'
    b.close()


def test_fix_core_unresolved_effect_refuses_approval_without_latch(vault):
    b = vault()
    p1 = b.command('Bring Sample A to the bench.', principal='carther')
    b.robot.faults['pick_tagged_object'] = 'unknown'
    assert approve(b, p1)['status'] == 'unknown'
    with b.store.tx(): b.store.conn.execute("DELETE FROM meta WHERE key='cortex_stop'")   # a vault from before the durable latch
    b.close(); b = vault()
    assert not b._stopped.is_set()
    p2 = b.command('Go to the shelf b', principal='carther')
    with pytest.raises(Denied, match=p1['operation_id']): approve(b, p2)
    assert not b.robot.calls
    b.close()


def test_fix_core_legacy_running_episode_does_not_block_rearm(brain):
    with brain.store.tx():
        brain.store.conn.execute("INSERT INTO episode(id,goal,request,outcome,started_at) VALUES('legacy1','{}','old run','running',1)")
    brain.stop()
    brain.clear_stop(principal='carther')
    p = brain.command('Go to the shelf b', principal='carther')
    assert approve(brain, p)['status'] == 'success'


def test_fix_core_unverified_live_effect_names_blocker_and_api_reconciles(arena, clock):
    class PhysicalLike(FakeRobot):
        def state(self): return {**super().state(), 'adapter': 'test-physical'}
    s = Store(policy=Policy(live_enabled=True), clock=clock); s.register_arena(arena)
    b = Brain(s, PhysicalLike(arena))
    token = 'synthetic-test-operator-token-32-characters'
    with TestClient(create_app(b, tokens={token: 'carther'}, start_idle=False)) as c:
        c.headers['Authorization'] = 'Bearer '+token
        p1 = c.post('/commands', json={'text': 'Open the claw'}).json()
        assert c.post(f"/operations/{p1['operation_id']}/approve", json={'digest': p1['digest']}).json()['status'] == 'needs_verification'
        p2 = c.post('/commands', json={'text': 'Close the claw'}).json()
        r = c.post(f"/operations/{p2['operation_id']}/approve", json={'digest': p2['digest']})
        assert r.status_code == 403 and p1['operation_id'] in r.json()['detail']   # unverified effect blocks new motion
        calls = len(b.robot.calls)
        assert c.post('/stop').status_code == 200
        r = c.post('/robot/clear_stop')
        assert r.status_code == 403 and p1['operation_id'] in r.json()['detail']   # the UI can show what to reconcile
        v = c.post(f"/operations/{p1['operation_id']}/verify", json={'ok': True, 'note': 'Test-only operator observation, not hardware'})
        assert v.status_code == 200 and v.json()['status'] == 'success'
        assert c.post('/robot/clear_stop').status_code == 200
        p3 = c.post('/commands', json={'text': 'Close the claw', 'idempotency_key': 'after-reconcile'}).json()
        assert c.post(f"/operations/{p3['operation_id']}/approve", json={'digest': p3['digest']}).json()['status'] == 'needs_verification'
        assert len(b.robot.calls) > calls
    b.close()
