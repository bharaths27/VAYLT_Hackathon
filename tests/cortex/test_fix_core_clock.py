"""Regression tests: an untrusted clock step is recoverable by an operator and never storms timers."""
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *

@pytest.fixture
def timed(arena, clock):
    s = Store(clock=clock, policy=Policy(clock_trusted=True)); s.register_arena(arena)
    b = Brain(s, FakeRobot(arena))
    yield b
    b.close()

def waiting(b): return b.store.one("SELECT COUNT(*) n FROM prospective_task WHERE status='waiting'")['n']


def test_fix_core_backward_clock_step_recovers_after_operator_attestation(timed, clock):
    clock.advance(3600); timed.sweep_due()                   # wrong-forward clock (e.g. bad NTP)
    clock.advance(-3600)                                     # corrected
    t = timed.schedule_at(clock()+5, 'Bring Sample A to bench', principal='carther')
    clock.advance(10)
    for _ in range(2):
        with pytest.raises(Stale): timed.sweep_due()
    with pytest.raises(CortexError): timed.reestablish_time(principal='carther', note=' ')
    r = timed.reestablish_time(principal='carther', note='Checked against the lab reference clock')
    assert r['previous_utc'] > r['utc'] and r['reestablished_by'] == 'carther'
    out = timed.sweep_due()
    assert [x['task_id'] for x in out] == [t['task_id']] and out[0]['proposal']['status'] == 'proposed'
    ev = timed.store.one("SELECT detail,at FROM operation_event WHERE op='clock_reestablished'")
    assert ev['detail'].startswith('carther: ') and ev['at'] == clock()
    assert not timed.robot.calls


def test_fix_core_forward_clock_jump_wakes_bounded_overdue_rest_wait(timed, clock):
    timed.CATCHUP_MAX = cap = 2
    timed.sweep_due()                                        # trusted baseline
    for i in range(5): timed.schedule_at(clock()+60*(i+1), 'Bring Sample A to bench', principal='carther')
    clock.advance(2*86400)                                   # e.g. an RTC-less boot then a large NTP step
    out = timed.sweep_due()
    assert len(out) == cap and waiting(timed) == 5-cap
    clock.advance(1)
    with pytest.raises(Stale, match='forward'): timed.sweep_due()
    assert waiting(timed) == 5-cap             # the rest wait for the operator
    timed.reestablish_time(principal='carther', note='NTP step verified')
    assert len(timed.sweep_due()) == 5-cap and waiting(timed) == 0
    assert not timed.robot.calls


def test_fix_core_reestablish_time_api_route(timed, clock):
    token = 'synthetic-test-operator-token-32-characters'
    timed.sweep_due(); clock.advance(-60)
    with pytest.raises(Stale): timed.sweep_due()
    with TestClient(create_app(timed, tokens={token: 'carther'}, start_idle=False)) as c:
        assert c.post('/clock/reestablish', json={'reason': 'x'}).status_code == 401
        r = c.post('/clock/reestablish', json={'reason': 'Checked against GPS'}, headers={'Authorization': 'Bearer '+token})
        assert r.status_code == 200 and r.json()['reestablished_by'] == 'carther'
    assert timed.sweep_due() == []
