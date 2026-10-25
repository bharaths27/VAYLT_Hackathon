"""Regression tests for luna's round-4 review of PR 7 (each fails with its fix reverted; controls marked)."""
import json,threading,time
from http.server import BaseHTTPRequestHandler,HTTPServer
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.learning import Dreamer
from engram.cortex.media import FrameHub,LocalJPEG
from engram.cortex.models import ModelResult,ModelUnavailable
from engram.cortex.planning import Compiler,verify
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_dream import SOP,Stub,idle_brain

TOKEN='synthetic-test-operator-token-32-characters'
LATCH="SELECT 1 FROM meta WHERE key='cortex_stop'"
RULE_TEXT='Safety goggles must be at the ppe station before handling Sample B.'
RULE={'subject':'sample_b','prerequisite':'goggles','required_zone':'ppe_station'}
def approve(b,p): return b.approve(p['operation_id'],p['digest'],principal='carther')


# P1-1: any stop phrase of the brain's and console's vocabulary is a STOP, before intent classification.
@pytest.mark.parametrize('said',['stop moving','Stop moving!','please stop','halt','freeze','emergency','wait','pause',
                                 'hold on','hold it','hang on','whoa','abort'])
def test_any_stop_phrase_latches_the_stop_and_reaches_the_adapter(brain,said):
    p=brain.command('Go to the shelf b',principal='carther')
    r=brain.command(said,principal='carther')
    assert r['kind']=='stop' and brain.robot.stopped and brain._stopped.is_set() and brain.store.one(LATCH)
    assert brain.operation(p['operation_id'])['status']=='cancelled'

def test_control_hold_the_tray_is_not_a_stop(brain):
    assert brain.command('Hold the tray',principal='carther')['kind']!='stop' and not brain.robot.stopped


# P1-2: verification starts from the claw's real content: unknown fails closed, a load travels with navigation.
def test_a_pickup_needs_a_reported_empty_claw(store):
    c=Compiler(store); g=Goal('fetch',('sample_a',),'bench'); plan=c.compile(g)
    unknown=c.fixture(); assert not unknown['holding_known']
    v=verify(g,plan['steps'],unknown,[],store.policy)
    assert not v.ok and 'contents are unknown' in ' '.join(v.errors)
    assert verify(g,plan['steps'],c.fixture(symbolic=True),[],store.policy).ok        # control: a declared empty claw

@pytest.mark.parametrize('goal,step',[(Goal('goto',zone='bench'),{'op':'go_to_zone','args':{'zone':'bench'}}),
    (Goal('primitive',primitive='drive_bounded',args={'distance_m':0.1}),{'op':'drive_bounded','args':{'distance_m':0.1}})])
def test_navigation_with_a_loaded_or_unknown_claw_is_refused(store,goal,step):
    c=Compiler(store); loaded={**c.fixture(symbolic=True),'holding':'sample_b'}
    v=verify(goal,[step],loaded,[],store.policy)
    assert not v.ok and 'already holds sample_b' in ' '.join(v.errors) and v.final_state['holding']=='sample_b'
    assert not verify(goal,[step],c.fixture(),[],store.policy).ok                        # unknown content
    assert verify(goal,[step],c.fixture(symbolic=True),[],store.policy).ok              # control: empty

def test_the_brain_refuses_motion_while_the_adapter_reports_a_load_or_nothing(arena,clock):
    class NoHoldReport(FakeRobot):
        def state(self): st=super().state(); st.pop('holding'); return st
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,NoHoldReport(arena))
    with pytest.raises(Conflict,match='contents are unknown'): b.command('Go to the shelf b',principal='carther')
    b.close()
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena)); b.robot.holding='sample_b'
    with pytest.raises(Conflict,match='already holds sample_b'): b.command('Go to the shelf b',principal='carther')
    b.close()


# P1-3: what is stored accounts only for its own wording; conditions and extra clauses stay unresolved.
def test_a_conditional_is_never_resolved_by_an_unconditional_structured_rule(store):
    store.teach('SOP','Safety goggles must be at the ppe station before handling Sample B, unless a supervisor approves.',owner='carther',rules=[RULE])
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',('sample_b',),'bench'))

def test_an_extra_clause_inside_a_template_sentence_stays_unresolved(store):
    store.teach('SOP','Safety goggles must be at the ppe station before handling Sample B and keep it dry.',owner='carther',rules=[RULE])
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',('sample_b',),'bench'))

def test_template_wording_the_structured_rules_leave_out_stays_unresolved(store):
    store.teach('SOP','Tray must be at the bench before handling Sample A.',owner='carther',rules=[])
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',('sample_a',),'shelf_b'))


# P1-4: a motion prohibition that names nothing registered is scoped to everything.
@pytest.mark.parametrize('body',['Never move anything.','Do not touch any of the samples.'])
def test_an_unscoped_motion_prohibition_blocks_planning(store,body):
    store.teach('Rule',body,owner='carther')
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(Goal('fetch',('sample_a',),'bench'))

def test_control_a_scoped_prohibition_blocks_only_its_object(store):
    store.teach('Rule','Never move Sample A.',owner='carther')
    with pytest.raises(CortexError,match='prohibits'): Compiler(store).compile(Goal('fetch',('sample_a',),'bench'))
    assert Compiler(store).compile(Goal('fetch',('sample_b',),'bench'))['steps']


# P1-5: revoking the skill behind an approved plan stops the run before its next dispatch.
def test_revoking_the_skill_mid_execution_stops_the_run(arena,clock):
    s=Store(clock=clock,policy=Policy(auto_publish_verified=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    s.teach('Lab rule',RULE_TEXT,owner='carther')
    assert approve(b,b.command('Bring Sample B to the bench.',principal='carther'))['status']=='success'
    for oid in ('sample_b','goggles'): b.robot.positions[oid]='home'; s.observe(oid,'home',origin='test',mode='SIMULATED')
    again=b.command('Bring Sample B to the bench.',principal='carther'); assert again['source']=='skill'
    before=len(b.robot.calls); execute=b.robot.execute
    def revoke_after_first(op,args):
        r=execute(op,args)
        if len(b.robot.calls)==before+1: b.skills.revoke(again['skill_id'])         # revoked while the run is in flight
        return r
    b.robot.execute=revoke_after_first
    r=approve(b,again)
    assert r['status']=='aborted' and 'revoked' in r['failure'] and len(b.robot.calls)==before+1
    b.close()


# P2-6: model work stays pending (retried, backed off) until answered or explicitly abandoned.
def test_input_skipped_by_the_call_budget_is_retried(arena,clock):
    b=idle_brain(arena,clock,max_model_calls_per_dream=1); b.store.teach('SOP',SOP,owner='op')
    seen=[]; b.dreamer.qwen=Stub(lambda bundle:seen.append(bundle) or [])
    b.dreamer.cycle(); assert len(seen)==1
    clock.advance(1); note=b.store.teach('Note','The operator prefers label colour number 9.',owner='op')
    r=b.dreamer.cycle(); assert len(seen)==1 and r['model_pending']                      # budget spent: kept pending
    clock.advance(b.dreamer.model_window_s)
    b.dreamer.cycle(); assert len(seen)==2 and note['source_id'] in json.dumps(seen[1])    # the same evidence, retried
    b.close()

def test_input_is_retried_after_a_transient_model_failure(arena,clock):
    b=idle_brain(arena,clock); b.store.teach('SOP',SOP,owner='op'); calls=[]
    class Flaky:
        def complete(self,purpose,bundle,**kw):
            calls.append(bundle)
            if len(calls)==1: raise ModelUnavailable('transient')
            return ModelResult({'candidates':[]},'stub','m',0,'h',1,'p')
    b.dreamer.qwen=Flaky()
    assert b.dreamer.cycle()['model_pending'] and len(calls)==1
    assert b.dreamer.cycle()['status']=='backoff'
    clock.advance(31); b.dreamer.cycle(); assert len(calls)==2 and calls[1]==calls[0]
    assert b.dreamer.cycle()['status']=='no_new_evidence'                                # answered: complete now
    b.close()

def test_a_persistently_failing_input_is_abandoned_explicitly(arena,clock):
    b=idle_brain(arena,clock,max_model_calls_per_dream=10); b.store.teach('SOP',SOP,owner='op'); n=[0]
    class Down:
        def complete(self,*a,**k): n[0]+=1; raise ModelUnavailable('down')
    b.dreamer.qwen=Down()
    for _ in range(Dreamer.MODEL_ATTEMPTS): b.dreamer.cycle(); clock.advance(3601)
    assert n[0]==Dreamer.MODEL_ATTEMPTS
    assert b.store.one("SELECT 1 FROM cx_decision WHERE stage='dream_model' AND detail LIKE 'input abandoned%'")
    assert b.dreamer.cycle()['status']=='no_new_evidence'
    b.close()


# P2-7: the rolling call cap is kept in the vault and survives a restart.
def test_the_model_call_cap_survives_a_restart(tmp_path,arena,clock):
    def open_brain():
        s=Store(tmp_path/'db',clock=clock,policy=Policy(idle_after_s=.01));s.register_arena(arena); return Brain(s,FakeRobot(arena))
    b=open_brain(); b.store.teach('SOP',SOP,owner='op'); stub=Stub(lambda bundle:[]); b.dreamer.qwen=stub
    for i in range(2):
        clock.advance(1); b.store.teach(f'Note {i}',f'The operator prefers label colour number {i}.',owner='op'); time.sleep(.02); b.dreamer.cycle()
    assert stub.calls==2; b.close()
    b=open_brain(); stub2=Stub(lambda bundle:[]); b.dreamer.qwen=stub2
    clock.advance(1); b.store.teach('Note 2','The operator prefers label colour number 2.',owner='op'); time.sleep(.02)
    assert b.dreamer.cycle()['model_pending'] and stub2.calls==0                       # the cap held across the restart
    clock.advance(b.dreamer.model_window_s); time.sleep(.02); b.dreamer.cycle(); assert stub2.calls==1
    b.close()


# P2-8: the server takes its operator token from a file, so no command line ever carries it.
def test_the_operator_token_can_come_from_a_file(brain,tmp_path,monkeypatch):
    f=tmp_path/'token'; f.write_text(TOKEN+chr(10))
    monkeypatch.delenv('CORTEX_API_TOKEN',raising=False); monkeypatch.setenv('CORTEX_API_TOKEN_FILE',str(f))
    with TestClient(create_app(brain,start_idle=False)) as c:
        assert c.get('/metrics',headers={'Authorization':'Bearer '+TOKEN}).status_code==200


# P3-9: the 'unknown' capture-time sentinel (what this API itself sends) still publishes the frame.
@pytest.mark.parametrize('header',['unknown','nan','not-a-time'])
def test_unknown_capture_time_still_publishes_the_frame(header):
    jpeg=bytes([0xff,0xd8])+b'FRAME'+bytes([0xff,0xd9])
    class Camera(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            self.send_response(200);self.send_header('X-Frame-Captured-At',header);self.send_header('Content-Length',str(len(jpeg)));self.end_headers();self.wfile.write(jpeg)
    srv=HTTPServer(('127.0.0.1',0),Camera);threading.Thread(target=srv.serve_forever,daemon=True).start()
    try:
        fetch=LocalJPEG(f'http://127.0.0.1:{srv.server_port}/frame.jpg'); data,captured=fetch()
        hub=FrameHub(fetch); hub.publish(data,captured)
        assert captured is None and hub.latest().jpeg==jpeg and hub.metadata()['freshness_basis']=='receipt_only_capture_unknown'
    finally: srv.shutdown();srv.server_close()
