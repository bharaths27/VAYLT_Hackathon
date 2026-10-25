"""/goals/propose: displayed text can never differ from the goal being approved (review finding 9)."""
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
TOKEN='synthetic-test-operator-token-32-characters'
FETCH_A={'kind':'fetch','targets':['sample_a'],'zone':'bench'}

@pytest.fixture
def client(brain):
    with TestClient(create_app(brain,tokens={TOKEN:'carther'},start_idle=False)) as c:
        c.headers['Authorization']='Bearer '+TOKEN;yield c

def test_unparsed_text_is_a_note_and_the_card_shows_the_typed_goal(client):
    note='do the usual thing with the blue rack, carefully'
    r=client.post('/goals/propose',json={'goal':FETCH_A,'request':note,'idempotency_key':'k1'});assert r.status_code==200
    p=r.json()
    assert p['plan']['goal']=='Bring Sample A to bench' and FETCH_A.items()<=p['plan']['goal_contract'].items()   # was: the free text shown as the goal
    assert p['operator_note']==note and p['request']!=note
    assert client.get('/operations/'+p['operation_id']).json()['plan']['goal']=='Bring Sample A to bench'
    again=client.post('/goals/propose',json={'goal':FETCH_A,'request':note,'idempotency_key':'k1'}).json();assert again['operation_id']==p['operation_id']
    notes=[e for e in client.get('/events').json() if e['op']=='operator_note' and e['episode_id']==p['operation_id']]
    assert [e['detail'] for e in notes]==[note]

def test_parsed_text_must_equal_the_typed_goal(client):
    ok=client.post('/goals/propose',json={'goal':FETCH_A,'request':'Bring Sample A to bench'}).json()
    assert ok['plan']['goal']=='Bring Sample A to bench' and 'operator_note' not in ok
    assert client.post('/goals/propose',json={'goal':FETCH_A,'request':'Bring Sample B to bench'}).status_code==409
    assert client.post('/goals/propose',json={'goal':FETCH_A,'request':"Don't bring Sample A to bench"}).status_code==403
