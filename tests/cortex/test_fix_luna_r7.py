"""Regression tests for luna's round-7 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import json
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.planning import Compiler
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_dream import SOP,Stub,idle_brain

LATCH="SELECT 1 FROM meta WHERE key='cortex_stop'"
TA,TB='synthetic-test-operator-token-for-alice-0123','synthetic-test-operator-token-for-bob-01234567'
def approve(b,p,who='carther'): return b.approve(p['operation_id'],p['digest'],principal=who)
def dump(store):
    """Every row of every table in the vault, as lower-case text."""
    for (name,) in [tuple(r.values()) for r in store.rows("SELECT name FROM sqlite_master WHERE type='table'")]:
        for row in store.rows(f'SELECT * FROM "{name}"'): yield name,json.dumps(row,default=str).lower()


# P1-1: the durable latch stays until the adapter confirmed the reset, so a reset that failed survives a restart.
class ReplyLost(FakeRobot):
    def reset_stop(self): super().reset_stop(); raise TimeoutError('the reset reply was lost')    # after the adapter re-armed
class StuckStop(FakeRobot):
    def reset_stop(self): pass                                                                    # the adapter stays stopped

def test_a_reset_that_fails_after_rearming_keeps_the_latch_through_a_restart(tmp_path,arena,clock):
    def open_brain(robot): s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);return Brain(s,robot)
    b=open_brain(ReplyLost(arena));b.stop()
    with pytest.raises(Conflict,match='stays latched'): b.clear_stop(principal='carther')
    assert b.store.one(LATCH) and b._stopped.is_set() and b.robot.stopped                  # still latched, stopped again
    b.close();b=open_brain(FakeRobot(arena))                                               # restart; the adapter reports armed
    p=b.command('Go to the shelf b',principal='carther')
    with pytest.raises(Denied,match='stop is latched'): approve(b,p)
    assert not b.robot.calls
    b.clear_stop(principal='carther');assert approve(b,p)['status']=='success'            # a confirmed reset clears it
    b.close()

def test_a_reset_the_adapter_does_not_confirm_keeps_the_latch(store,arena):
    b=Brain(store,StuckStop(arena));b.stop()
    with pytest.raises(Conflict,match='did not confirm'): b.clear_stop(principal='carther')
    assert store.one(LATCH) and b._stopped.is_set()

class StopOnConfirm(FakeRobot):
    """The adapter, except that a STOP lands right after it confirmed the reset (between the reset and the clear)."""
    brain=None
    def state(self):
        s=super().state()
        if self.brain: b,self.brain=self.brain,None; b.stop()
        return s

def test_a_stop_arriving_after_the_reset_was_confirmed_wins(store,arena):
    b=Brain(store,StopOnConfirm(arena));b.stop();b.robot.brain=b
    with pytest.raises(Conflict,match='a stop arrived'): b.clear_stop(principal='carther')
    assert store.one(LATCH) and b._stopped.is_set() and b.robot.stopped

class StopAfterCommit:
    """The vault connection, except that a STOP lands right after the next COMMIT (the clear has committed, memory not)."""
    def __init__(self,conn,brain): self.conn,self.brain,self.armed=conn,brain,True
    def execute(self,sql,*args):
        out=self.conn.execute(sql,*args)
        if self.armed and sql=='COMMIT': self.armed=False; self.brain.stop()
        return out
    def __getattr__(self,name): return getattr(self.conn,name)

def test_guard_a_stop_landing_right_after_the_clearing_commit_keeps_the_event_set(brain):
    brain.stop(); w=StopAfterCommit(brain.store.conn,brain); brain.store.conn=w
    try: brain.clear_stop(principal='carther')                                             # cleared, then stopped again
    except Conflict: pass                                                                  # (or refused, as before)
    brain.store.conn=w.conn
    assert not w.armed and brain._stopped.is_set() and brain.store.one(LATCH) and brain.robot.stopped


# P1-2: a handling limitation said without a modal or hazard word is an unresolved constraint, never accepted silently.
# (Round 8: "Sample A weighs 30 kg", "Sample A is sharp" are facts, and "It is too heavy to carry" names nothing: they
# block nothing now; test_fix_luna_r8 pins that.)
LIMITS=['Sample A is too heavy to carry.','Sample A is too hot to touch.','Sample A is too big for the claw.',
        'Carry Sample A upright.','Lift Sample A with both hands.','Hold Sample A by the cap.',
        'Sample A does not fit in the claw.','No lifting of Sample A.','Nobody moves Sample A.',
        'Handle Sample A gently, one at a time.','Sample A is a biohazard.']
@pytest.mark.parametrize('sentence',LIMITS)
def test_an_unparsed_handling_limitation_needs_structured_review(brain,sentence):
    assert brain.store.teach('Note',sentence,owner='carther')['unresolved_rules'],sentence
    with pytest.raises(Missing,match='structured review'): brain.command('Bring Sample A to the bench.',principal='carther')

@pytest.mark.parametrize('sentence',['Safety goggles must be at the ppe station before handling Sample B gently.',
                                     'Heavy safety goggles must be at the ppe station before handling Sample B.'])
def test_a_limitation_inside_a_prerequisite_clause_is_not_consumed_with_it(brain,sentence):
    x=brain.store.teach('SOP',sentence,owner='carther')
    assert x['rules'] and x['unresolved_rules'],x                                          # the rule, and the limitation open
    with pytest.raises(Missing,match='structured review'): brain.command('Bring Sample B to the bench.',principal='carther')

@pytest.mark.parametrize('sentence',['Sample A is too fragile to lift.','Never carry it by the lid.'])
def test_guard_limitations_with_hazard_or_deontic_wording_stay_caught(store,sentence):
    assert store.teach('Note',sentence,owner='carther')['unresolved_rules']
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',('sample_a',),'bench'))

@pytest.mark.parametrize('sentence',['Sample A is stored in fridge 2.','Safety goggles are kept at the ppe station.',
    'The example operator prefers concise explanations that include the relevant source.',
    'In this synthetic fixture, prepare the inspection means bring Sample B to the bench while satisfying the current lab rules.'])
def test_control_plain_notes_and_task_definitions_add_no_constraint(store,sentence):
    assert not store.teach('Note',sentence,owner='carther')['unresolved_rules']

def test_control_a_reviewers_ban_covers_a_limitation_about_exactly_that_object(store):
    assert not store.teach('Note','Sample A is too heavy to carry.',owner='carther',rules=[],prohibitions=['sample_a'])['unresolved_rules']
    with pytest.raises(CortexError,match='prohibits'): Compiler(store).compile(Goal('fetch',('sample_a',),'bench'))


# P2-3: a commitment is its owner's request; scheduling one adds no shared prohibition or unresolved constraint.
def test_a_scheduled_request_creates_no_shared_constraint(brain):
    brain.schedule('the tray arrives','do not move Sample B',principal='alice')
    brain.schedule('the bell rings','bring Sample B to the bench if it is clean',principal='alice')
    assert not brain.store.prohibitions() and not brain.store.rows('SELECT 1 FROM cx_unresolved')
    assert brain.command('Bring Sample B to the bench.',principal='bob')['status']=='proposed'   # was: denied for everyone
    assert 'error' in brain.trigger('the bell rings',event_id='ev1',principal='alice')[0]       # her wake is refused as a request
    with pytest.raises(CortexError,match='only its own request'):
        brain.store.teach('Rule','Never move Sample B.',owner='alice',kind='future',prohibitions=['sample_b'])

def test_the_api_does_not_teach_a_source_as_a_commitment(brain):
    with TestClient(create_app(brain,tokens={TA:'alice'},start_idle=False)) as c:
        r=c.post('/memory/sources',json={'title':'Rule','text':'Never move Sample B.','kind':'future'},headers={'Authorization':'Bearer '+TA})
        assert r.status_code==403 and 'reserved' in r.json()['detail']
    assert not brain.store.rows("SELECT 1 FROM source WHERE kind='future'")


# P2-4: deleting a source removes the text its plans carried: plan, step arguments, goal, and idle memories of them.
ANNOUNCE={'announce the result':{'kind':'primitive','primitive':'speak','args':{'text':'zqxmarker phrase'}}}
def test_deleting_a_source_removes_the_text_its_plans_carried(tmp_path,arena,clock):
    s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    src=s.teach('Announcement','To announce the result, say the zqxmarker phrase.',owner='carther',goal_definitions=ANNOUNCE)
    p=b.command('announce the result',principal='carther');assert 'zqxmarker' in json.dumps(p['plan'])
    assert approve(b,p)['status']=='success' and b.dreamer.cycle(force=True)['status']=='complete'
    copies={name for name,row in dump(s) if 'zqxmarker' in row}                           # the premise: copies exist
    assert {'episode','cx_episode','operation_event','cx_derived','cx_practice','skill_version'}<=copies,copies
    summary=s.one("SELECT * FROM cx_derived WHERE kind='summary'")
    s.revoke_source(src['source_id'],delete=True)
    for name,row in dump(s): assert 'zqxmarker' not in row,(name,row)
    assert b.operation(p['operation_id'])['plan'] is None
    again=s.derive('summary',json.loads(summary['payload_json']),json.loads(summary['refs_json']),origin='idle_consolidation',
                   verified=True,verifier='episodic-summary-v1')
    assert again['status']=='VERIFIED' and again['id']!=summary['id']                    # a purged memory never stands in
    b.close()

def test_deleting_a_source_removes_its_text_from_episodes_that_never_got_a_plan(tmp_path,arena,clock):
    for o in arena['objects']:
        if o['id']=='sample_a':o['aliases']=['zqxsample']                                # a request word only an alias can carry
    s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    long={'recite the notice':{'kind':'primitive','primitive':'speak','args':{'text':'zqxmarker '+'x'*290}}}   # too long to speak
    notice=s.teach('Notice','The notice is long.',owner='carther',goal_definitions=long)
    with pytest.raises(CortexError): b.command('recite the notice',principal='carther')  # planning fails: no plan cites it
    s.teach('Rule','Never move Sample A.',owner='carther')
    t=b.schedule('the zqxtrigger arrives','bring zqxsample to the bench',principal='carther')
    assert 'error' in b.trigger('the zqxtrigger arrives',event_id='ev1',principal='carther')[0]   # its proposal was refused
    assert any('zqxmarker' in row for name,row in dump(s) if name=='episode') and any('zqxsample' in row for name,row in dump(s) if name=='episode')
    s.revoke_source(notice['source_id'],delete=True)
    s.revoke_source(s.one('SELECT source_id FROM prospective_task WHERE id=?',(t['task_id'],))['source_id'],delete=True)
    for name,row in dump(s):
        if name not in {'object_binding','cx_entity','cx_station'}: assert 'zqxmarker' not in row and 'zqxsample' not in row,(name,row)
    b.close()

def test_a_proposal_whose_plan_was_purged_can_no_longer_be_approved(brain):
    src=brain.store.teach('Announcement','To announce the result, say the phrase.',owner='carther',goal_definitions=ANNOUNCE)
    p=brain.propose(Goal.from_dict(ANNOUNCE['announce the result']),'Speak the phrase',principal='carther')   # no alias ref
    brain.store.revoke_source(src['source_id'],delete=True)
    assert brain.operation(p['operation_id'])['status']=='invalidated'
    with pytest.raises(Conflict): approve(brain,p)


# P2-5: the dream input is compared before the call budget; an answered or unaskable input lets the watermark advance.
def dreaming(arena,clock,cap):
    b=idle_brain(arena,clock);b.store.teach('SOP',SOP,owner='op')
    stub=Stub(lambda bundle:[]);b.dreamer.qwen=stub;b.dreamer.model_calls_per_window=cap
    return b,stub
def runs(b): return b.store.one('SELECT COUNT(*) n FROM cx_dream')['n']

def test_an_answered_input_with_the_call_budget_spent_adds_no_run_per_tick(arena,clock):
    b,stub=dreaming(arena,clock,1)
    assert b.dreamer.cycle()['status']=='complete' and stub.calls==1                      # answered: the window's one call
    b.schedule('the tray arrives','bring Sample A to the bench',principal='op')           # new state, not in the model input
    n=runs(b)
    for _ in range(5): b.dreamer.cycle()
    assert runs(b)==n+1 and stub.calls==1                                                  # was: one more run every tick
    clock.advance(b.dreamer.model_window_s+1);b.dreamer.cycle()
    assert runs(b)==n+1 and stub.calls==1                                                  # nor asked again once the budget frees
    b.close()

def test_a_zero_call_cap_never_keeps_the_dreamer_running(arena,clock):
    b,stub=dreaming(arena,clock,0)
    for _ in range(5): b.dreamer.cycle()
    clock.advance(b.dreamer.model_window_s+1)
    for _ in range(3): b.dreamer.cycle()
    assert runs(b)==1 and stub.calls==0                                                    # was: a run every tick, for ever
    b.close()

def test_a_new_input_waits_for_the_call_budget_without_a_run_per_tick(arena,clock):
    b,stub=dreaming(arena,clock,1)
    b.dreamer.cycle();assert stub.calls==1
    clock.advance(1);b.store.teach('Note','The operator prefers label colour number 1.',owner='op')   # a new model input
    n=runs(b)
    for _ in range(5): b.dreamer.cycle()
    assert runs(b)==n+1 and stub.calls==1                                                  # deferred, not re-run every tick
    clock.advance(b.dreamer.model_window_s)                                                # the oldest call left the window
    b.dreamer.cycle();assert stub.calls==2                                                 # asked as soon as the budget allows
    b.close()


# P2-7: /metrics and derived memories show a principal its own episode records only.
def test_metrics_and_derived_memories_are_scoped_to_the_principal(arena,clock):
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    with TestClient(create_app(b,tokens={TA:'alice',TB:'bob'},start_idle=False)) as c:
        A,B={'Authorization':'Bearer '+TA},{'Authorization':'Bearer '+TB}
        p=c.post('/commands',json={'text':'Bring Sample A to the bench.'},headers=A).json()
        assert c.post(f"/operations/{p['operation_id']}/approve",json={'digest':p['digest']},headers=A).json()['status']=='success'
        assert c.post('/dream/compile',headers=A).json()['status']=='complete'           # a summary quoting her request
        ids=lambda h:[e['id'] for e in c.get('/metrics',headers=h).json()['episodes']]
        assert p['operation_id'] in ids(A) and p['operation_id'] not in ids(B)
        quotes=lambda data:'bring sample a' in json.dumps(data).lower()
        assert quotes(c.get('/memory/derived',headers=A).json()) and not quotes(c.get('/memory/derived',headers=B).json())
        q=c.post('/memory/query',json={'goal':'show me sample a'},headers=B).json()       # no typed goal: recent memories
        assert not quotes(q.get('derived'))
    b.close()
