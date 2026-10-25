"""The timed-task scheduler survives errors, backs off, rate-limits decisions, always cleans up (review finding 8)."""
import sqlite3,time
import pytest
from fastapi.testclient import TestClient
from engram.cortex import api
from engram.cortex.store import Store
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.types import Policy
TOKEN='synthetic-test-operator-token-32-characters'

@pytest.fixture
def timed(arena,clock,monkeypatch):
    monkeypatch.setattr(api,'SWEEP_S',.02,raising=False);monkeypatch.setattr(api,'SWEEP_MAX_S',.08,raising=False)
    s=Store(clock=clock,policy=Policy(clock_trusted=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    yield b
    b.dreamer.stop();b.close()

def decisions(b):return b.store.rows("SELECT detail FROM cx_decision WHERE stage='clock_sweep'")

def test_non_cortex_error_does_not_kill_the_loop_and_shutdown_cleans_up(timed):
    calls=[]
    def locked():calls.append(1);raise sqlite3.OperationalError('database is locked')
    timed.sweep_due=locked;stops=[];stop=timed.dreamer.stop;timed.dreamer.stop=lambda:stops.append(1) or stop()
    with TestClient(api.create_app(timed,tokens={TOKEN:'carther'},start_idle=True)):time.sleep(1.2)   # was: one call, then shutdown re-raised
    assert len(calls)>=4
    assert stops                                                              # cleanup ran (was skipped by the re-raise)
    assert [d['detail'] for d in decisions(timed)]==['OperationalError']      # recorded once, not once per attempt

def test_clock_rollback_backs_off_and_records_one_decision(timed,clock):
    with timed.store.tx():timed.store.conn.execute("INSERT INTO meta VALUES('cortex_last_utc',?)",(str(clock()+3600),))
    real,calls=timed.sweep_due,[]
    def counted():calls.append(time.monotonic());return real()
    timed.sweep_due=counted
    with TestClient(api.create_app(timed,tokens={TOKEN:'carther'},start_idle=False)):time.sleep(1.5)
    assert len(calls)>=4 and [d['detail'] for d in decisions(timed)]==['STALE_EVIDENCE']   # was ~1 row per second
    gaps=[b-a for a,b in zip(calls,calls[1:])]
    assert gaps[-1]>gaps[0]                                                    # exponential backoff
