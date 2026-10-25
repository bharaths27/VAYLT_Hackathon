"""Regression tests: commands are episodes, not sources; recipes/skills dedupe; hot path does not grow (finding 8)."""
import statistics,time
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store

SOP='Safety goggles must be at the ppe station before handling Sample B.'
def counts(s): return {t:s.one('SELECT COUNT(*) n FROM '+t)['n'] for t in ('source','source_chunk','skill_version','cx_derived')}

def test_commands_are_not_recalled_as_knowledge(brain):
    sources=counts(brain.store)['source']
    for _ in range(3):
        p=brain.command('Bring Sample A to bench',principal='carther'); brain.reject(p['operation_id'],principal='carther')
    assert counts(brain.store)['source']==sources
    r=brain.command('What do you know about sample A?',principal='carther')
    assert r['kind']=='answer' and 'Bring Sample A' not in r['reply']
    assert all('Bring Sample A' not in h['text'] for h in r['memories']['external'])
    assert len(r['memories']['episodic'])==3                                # still remembered as episodes

def test_repeated_identical_commands_dedupe_recipes_and_skills(brain):
    brain.store.teach('Rule',SOP,owner='carther')
    first=None
    for request in ['Bring Sample B to bench']*15+['Open the claw']*15:
        p=brain.command(request,principal='carther'); brain.reject(p['operation_id'],principal='carther')
        if first is None: first=counts(brain.store)
    after=counts(brain.store)
    assert after['source']==first['source']==1                               # only the taught rule
    assert after['skill_version']==2 and brain.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='task_recipe'")['n']==2

def test_propose_cost_and_rows_do_not_grow_with_repeated_commands(arena,clock):
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena)); s.teach('Rule',SOP,owner='op')
    times=[]; rows=None
    for i in range(500):
        t=time.perf_counter(); p=b.command('Bring Sample B to bench',principal='op'); times.append(time.perf_counter()-t)
        b.reject(p['operation_id'],principal='op'); clock.advance(1)
        if i==0: rows=counts(s)
    early,late=statistics.median(times[10:60]),statistics.median(times[-50:])
    assert counts(s)==rows, 'source/skill/derived rows grew per repeated identical command'
    assert late<2*early, f'propose time grew from {early*1000:.1f} ms to {late*1000:.1f} ms'
    b.close()
