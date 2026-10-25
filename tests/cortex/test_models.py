import json,threading
import pytest
from engram.cortex.models import *
from engram.cortex.store import Store
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.types import *

class LocalModelTransport:
    def __init__(self,steps=None,tokens=100):self.calls=[];self.tokens=tokens;self.steps=steps or [{'op':'observe','args':{}}]
    def __call__(self,url,body,headers,timeout):
        self.calls.append((url,body,dict(headers)))
        if url.endswith('/apply-template'):return {'prompt':'<system>'+body['messages'][0]['content']+'<user>'+body['messages'][1]['content']+THINK_OFF}
        if url.endswith('/tokenize'):return {'tokens':list(range(self.tokens))}
        return {'model':'test-Qwen-compatible-stub','content':json.dumps({'steps':self.steps})}

def test_qwen_gets_contract_and_current_memory_not_just_hash(tmp_path):
    contract=tmp_path/'ENGRAM.md';contract.write_text('TEST CONTRACT: observations are not hypotheses.')
    transport=LocalModelTransport(); q=QwenClient(contract_path=contract,transport=transport)
    out=q.complete('plan',{'facts':[{'required_zone':'shelf_b'}],'missing':['current location']},goal={'kind':'fetch'})
    messages=transport.calls[0][1]['messages']
    assert 'TEST CONTRACT' in messages[0]['content']
    assert 'shelf_b' in messages[1]['content'] and 'current location' in messages[1]['content']
    assert transport.calls[0][1]['chat_template_kwargs']=={'enable_thinking':False}
    assert transport.calls[1][1]['content']==transport.calls[2][1]['prompt']
    assert out.input_tokens==100 and out.contract_hash==q.contract_hash

def test_qwen_context_overflow_never_silently_truncates():
    t=LocalModelTransport(tokens=3000);q=QwenClient(input_budget=1024,transport=t)
    with pytest.raises(Missing):q.complete('plan',{'facts':['must not move']},goal={'kind':'fetch'})
    assert len(t.calls)==2

@pytest.mark.parametrize('url',['https://evil.example','http://192.168.1.1','http://localhost@evil.example','http://localhost:8080/?token=x'])
def test_qwen_network_boundary(url):
    with pytest.raises(Denied):QwenClient(endpoint=url)

@pytest.mark.parametrize('bad',[{}, {'content':'<think>ignored</think>{"steps":[]}'}, {'content':'{"steps": NaN}'}, {'content':'{"steps":[],"approve":true}'}])
def test_qwen_malformed_output_rejected(bad):
    t=LocalModelTransport()
    def transport(url,body,headers,timeout):
        if url.endswith('/completion'):return bad
        return t(url,body,headers,timeout)
    with pytest.raises(ModelUnavailable):QwenClient(transport=transport).complete('plan',{},goal={'kind':'fetch'})

def test_model_goal_error_falls_back_to_verified_composer(arena,clock):
    transport=LocalModelTransport([{'op':'drive_bounded','args':{'distance_m':.5}}])
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena),qwen=QwenClient(transport=transport,planning_order='model_first'))
    p=b.command('Bring Sample A to bench',principal='carther')
    assert p['source']=='fallback' and p['status']=='proposed'
    assert p['plan']['steps'][0]['op']=='locate_tag'
    assert not b.robot.calls;b.close()

def test_model_validated_plan_used_and_manifest_recorded(arena,clock):
    steps=[{'op':'locate_tag','args':{'object_id':'sample_a'}},{'op':'pick_tagged_object','args':{'object_id':'sample_a'}},{'op':'place_at_zone','args':{'zone':'bench'}}]
    s=Store(clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena),qwen=QwenClient(transport=LocalModelTransport(steps),planning_order='model_first'))
    p=b.command('Bring Sample A to bench',principal='carther')
    assert p['source']=='model' and p['plan']['steps']==steps
    assert s.one("SELECT * FROM cx_manifest WHERE id='last_qwen_request'")
    b.close()

TEST_MODEL='test-intent-model'
@pytest.fixture(autouse=True)
def intent_model_env(monkeypatch): monkeypatch.setenv('TYPESAFE_INTENT_MODEL',TEST_MODEL)   # the model id comes only from the environment

class AdvisoryTransport:
    def __init__(self):self.calls=[]
    def __call__(self,url,body,headers,timeout):
        self.calls.append((url,body,headers))
        values={'negated':0.01,'ambiguous':0.01,'unsupported':0.01,'fits':.99,'sufficient':.99,'practice':.99,'conflict':.01,'complete':.99,'correction':.01,'directed':.99,'recall':.01,'future':.01}
        return {'model':TEST_MODEL,'answers':{k:{'type':'noul','noul':values[k]} for k in body['questions']}}

def test_intent_disabled_private_or_keyless_never_calls():
    t=AdvisoryTransport()
    for enabled,key,allowed in [(False,'test',True),(True,'test',False),(True,'',True)]:
        r=IntentAdvisor(enabled=enabled,api_key=key,transport=t).route('synthetic',disclosure_allowed=allowed)
        assert r['status']!='ok'
    assert not t.calls

def test_intent_model_id_comes_from_env_only(monkeypatch):
    t=AdvisoryTransport()
    monkeypatch.delenv('TYPESAFE_INTENT_MODEL',raising=False)
    r=IntentAdvisor(enabled=True,api_key='TEST',transport=t).route('synthetic',disclosure_allowed=True)
    assert r['status']=='unavailable' and r['reason']=='model not configured' and not t.calls
    monkeypatch.setenv('TYPESAFE_INTENT_MODEL',TEST_MODEL)
    assert IntentAdvisor(enabled=True,api_key='TEST',transport=t).route('synthetic',disclosure_allowed=True)['status']=='ok'
    assert t.calls[0][1]['model']==TEST_MODEL

@pytest.mark.parametrize('invalid',[{}, {'model':'other','answers':{}}, {'model':TEST_MODEL,'answers':{'score':{'type':'noul','noul':float('nan')}}}, {'model':TEST_MODEL,'answers':{'score':{'type':'noul','noul':True}}}, {'model':TEST_MODEL,'answers':{'score':{'type':'noul','noul':1.2}}}])
def test_intent_invalid_is_unavailable_not_authorized(invalid):
    j=IntentAdvisor(enabled=True,api_key='TEST_NOT_A_KEY',transport=lambda *args:invalid)
    r=j.evaluate('candidate_review',{}, {'score':'test'},disclosure_allowed=True)
    assert r['status']=='unavailable' and r['scores']=={}

def test_intent_failure_does_not_leak_credential():
    key='TEST_SECRET_SENTINEL'
    def broken(*a):raise RuntimeError('bad '+key)
    r=IntentAdvisor(enabled=True,api_key=key,transport=broken).route('synthetic',disclosure_allowed=True)
    assert key not in json.dumps(r) and r['status']=='unavailable'

def test_intent_budget_is_bounded():
    t=AdvisoryTransport();j=IntentAdvisor(enabled=True,api_key='TEST',transport=t,max_calls=1)
    assert j.route('synthetic',disclosure_allowed=True)['status']=='ok'
    assert j.route('synthetic',disclosure_allowed=True)['status']=='budget_exhausted'
    assert len(t.calls)==1

def test_all_advisory_hooks_have_typed_responses_without_authority():
    t=AdvisoryTransport();j=IntentAdvisor(enabled=True,api_key='TEST',transport=t)
    for stage in sorted(j.STAGES):
        r=j.evaluate(stage,{'synthetic':True},{'fits':'Does this match the evidence?'},disclosure_allowed=True)
        assert r['status']=='ok' and r['authority'] is False
    assert len(t.calls)==len(j.STAGES)

def test_cloud_permitted_flow_exercises_route_relevance_review_selection(arena,clock):
    t=AdvisoryTransport();j=IntentAdvisor(enabled=True,api_key='TEST',transport=t)
    s=Store(clock=clock,policy=Policy(cloud_enabled=True,auto_publish_verified=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena),intent=j)
    s.teach('Public synthetic SOP','Safety goggles must be at ppe station before handling Sample B.',owner='carther',cloud_ok=True)
    b.command('Bring Sample B to bench',principal='carther',cloud_allowed=True)
    b.command('Bring Sample B to bench',principal='carther',cloud_allowed=True)
    b.dreamer.cycle(force=True)
    stages={r['stage'] for r in s.rows('SELECT stage FROM cx_decision')}
    assert {'route','relevance','candidate_review','skill_selection','dream_priority'}<=stages
    assert not b.robot.calls;b.close()

def test_dns_or_transport_stall_has_wall_clock_deadline(monkeypatch):
    import engram.cortex.models as module
    import time
    release=threading.Event()
    def stalled(*args):release.wait(1);return {'ok':True}
    monkeypatch.setattr(module,'_request_json_blocking',stalled)
    start=time.monotonic()
    try:
        with pytest.raises(ModelUnavailable):module.request_json('https://api.typesafe.ai/v1/systemone',{}, {},.03)
        assert time.monotonic()-start<.5
    finally:release.set()
