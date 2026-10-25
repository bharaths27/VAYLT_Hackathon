"""Regression tests: episode/commitment/wake transitions go through one guarded path."""
import threading
import pytest
from engram.cortex.types import *

def make_task(b): return b.command('When calibration finishes, bring Sample A to bench.', principal='carther')
def fire(b, e='e1'): return b.trigger('calibration finishes', event_id=e, principal='carther')
def approve(b, p): return b.approve(p['operation_id'], p['digest'], principal='carther')
def task_row(b, tid): return b.store.one('SELECT status,proposal_id FROM prospective_task WHERE id=?', (tid,))


def test_fix_core_revoke_source_mid_effect_keeps_real_outcome(brain):
    t = make_task(brain)
    src = brain.store.one('SELECT source_id FROM prospective_task WHERE id=?', (t['task_id'],))['source_id']
    p = fire(brain)[0]['proposal']
    orig, seen = brain.robot.execute, {}
    def execute(op, args):
        r = orig(op, args)
        if op == 'pick_tagged_object':
            brain.store.revoke_source(src)            # operator withdraws the instruction mid-effect
            seen['task'] = task_row(brain, t['task_id'])['status']
        return r
    brain.robot.execute = execute
    r = approve(brain, p)
    assert seen['task'] == 'fired'                      # revocation did not claim a running commitment was cancelled
    assert r['status'] == 'aborted' and brain.robot.state()['holding'] == 'sample_a'
    assert task_row(brain, t['task_id'])['status'] == 'unknown'   # the real, unresolved outcome is recorded
    assert brain.store.source_state(src)['status'] == 'revoked'


def test_fix_core_revoke_source_before_approval_withdraws_commitment(brain):
    t = make_task(brain)
    src = brain.store.one('SELECT source_id FROM prospective_task WHERE id=?', (t['task_id'],))['source_id']
    p = fire(brain)[0]['proposal']
    brain.store.revoke_source(src)
    assert task_row(brain, t['task_id'])['status'] == 'cancelled'
    with pytest.raises(CortexError): approve(brain, p)
    assert not brain.robot.calls


def test_fix_core_verify_outcome_concurrent_submissions_one_wins(brain):
    p = brain.command('Bring Sample A to the bench.', principal='carther')
    brain.robot.faults['pick_tagged_object'] = 'unknown'
    assert approve(brain, p)['status'] == 'unknown'
    barrier, real = threading.Barrier(2), brain.operation
    def op(ep):                                         # widen the old check-then-write window
        r = real(ep)
        try: barrier.wait(timeout=1)
        except threading.BrokenBarrierError: pass
        return r
    brain.operation = op
    won, lost = [], []
    def run(ok):
        try: won.append(brain.verify_outcome(p['operation_id'], principal='carther', ok=ok, note=f'ok={ok}')['status'])
        except Conflict as e: lost.append(e)
    ths = [threading.Thread(target=run, args=(v,)) for v in (False, True)]
    [t.start() for t in ths]; [t.join(10) for t in ths]
    brain.operation = real
    ev = brain.store.rows("SELECT status FROM operation_event WHERE op='verify'")
    assert len(won) == 1 and len(lost) == 1 and len(ev) == 1
    assert real(p['operation_id'])['status'] == won[0] == ev[0]['status']


def test_fix_core_stop_settles_woken_commitment(brain):
    t = make_task(brain)
    p = fire(brain)[0]['proposal']
    brain.stop()
    assert brain.operation(p['operation_id'])['status'] == 'cancelled'
    assert task_row(brain, t['task_id'])['status'] == 'cancelled'   # not stuck at 'fired'


def test_fix_core_approval_racing_the_wake_link_is_recorded(brain):
    t = make_task(brain)
    real = brain.propose
    def propose(*a, **k):
        prop = real(*a, **k)
        assert approve(brain, prop)['status'] == 'success'   # operator approves from the pending list at once
        return prop
    brain.propose = propose
    out = fire(brain)
    brain.propose = real
    op = out[0]['proposal']['operation_id']
    assert task_row(brain, t['task_id']) == {'status': 'done', 'proposal_id': op}
    assert brain.store.one('SELECT status,proposal_id FROM cx_wake')== {'status': 'linked', 'proposal_id': op}


def test_fix_core_commitment_withdrawn_during_planning_never_becomes_approvable(brain):
    t = make_task(brain)
    real = brain.store.bundle
    def bundle(*a, **k):
        brain.cancel_task(t['task_id'], principal='carther')   # withdrawn while the proposal is being prepared
        return real(*a, **k)
    brain.store.bundle = bundle
    assert fire(brain) == []
    brain.store.bundle = real
    ep = brain.store.one('SELECT outcome FROM episode')
    assert ep['outcome'] == 'cancelled' and not brain.pending()
    assert task_row(brain, t['task_id'])['status'] == 'cancelled'
    assert brain.store.one('SELECT status FROM cx_wake')['status'] == 'cancelled'
    assert not brain.robot.calls
