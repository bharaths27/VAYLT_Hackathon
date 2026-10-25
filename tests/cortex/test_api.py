import time
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.media import FrameHub
from engram.cortex.types import *
TOKEN='synthetic-test-operator-token-32-characters'
OTHER='second-synthetic-test-token-32-characters'
@pytest.fixture
def client(brain):
    app=create_app(brain,tokens={TOKEN:'carther',OTHER:'other'},start_idle=False)
    with TestClient(app) as c:
        c.headers['Authorization']='Bearer '+TOKEN
        yield c

def test_auth_required_and_body_identity_not_authority(client,brain):
    p=client.post('/commands',json={'text':'Bring Sample A to bench','who':'not-the-owner'}).json()
    assert p['owner']=='carther'
    assert p['plan']['goal']=='Bring Sample A to bench' and p['plan']['goal_contract']['kind']=='fetch'
    assert client.get('/memory/derived',headers={'Authorization':'bad'}).status_code==401
    assert client.post('/operations/'+p['operation_id']+'/approve',json={'digest':p['digest'],'approved_by':'carther'},headers={'Authorization':'Bearer '+OTHER}).status_code==403
    assert not brain.robot.calls

def test_full_api_source_memory_plan_approval_recall(client,brain):
    src=client.post('/memory/sources',json={'title':'SOP','text':'Safety goggles must be at ppe station before handling Sample B.'})
    assert src.status_code==200
    p=client.post('/commands',json={'text':'Bring Sample B to bench'}).json()
    assert p['status']=='proposed' and p['check']['physical']
    assert client.get('/operations/pending').json()[0]['operation_id']==p['operation_id']
    r=client.post('/operations/'+p['operation_id']+'/approve',json={'digest':p['digest']})
    assert r.status_code==200 and r.json()['outcome']=='success'
    recall=client.post('/memory/recall',json={'question':'Where is Sample B?'})
    assert recall.json()['kind']=='answer'
    assert client.get('/memory/derived').json()

def test_api_future_cancel_idempotent_event(client):
    t=client.post('/prospective',json={'trigger':'ready','request':'Bring Sample A to bench'}).json()
    assert client.post('/prospective/'+t['task_id']+'/cancel').status_code==200
    assert client.post('/events/trigger',json={'trigger':'ready','event_id':'x'}).json()==[]

def test_api_reject_is_persistent(client):
    p=client.post('/commands',json={'text':'Bring Sample A to bench'}).json()
    assert client.post('/operations/'+p['operation_id']+'/reject').json()['status']=='cancelled'
    assert client.get('/operations/pending').json()==[]

def test_api_returns_current_package_and_seven_roles(client):
    p=client.get('/memory/package').json(); assert len(p['sha256'])==64
    assert p['model_prompt_enabled'] is False
    r=client.post('/memory/recall',json={'question':'memory status'}).json()
    assert set(r['memories'])==set(MEMORY_KINDS)

def test_api_mutations_reject_cross_site(client):
    assert client.post('/commands',json={'text':'hello'},headers={'Sec-Fetch-Site':'cross-site'}).status_code==403

def test_api_invalid_extra_fields_no_effect(client,brain):
    assert client.post('/commands',json={'text':'Open claw','approve':True}).status_code==422
    assert not brain.robot.calls

def test_no_camera_is_honest(client):
    assert not client.get('/camera/status').json()['available']
    assert client.get('/frame.jpg').status_code==503

def test_live_frame_bytes_metadata_not_timer_ticks(brain):
    hub=FrameHub(lambda:(b'\xff\xd8TEST\xff\xd9',None),fps=10)
    app=create_app(brain,tokens={TOKEN:'carther'},frame_hub=hub,start_idle=False)
    with TestClient(app) as c:
        c.headers['Authorization']='Bearer '+TOKEN
        hub.publish(b'\xff\xd8TEST\xff\xd9')
        r=c.get('/frame.jpg');assert r.status_code==200 and r.content.startswith(b'\xff\xd8')
        assert r.headers['X-Frame-Captured-At']=='unknown'
        assert c.get('/camera/status').json()['buffered_frames']==1

def test_missing_auth_configuration_fails_closed(brain):
    with pytest.raises(Denied):create_app(brain,tokens={'short':'operator'})

def test_a_blank_command_is_accepted_and_answered_as_misheard(client):
    r=client.post('/commands',json={'text':''})
    assert r.status_code==200 and (r.json()['classification'],r.json()['needs_clarification'])==('misheard',True)
