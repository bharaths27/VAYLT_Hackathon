"""Regression tests for luna's round-3 review of PR 7 (each fails with its fix reverted; controls marked)."""
import json,sqlite3
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.learning import Skills
from engram.cortex.planning import Compiler,parse_goal
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_dream import SOP,Stub,idle_brain

TOKEN='synthetic-test-operator-token-32-characters'
LATCH="SELECT 1 FROM meta WHERE key='cortex_stop'"
def approve(b,p): return b.approve(p['operation_id'],p['digest'],principal='carther')

class FailingCommit:
    """The vault connection, except that the next COMMIT is refused (a full disk, an I/O error)."""
    def __init__(self,conn): self.conn=conn; self.armed=True
    def execute(self,sql,*args):
        if self.armed and sql=='COMMIT': self.armed=False; raise sqlite3.OperationalError('disk I/O error')
        return self.conn.execute(sql,*args)
    def __getattr__(self,name): return getattr(self.conn,name)


# P1-1: storage trouble never swallows the hardware STOP.
def test_hardware_stop_goes_out_even_when_the_latch_cannot_be_stored(brain):
    p=brain.command('Go to the shelf b',principal='carther')
    def disk_full(reason): raise sqlite3.OperationalError('database or disk is full')
    brain._latch=disk_full
    with pytest.raises(sqlite3.OperationalError): brain.stop()                 # the storage error still surfaces...
    assert brain.robot.stopped and brain._stopped.is_set()                     # ...after the adapter received STOP
    with pytest.raises(Denied,match='stop is latched'): approve(brain,p)


# P1-2: re-arming follows the durable transition, never precedes it; approval reads the durable latch.
def test_a_refused_commit_while_clearing_keeps_the_robot_stopped(brain):
    brain.stop(); fc=FailingCommit(brain.store.conn); brain.store.conn=fc
    with pytest.raises(sqlite3.OperationalError): brain.clear_stop(principal='carther')
    brain.store.conn=fc.conn
    assert brain.robot.stopped and brain._stopped.is_set() and brain.store.one(LATCH)
    p=brain.command('Go to the shelf b',principal='carther')
    with pytest.raises(Denied,match='stop is latched'): approve(brain,p)
    assert not [c for c in brain.robot.calls if c[0]=='go_to_zone']

def test_approval_reads_the_durable_latch_not_only_memory(brain):
    brain.stop(); brain._stopped.clear(); brain.robot.reset_stop()             # memory and adapter disagree with the vault
    p=brain.command('Go to the shelf b',principal='carther')
    with pytest.raises(Denied,match='stop is latched'): approve(brain,p)

def test_a_stop_arriving_while_the_latch_is_cleared_wins(brain):
    brain.stop(); reset=brain.robot.reset_stop
    def reset_then_stop(): reset(); brain.stop()                               # a STOP lands right after the durable clear
    brain.robot.reset_stop=reset_then_stop
    with pytest.raises(Conflict,match='stays stopped'): brain.clear_stop(principal='carther')
    assert brain.robot.stopped and brain._stopped.is_set() and brain.store.one(LATCH)


# P1-3: a structured rule resolves only the sentence it structures.
RULE={'subject':'sample_b','prerequisite':'goggles','required_zone':'ppe_station'}
def test_a_structured_rule_does_not_hide_another_safety_sentence_about_the_same_object(store):
    store.teach('SOP','Safety goggles must be at the ppe station before handling Sample B. Sample B should not be moved while wet.',owner='carther',rules=[RULE])
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',('sample_b',),'bench'))

def test_control_the_structured_sentence_alone_is_resolved(store):
    store.teach('SOP','Before anyone handles Sample B, the goggles have to be at the ppe station.',owner='carther',rules=[RULE])
    assert Compiler(store).compile(Goal('fetch',('sample_b',),'bench'))['steps']


# P2-4: conditionals are detected whatever `rules` says, and scoped to stations as well as objects.
@pytest.mark.parametrize('rules',[None,[]])
@pytest.mark.parametrize('body',['Shelf B is not to be used unless a supervisor approves.',
                                 'Shelf B can be used provided that a supervisor approves.'])   # no safety wording: only the conditional
def test_a_station_conditional_requires_clarification_before_planning_there(brain,rules,body):
    brain.store.teach('Shelf note',body,owner='carther',rules=rules)
    with pytest.raises(Missing,match='structured review'): brain.command('Go to the shelf b',principal='carther')
    assert brain.command('Go to the bench',principal='carther')['status']=='proposed'

def test_a_motion_conditional_naming_nothing_registered_is_scoped_to_everything(brain):
    brain.store.teach('Note','Lunch is served daily except Sunday.',owner='carther')            # control: benign, scopes nothing
    assert brain.command('Go to the bench',principal='carther')['status']=='proposed'
    brain.store.teach('Rule','Nothing may be moved unless a supervisor approves.',owner='carther')
    with pytest.raises(Missing,match='structured review'): brain.command('Go to the bench',principal='carther')


# P2-5: an alias two objects share is refused, never read as "both of them".
def test_an_alias_two_objects_share_is_refused_not_read_as_both(arena,clock):
    for o in arena['objects']:
        if o['id'] in ('sample_a','sample_b'): o['aliases']=['sample']
    s=Store(clock=clock);s.register_arena(arena)
    with pytest.raises(Missing,match='more than one'): parse_goal('Bring the sample to the bench',s)
    assert parse_goal('Bring Sample A to the bench',s).targets==('sample_a',)
    assert set(parse_goal('Bring Sample A and Sample B to the bench',s).targets)=={'sample_a','sample_b'}
    s.close()


# P2-7: no response can be framed, the inspector's approval page included.
def test_no_response_can_be_framed(brain):
    with TestClient(create_app(brain,tokens={TOKEN:'carther'},start_idle=False)) as c:
        for r in (c.get('/cortex'),c.get('/health')):
            assert r.headers['X-Frame-Options']=='DENY' and "frame-ancestors 'none'" in r.headers['Content-Security-Policy']


# P2-8: a skill is tested from a start away from its destination, and a no-op is never competence.
def _anchored_plan(store,goal,fixture):
    plan=Compiler(store).compile(goal,fixture); plan['refs']=['episode:anchor']
    with store.tx(): store.conn.execute("INSERT OR IGNORE INTO episode(id,goal,request,outcome,started_at) VALUES('anchor','{}','x','proposed',1)")
    return plan

def test_a_goto_skill_is_tested_starting_away_from_its_destination(store):
    f=Compiler(store).fixture(); k=Skills(store); sk=k.create(_anchored_plan(store,Goal('goto',zone='bench'),f))
    rec=k.test(sk['id'],f)
    assert rec['ok'] and any(c['start_zone'] not in (None,'bench') for c in rec['cases'])

def test_a_no_op_fetch_procedure_never_becomes_a_skill(clock):
    s=Store(clock=clock);s.register_arena({'zones':['bench'],'objects':[{'id':'sample_a','name':'Sample A','tag':1}]})
    s.observe('sample_a','bench',origin='camera',mode='OPERATOR')               # one station: the goal already holds
    g=Goal('fetch',('sample_a',),'bench'); f=Compiler(s).fixture(); plan=_anchored_plan(s,g,f)
    assert [x['op'] for x in plan['steps']]==['observe']
    k=Skills(s); rec=k.test(k.create(plan)['id'],f)
    assert not rec['ok'] and 'no effect step' in json.dumps(rec)
    s.close()


# P2-9: every input of the idle hypothesis prompt is citable.
def test_the_dream_prompt_holds_only_citable_inputs(arena,clock):
    b=idle_brain(arena,clock); b.store.teach('SOP',SOP,owner='op'); b.store.observe('goggles','bench',origin='camera',mode='OPERATOR')
    b.schedule('the tray arrives','bring Sample A to the bench',principal='op')
    seen=[]; b.dreamer.qwen=Stub(lambda bundle:seen.append(bundle) or [])
    b.dreamer.cycle()
    bundle=seen[0]; allowed=set(bundle['allowed_refs'])
    cite={'rules':lambda x:'rule:'+x['rule_id'],'episodes':lambda x:'episode:'+x['id'],
          'declared_knowledge':lambda x:'source:'+x['source_id'],'source_passages':lambda x:'source:'+x['source_id']}
    assert set(bundle)==set(cite)|{'allowed_refs'}
    assert all(ref(x) in allowed for k,ref in cite.items() for x in bundle[k])
    b.close()
