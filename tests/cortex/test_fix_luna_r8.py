"""Regression tests for luna's round-8 review of PR 7 and the warden's scoping rule (each fails with its fix reverted;
controls/guards marked)."""
import asyncio,json,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from engram.cortex.api import create_app
from engram.cortex.learning import Dreamer
from engram.cortex.models import ModelUnavailable,QwenClient
from engram.cortex.planning import Compiler
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_dream import SOP,Stub,idle_brain
from .test_fix_learn_transport import THINK,wait_local_slot_free
from .test_fix_luna_r7 import TA,dump

LATCH="SELECT 1 FROM meta WHERE key='cortex_stop'"
TS='synthetic-test-sensor-token-for-the-camera-01'
def approve(b,p): return b.approve(p['operation_id'],p['digest'],principal='carther')
def fetch(b,who='Sample A'): return b.command(f'Bring {who} to the bench.',principal='carther')


# P1-1: only an explicit `stopped: False` confirms a reset; an adapter that does not report its stop state gets no motion.
class Unreported(FakeRobot):
    """An adapter whose state omits the stop field, and whose reset does nothing."""
    def state(self): s=super().state(); s.pop('stopped'); return s
    def reset_stop(self): pass

def test_a_reset_the_adapter_does_not_report_keeps_the_latch(store,arena):
    b=Brain(store,Unreported(arena));b.stop()
    with pytest.raises(Conflict,match='does not report'): b.clear_stop(principal='carther')
    assert store.one(LATCH) and b._stopped.is_set()

def test_an_adapter_that_does_not_report_its_stop_state_gets_no_motion(store,arena):
    b=Brain(store,Unreported(arena));p=b.command('Go to the shelf b',principal='carther')
    with pytest.raises(Denied,match='does not report'): approve(b,p)
    assert not b.robot.calls


# P1-2 (warden): a handling prohibition or limitation is scoped to what it names and blocks only plans that handle it;
# naming nothing, a statement blocks everything only as an instruction about handling or motion that may mean any
# object; a declarative fact blocks nothing.
@pytest.mark.parametrize('sentence',['Leave Sample A alone.','Leave Sample A untouched.','Leave Sample A where it is.','Hands off Sample A.'])
def test_a_handling_prohibition_refuses_plans_that_handle_what_it_names_and_only_those(brain,sentence):
    assert brain.store.teach('Note',sentence,owner='carther')['unresolved_rules']
    with pytest.raises(Missing,match='structured review'): fetch(brain)                    # was: planned to move it
    assert fetch(brain,'Sample B')['status']=='proposed'
    assert brain.command('Find Sample A',principal='carther')['status']=='proposed'        # looking handles nothing

@pytest.mark.parametrize('sentence',['It is hot today.','Fridge 2 holds Sample A.','Sample A weighs 30 kg.','Sample A is sharp.',
                                     'The tray has a handle.'])                       # a noun, not a handling verb
def test_a_plain_fact_blocks_nothing(brain,sentence):
    assert not brain.store.teach('Note',sentence,owner='carther')['unresolved_rules']
    assert fetch(brain)['status']=='proposed' and brain.command('Go to the shelf b',principal='carther')['status']=='proposed'

@pytest.mark.parametrize('sentence',['It is too heavy to carry.','Lunch is served daily except Sunday.','Always wear a lab coat.',
                                     'Put your bag in the locker.'])
def test_a_statement_naming_no_object_it_could_mean_freezes_nothing(brain,sentence):
    x=brain.store.teach('Note',sentence,owner='carther')
    assert x['unresolved_rules'] and 'blocks nothing' in x['unresolved_rules'][0],x       # listed for review
    assert fetch(brain)['status']=='proposed' and brain.command('Go to the shelf b',principal='carther')['status']=='proposed'

@pytest.mark.parametrize('sentence',['Never lift anything.','Carry it upright.','It cannot be lifted.'])
def test_an_instruction_about_handling_that_may_mean_any_object_blocks_every_handling_plan(brain,sentence):
    brain.store.teach('Note',sentence,owner='carther')
    for target in ('sample_a','sample_b','tray'):
        with pytest.raises(Missing,match='structured review'): Compiler(brain.store).compile(Goal('fetch',(target,),'bench'))
    assert brain.command('Go to the shelf b',principal='carther')['status']=='proposed'    # it lifts nothing: it may drive

@pytest.mark.parametrize('sentence',['Do not go there.',"Don't move anything heavy."])
def test_guard_an_instruction_about_motion_that_may_mean_anything_blocks_every_plan(brain,sentence):
    brain.store.teach('Note',sentence,owner='carther')
    with pytest.raises(Missing,match='structured review'): brain.command('Go to the shelf b',principal='carther')
    with pytest.raises(Missing,match='structured review'): fetch(brain)


# P2-3: deleting a source removes its title, a reviewer's own rule keys, and the request and search row of every
# episode built from it (a task alias's name too).
def test_deleting_a_source_removes_its_title_rule_keys_and_the_requests_built_from_it(tmp_path,arena,clock):
    s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    rule={'subject':'sample_b','prerequisite':'goggles','required_zone':'ppe_station','rule_key':'zqxkey'}
    src=s.teach('zqxtitle notice','Safety goggles must be at the ppe station before handling Sample B.',owner='carther',
                rules=[rule],goal_definitions={'zqxalias please':{'kind':'goto','zone':'bench'}})
    assert b.command('zqxalias please',principal='carther')['status']=='proposed'          # the alias's own name
    assert fetch(b,'Sample B')['status']=='proposed'                                        # a plan citing its rule
    copies={name for name,row in dump(s) if 'zqx' in row}
    assert {'source','cx_rule','episode','cx_episode_fts_content'}<=copies,copies          # the premise
    s.revoke_source(src['source_id'],delete=True)
    for name,row in dump(s): assert 'zqx' not in row,(name,row)
    b.close()


# P2-4: an idle (dream) call yields to a foreground plan on the same client: its request is closed, the plan is served.
def test_a_foreground_plan_preempts_a_background_dream_on_the_same_client():
    started,release=threading.Event(),threading.Event()
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/apply-template': out={'prompt':'P'+THINK}
            elif self.path=='/tokenize': out={'tokens':[1,2,3]}
            elif 'candidates' in json.dumps(body.get('json_schema')):                     # the dream: a long inference
                started.set(); release.wait(30); out={'content':json.dumps({'candidates':[]}),'model':'m'}
            else: out={'content':json.dumps({'steps':[{'op':'gripper_open','args':{}}]}),'model':'m'}
            data=json.dumps(out).encode()
            try: self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except OSError: pass                                                           # the preempted client left
    srv=ThreadingHTTPServer(('127.0.0.1',0),H);srv.daemon_threads=True
    threading.Thread(target=srv.serve_forever,daemon=True).start()
    q=QwenClient(endpoint=f'http://127.0.0.1:{srv.server_port}',timeout_s=60);errors=[]
    def dream():
        try: q.complete('dream',{'rules':[]})
        except ModelUnavailable as e: errors.append(str(e))
    try:
        assert wait_local_slot_free()
        t=threading.Thread(target=dream);t.start();assert started.wait(10)
        t0=time.monotonic()
        out=q.complete('plan',{},goal=Goal('primitive',primitive='gripper_open').to_dict())   # was: ModelUnavailable at once
        assert out.value['steps']==[{'op':'gripper_open','args':{}}] and time.monotonic()-t0<10
        t.join(10);assert not t.is_alive() and errors and 'preempted' in errors[0],errors
    finally:
        release.set();srv.shutdown();srv.server_close()
    assert wait_local_slot_free()


def test_a_dream_call_that_yielded_to_foreground_work_is_no_failed_attempt(arena,clock):
    b=idle_brain(arena,clock);b.store.teach('SOP',SOP,owner='op')
    class Yielding:
        def complete(self,purpose,bundle,**kw):
            self.work=b.activity.foreground();self.work.__enter__()                       # an interactive request arrives
            raise ModelUnavailable('model request preempted by a foreground request')
    q=Yielding();b.dreamer.qwen=q
    r=b.dreamer.cycle();q.work.__exit__(None,None,None)
    assert r['status']=='yielded' and r['model_pending'] and b.dreamer._model_state()['attempts']==0   # was: 1 attempt
    b.close()


# P3-6: the live-hypothesis cap holds for every accepted candidate, not once per cycle.
def test_hypotheses_never_exceed_the_live_cap(arena,clock):
    b=idle_brain(arena,clock);src=b.store.teach('SOP',SOP,owner='op')
    with b.store.tx():
        for i in range(Dreamer.LIVE_CAP-1):
            b.store.conn.execute('INSERT INTO cx_derived VALUES(?,?,?,?,?,?,?,?,?,?)',(f'h{i}','hypothesis','CANDIDATE','{}',
                json.dumps(['source:'+src['source_id']]),'',f'fp{i}','test',None,clock()))
    b.dreamer.qwen=Stub(lambda bundle:[{'text':f'Distinct idea {i}.','refs':[bundle['allowed_refs'][0]]} for i in range(3)])
    b.dreamer.cycle()
    assert b.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='hypothesis' AND status IN ('CANDIDATE','VERIFIED')")['n']==Dreamer.LIVE_CAP
    b.close()


# P3-7: the outer guard checks the route's role before reading one body byte (a wrong-role chunked request).
def test_a_credential_of_the_wrong_role_is_refused_before_any_body_byte(brain):
    app=create_app(brain,tokens={TA:'alice'},sensor_tokens={TS:'sensor:camera'},start_idle=False)
    def call(path,token):
        reads,sent=[],[]
        async def receive(): reads.append(1); return {'type':'http.request','body':b'x'*400000,'more_body':False}
        async def send(m): sent.append(m)
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','path':path,'raw_path':path.encode(),
               'root_path':'','scheme':'http','query_string':b'','client':('127.0.0.1',1),'server':('127.0.0.1',80),
               'headers':[(b'authorization',('Bearer '+token).encode()),(b'transfer-encoding',b'chunked')]}
        asyncio.run(app(scope,receive,send))
        return sent[0]['status'],reads
    assert call('/commands',TS)==(401,[])                     # was: the body read (here 413, for its size) first
    assert call('/observations/sensor',TA)==(401,[])
