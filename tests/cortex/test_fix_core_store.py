"""Regression tests for the vault transaction and ownership fixes."""
import sqlite3
import pytest
from engram.cortex.types import *


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
