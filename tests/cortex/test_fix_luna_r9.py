"""Regression tests for luna's round-9 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import json,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.models import IntentAdvisor,ModelResult,QwenClient
from engram.cortex.planning import Compiler
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_transport import THINK,wait_local_slot_free
from .test_fix_luna_r7 import TA,TB,dump

LATCH="SELECT 1 FROM meta WHERE key='cortex_stop'"
def approve(b,p): return b.approve(p['operation_id'],p['digest'],principal='carther')


# P1-1: STOP reaches the robot at once, whatever holds the store; the durable latch follows.
class InFlight(FakeRobot):
    """A physical action in flight until STOP reaches the robot, and the time STOP reached it."""
    def __init__(self,arena): super().__init__(arena);self.dispatched=threading.Event();self.release=threading.Event();self.stop_at=None
    def execute(self,op,args):
        if op=='go_to_zone': self.dispatched.set();self.release.wait(10)
        return super().execute(op,args)
    def stop(self):
        if self.stop_at is None: self.stop_at=time.monotonic()
        super().stop();self.release.set()

def test_stop_reaches_the_robot_while_another_thread_holds_the_store(store,arena):
    b=Brain(store,InFlight(arena));p=b.command('Go to the shelf b',principal='carther')
    runner=threading.Thread(target=lambda:approve(b,p),daemon=True);runner.start();assert b.robot.dispatched.wait(5)
    held,hold=threading.Event(),threading.Event()
    def holder():
        with b.store.tx(): held.set();hold.wait(10)                                        # a stalled transaction
    h=threading.Thread(target=holder,daemon=True);h.start();assert held.wait(5)
    t0=time.monotonic();s=threading.Thread(target=b.stop,daemon=True);s.start()
    end=time.monotonic()+1
    while b.robot.stop_at is None and time.monotonic()<end: time.sleep(.002)
    reached=b.robot.stop_at;hold.set();s.join(10);h.join(10);runner.join(10)
    assert reached is not None and reached-t0<.1,reached                                    # was: only once the store freed
    assert store.one(LATCH) and b._stopped.is_set()                                         # and then the durable latch


# P1-2: what a commitment produced stays its owner's: derived memories, procedures and evidence.
def test_memories_built_from_a_commitment_stay_its_owners(brain):
    brain.schedule('the tray arrives','bring Sample A to the bench',principal='alice')
    assert brain.trigger('the tray arrives',event_id='ev1',principal='alice')[0]['proposal']['status']=='proposed'
    fsid=brain.store.one("SELECT id FROM source WHERE kind='future'")['id']
    cites=lambda items,key='refs':[x for x in items if 'source:'+fsid in (x[key] if key else x['definition']['refs'])]
    recipe=cites(brain.store.derived())                                                      # the premise: a recipe cites it
    assert recipe and cites(brain.store.bundle('anything at all',None,principal='alice')['derived'])
    assert not cites(brain.store.bundle('anything at all',None,principal='bob')['derived'])  # was: shown to bob
    assert not cites(brain.store.bundle('anything at all',None)['derived'])                  # no principal: nobody
    note=brain.store.derive('summary',{'about':'that recipe'},['derived:'+recipe[0]['id']],origin='test',verified=True,verifier='episodic-summary-v1')
    shown=lambda who:[d['id'] for d in brain.store.bundle('anything at all',None,principal=who)['derived']]
    assert note['id'] in shown('alice') and note['id'] not in shown('bob')                   # cited through a derived memory
    with TestClient(create_app(brain,tokens={TA:'alice',TB:'bob'},start_idle=False)) as c:
        assert cites(c.get('/skills',headers={'Authorization':'Bearer '+TA}).json(),None)
        assert not cites(c.get('/skills',headers={'Authorization':'Bearer '+TB}).json(),None)
    chunk=brain.store.one('SELECT id FROM source_chunk WHERE source_id=?',(fsid,))['id']
    with pytest.raises(Missing): brain.store.evidence(chunk)                                 # no principal: nobody's
    with pytest.raises(Missing): brain.store.evidence(chunk,principal='bob')
    assert brain.store.evidence(chunk,principal='alice')['text'].startswith('When the tray arrives')


# P2-3: compiler first; the local model only for a goal the compiler cannot compose, its plan checked the same way.
GOOD=[{'op':'locate_tag','args':{'object_id':'sample_a'}},{'op':'pick_tagged_object','args':{'object_id':'sample_a'}},{'op':'place_at_zone','args':{'zone':'bench'}}]
class Planner:
    """A local model that answers the given steps, counting its calls."""
    planning_order='compiler_first'
    def __init__(self,steps): self.steps=steps;self.calls=0
    def manifest(self): return {}
    def complete(self,purpose,bundle,**kw): self.calls+=1;return ModelResult({'steps':self.steps},'stub','m',0,'h',1,'p')

def model_brain(store,arena,steps): return Brain(store,FakeRobot(arena),qwen=Planner(steps))

def with_a_gap(monkeypatch):
    """A goal the compiler composes wrongly: its own independent check rejects the plan (a gap, not a refusal, which
    the compiler's checks raise before composing)."""
    monkeypatch.setattr(Compiler,'_compose',lambda self,goal,f,rules:[{'op':'observe','args':{}}],raising=False)

def test_guard_a_goal_the_compiler_composes_never_reaches_the_model(store,arena):
    b=model_brain(store,arena,GOOD);p=b.command('Bring Sample A to the bench.',principal='carther')
    assert p['source']=='composed' and b.qwen.calls==0

def test_a_compiler_gap_goes_to_the_model_whose_plan_is_checked_like_any(store,arena,monkeypatch):
    with_a_gap(monkeypatch)
    b=model_brain(store,arena,GOOD);p=b.command('Bring Sample A to the bench.',principal='carther')
    assert p['status']=='proposed' and p['source']=='model' and p['plan']['steps']==GOOD and b.qwen.calls==1   # was: refused

def test_a_compiler_gap_with_a_model_plan_the_check_rejects_is_refused(store,arena,monkeypatch):
    from engram.cortex.planning import CompilerGap
    with_a_gap(monkeypatch)
    b=model_brain(store,arena,[{'op':'observe','args':{}}])
    with pytest.raises(CompilerGap): b.command('Bring Sample A to the bench.',principal='carther')
    assert b.qwen.calls==1

def test_guard_a_refusal_is_no_gap_and_never_reaches_the_model(store,arena,monkeypatch):
    with_a_gap(monkeypatch);store.teach('Note','Leave Sample A alone.',owner='carther')
    b=model_brain(store,arena,GOOD)
    with pytest.raises(Missing,match='structured review'): b.command('Bring Sample A to the bench.',principal='carther')
    assert b.qwen.calls==0


def test_guard_a_model_plan_meets_the_cyclic_prerequisite_refusal_too(store,arena):
    for sub,pre,zone in (('sample_b','goggles','ppe_station'),('goggles','sample_b','bench')):               # a cycle
        store.teach('Rule','Cyclic.',owner='carther',rules=[{'subject':sub,'prerequisite':pre,'required_zone':zone}])
    store.observe('goggles','ppe_station',origin='camera',mode='OPERATOR');store.observe('sample_b','bench',origin='camera',mode='OPERATOR')
    steps=[{'op':'locate_tag','args':{'object_id':'sample_b'}},{'op':'pick_tagged_object','args':{'object_id':'sample_b'}},
           {'op':'place_at_zone','args':{'zone':'shelf_b'}}]                                  # passes the step check alone
    q=Planner(steps);q.planning_order='model_first';b=Brain(store,FakeRobot(arena),qwen=q)
    with pytest.raises(Conflict,match='cyclic'): b.command('Bring Sample B to the shelf b.',principal='carther')
    assert q.calls==1


# P2-4: deleting a source also purges its task aliases' episodes that never got a plan.
def test_deleting_a_source_purges_its_aliases_episodes_that_never_got_a_plan(tmp_path,arena,clock):
    s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    s.teach('Shelf note','Shelf B can be used if a supervisor approves.',owner='carther')    # refuses goals at shelf B
    s.teach('Note','Leave Sample A alone.',owner='carther')                                   # refuses handling Sample A
    src=s.teach('Shortcuts','Two shortcuts.',owner='carther',goal_definitions={'zqxgo please':{'kind':'goto','zone':'shelf_b'},
                'zqxfetch please':{'kind':'fetch','targets':['sample_a'],'zone':'bench'}})
    for alias in ('zqxgo please','zqxfetch please'):
        with pytest.raises(Missing,match='structured review'): b.command(alias,principal='carther')   # refused before any plan
    assert sum('zqx' in row for name,row in dump(s) if name=='episode')==2                  # the premise
    s.revoke_source(src['source_id'],delete=True)
    for name,row in dump(s): assert 'zqx' not in row,(name,row)
    b.close()


# P2-5: the local model endpoint is plain http, the scheme whose calls can yield (test_fix_luna_r8 preempts one).
def test_the_local_model_endpoint_is_plain_http():
    with pytest.raises(Denied,match='plain http'): QwenClient(endpoint='https://127.0.0.1:8080')
    assert QwenClient(endpoint='http://127.0.0.1:8080').endpoint=='http://127.0.0.1:8080'


# P2-6 as stated is not reproducible: each planning route reaches propose() or command(), which plan inside
# activity.foreground(). The handlers are now foreground for their whole length too; this pins the API behaviour.
def test_guard_an_api_plan_preempts_an_in_flight_dream_which_records_no_failure(arena,clock):
    started,release=threading.Event(),threading.Event()
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/apply-template': out={'prompt':'P'+THINK}
            elif self.path=='/tokenize': out={'tokens':[1,2,3]}
            elif 'candidates' in json.dumps(body.get('json_schema')): started.set();release.wait(30);out={'content':'{"candidates":[]}'}
            else: out={'content':json.dumps({'steps':[{'op':'go_to_zone','args':{'zone':'shelf_b'}}]}),'model':'m'}
            data=json.dumps(out).encode()
            try: self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except OSError: pass
    srv=ThreadingHTTPServer(('127.0.0.1',0),H);srv.daemon_threads=True;threading.Thread(target=srv.serve_forever,daemon=True).start()
    s=Store(clock=clock,policy=Policy(idle_after_s=.01));s.register_arena(arena)
    b=Brain(s,FakeRobot(arena),qwen=QwenClient(endpoint=f'http://127.0.0.1:{srv.server_port}',timeout_s=60,planning_order='model_first'))
    s.teach('Note','The operator prefers label colour number 1.',owner='carther');out=[]
    try:
        assert wait_local_slot_free()
        with TestClient(create_app(b,tokens={TA:'carther'},start_idle=False)) as c:
            t=threading.Thread(target=lambda:out.append(b.dreamer.cycle(force=True)));t.start();assert started.wait(10)
            r=c.post('/plan/goto',json={'zone':'shelf_b'},headers={'Authorization':'Bearer '+TA}).json()
            t.join(15)
        assert r['status']=='proposed' and r['source']=='model'                               # the model served the plan
        assert out and out[0]['status']=='yielded' and b.dreamer._model_state()['attempts']==0
        assert not s.one("SELECT 1 FROM meta WHERE key='cx_dream_backoff'")                  # no failure recorded
    finally:
        release.set();srv.shutdown();srv.server_close();b.close()


# P3-7: a zero advisory budget answers "exhausted"; a malformed one is refused at construction.
def test_an_advisor_with_a_zero_call_budget_answers_exhausted(monkeypatch):
    monkeypatch.setenv('TYPESAFE_INTENT_MODEL','test-intent-model');sent=[]
    a=IntentAdvisor(enabled=True,api_key='TEST_NOT_A_KEY',transport=lambda *x,**k:sent.append(x) or {},max_calls=0)
    r=a.route('bring sample a',disclosure_allowed=True)
    assert r['status']=='budget_exhausted' and r['retry_after_s'] is None and not sent      # was: IndexError
    for bad in (-1,1.5,True):
        with pytest.raises(CortexError): IntentAdvisor(max_calls=bad)
