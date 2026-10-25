"""Regression tests for the vault transaction and ownership fixes."""
import copy,json,sqlite3,subprocess,sys
from pathlib import Path
import pytest
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
ROOT=Path(__file__).resolve().parents[2]


def test_fix_core_second_owner_refused_before_any_write(tmp_path, arena, clock):
    pth = tmp_path/'db'
    s = Store(pth, clock=clock); s.register_arena(arena)
    owner = Brain(s, FakeRobot(arena))
    s.observe('sample_a', 'bench', origin='op', mode='OPERATOR')
    p = owner.command('Bring Sample A to the shelf b.', principal='carther')
    e0 = s.epoch()
    other = copy.deepcopy(arena); other['objects'][0]['name'] = 'Sample A v2'
    with pytest.raises(Conflict):
        Store(pth, clock=clock).register_arena(other)    # what a second API process does at startup
    a = next(o for o in s.objects() if o['id'] == 'sample_a')
    assert s.epoch() == e0 and a['name'] == 'Sample A' and a['last_zone'] == 'bench'
    assert owner.approve(p['operation_id'], p['digest'], principal='carther')['status'] == 'success'
    owner.close()


def test_fix_core_brain_reuses_store_lock(tmp_path, arena, clock):
    pth = tmp_path/'db'
    s = Store(pth, clock=clock); s.register_arena(arena)
    b = Brain(s, FakeRobot(arena))                       # same process: must not refuse itself
    assert s.file_lock and b._file_lock is None
    b.close()
    again = Store(pth, clock=clock); assert again.file_lock; again.close()   # close released ownership


def test_fix_core_seed_script_keeps_working_and_respects_owner(tmp_path):
    db = tmp_path/'seed.db'
    run = lambda: subprocess.run([sys.executable, str(ROOT/'scripts/cortex_seed.py'), '--db', str(db), '--reviewed-by', 'carther'],
                                 cwd=ROOT, capture_output=True, text=True, timeout=60)
    first = run()
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)['source_ids']
    held = Store(db)
    refused = run()
    assert refused.returncode != 0 and 'owns this vault' in refused.stderr
    held.close()


def test_fix_core_failed_commit_rolls_back_and_frees_connection(store):
    store.conn.execute('CREATE TABLE p(id INTEGER PRIMARY KEY)')
    store.conn.execute('CREATE TABLE c(pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)')
    with pytest.raises(sqlite3.IntegrityError):
        with store.tx():
            store.conn.execute('INSERT INTO c VALUES(42)')   # the violation surfaces only at COMMIT
    assert not store.conn.in_transaction and store._depth == 0
    assert store.one('SELECT COUNT(*) n FROM c')['n'] == 0  # nothing from the refused transaction persisted
    taught = store.teach('t', 'Sample A note', owner='x')     # the shared connection still works
    assert store.source_state(taught['source_id'])['status'] == 'accepted'
