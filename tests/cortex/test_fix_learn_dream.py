"""Regression tests for the idle Dreamer (findings 4, 10, 12)."""
import time
from engram.cortex.models import ModelResult
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *

SOP='Safety goggles must be at ppe station before handling Sample B.'

class Stub:
    def __init__(self,make,delay=0):self.calls=0;self.make=make;self.delay=delay
    def complete(self,purpose,bundle,**kw):
        self.calls+=1; time.sleep(self.delay)
        return ModelResult({'candidates':self.make(bundle)},'stub','m',0,'h',1,'p')

def idle_brain(arena,clock,**policy):
    s=Store(clock=clock,policy=Policy(idle_after_s=.01,**policy));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    time.sleep(.03); return b

def test_bad_candidates_are_isolated_and_the_model_is_not_called_every_tick(arena,clock):
    b=idle_brain(arena,clock); b.store.teach('SOP',SOP,owner='op')
    stub=Stub(lambda bundle:[{'text':'No refs at all.','refs':[]},{'text':'   ','refs':[bundle['allowed_refs'][0]]},
                             {'text':'Goggles near the bench may help.','refs':[bundle['allowed_refs'][0]]}])
    b.dreamer.qwen=stub
    first=b.dreamer.cycle()
    assert first['status']=='complete' and len(first['errors'])>=2
    rows=b.store.rows("SELECT status FROM cx_derived WHERE kind='hypothesis'")
    assert [r['status'] for r in rows]==['CANDIDATE']                      # the valid sibling survived
    statuses=[b.dreamer.cycle()['status'] for _ in range(6)]
    assert stub.calls==1 and set(statuses)=={'no_new_evidence'}
    b.close()

def test_failed_cycle_backs_off_instead_of_retrying_every_tick(arena,clock,monkeypatch):
    b=idle_brain(arena,clock); b.store.teach('SOP',SOP,owner='op')
    def boom(*a,**k): raise RuntimeError('injected')
    monkeypatch.setattr(b.dreamer.skills,'create',boom)
    assert b.dreamer.cycle()['status']=='failed'
    assert b.dreamer.cycle()['status']=='backoff'
    clock.advance(31)
    monkeypatch.undo()
    assert b.dreamer.cycle()['status']=='complete'
    assert b.store.one("SELECT 1 FROM meta WHERE key='cx_dream_backoff'") is None
    b.close()

def test_model_calls_are_capped_per_time_window(arena,clock):
    b=idle_brain(arena,clock); b.store.teach('SOP',SOP,owner='op')
    stub=Stub(lambda bundle:[]); b.dreamer.qwen=stub
    for i in range(6):
        b.store.teach(f'Note {i}',f'The operator prefers label colour number {i}.',owner='op')   # new evidence every cycle
        assert b.dreamer.cycle()['status']=='complete'
    assert stub.calls==b.store.policy.max_model_calls_per_dream==2
    b.close()

def test_late_model_result_is_kept_unless_foreground_interrupted(arena,clock):
    b=idle_brain(arena,clock,dream_budget_s=.05); b.store.teach('SOP',SOP,owner='op')
    b.dreamer.qwen=Stub(lambda bundle:[{'text':'Late but valid.','refs':[bundle['allowed_refs'][0]]}],delay=.1)  # Pi-like latency
    r=b.dreamer.cycle()
    assert r['status']=='complete' and b.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='hypothesis'")['n']==1
    b.close()

def test_hypothesis_cap_counts_only_live_rows(arena,clock):
    b=idle_brain(arena,clock); b.store.teach('SOP',SOP,owner='op')
    with b.store.tx():
        for i in range(130):
            b.store.conn.execute('INSERT INTO cx_derived VALUES(?,?,?,?,?,?,?,?,?,?)',(f'old{i}','hypothesis','STALE','{}','[]','',f'fp{i}','test',None,0))
    stub=Stub(lambda bundle:[{'text':'Fresh idea.','refs':[bundle['allowed_refs'][0]]}]); b.dreamer.qwen=stub
    b.dreamer.cycle()
    assert stub.calls==1 and b.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='hypothesis' AND status='CANDIDATE'")['n']==1
    b.close()

def test_practice_cap_is_rolling_not_lifetime(arena,clock):
    b=idle_brain(arena,clock); b.store.teach('SOP',SOP,owner='op')
    with b.store.tx():
        for i in range(130):
            b.store.conn.execute('INSERT INTO cx_practice VALUES(?,?,?,?,?,?,?,?)',(f'old{i}','{}','{}','{}','PASS','symbolic-verifier-v1','SIMULATED',clock()-2*86400))
    assert b.dreamer.cycle()['practice_ids']
    b.close()

def test_practice_backlog_is_not_starved_by_the_watermark(arena,clock):
    b=idle_brain(arena,clock,dream_batch=1)
    b.store.teach('SOP1',SOP,owner='op'); b.store.teach('SOP2','Tray must be at the bench before handling Sample A.',owner='op')
    done=[len(b.dreamer.cycle().get('practice_ids',[])) for _ in range(3)]
    assert done[:2]==[1,1] and b.store.one('SELECT COUNT(*) n FROM cx_practice')['n']==2
    assert b.dreamer.cycle()['status']=='no_new_evidence'
    b.close()

def test_legacy_running_episode_does_not_block_dreaming(brain):
    brain.store.teach('SOP',SOP,owner='op')
    with brain.store.tx():
        brain.store.conn.execute("INSERT INTO episode(id,goal,request,outcome,started_at) VALUES('legacy','legacy','legacy','running',?)",(brain.store.clock(),))
    assert brain.dreamer.cycle(force=True)['status']=='complete'
