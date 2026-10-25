import copy,json,random,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
import pytest
from fastapi.testclient import TestClient
from engram.cortex.sensors import ingest
from engram.cortex.types import *
from engram.cortex.planning import Compiler,verify
from engram.cortex.simulation import Result
from engram.cortex.models import QwenClient
from engram.cortex.api import create_app

TOKEN='operator-token-'+('x'*32);SENSOR='sensor-token-'+('y'*32)
def packet(clock,**updates):
    return dict(object_id='sample_a',zone='bench',captured_at=float(clock()),boot_id='boot-1',sequence=1,frame_id='frame-10',calibration_id='calibration-tested',**updates)
def test_sensor_identity_and_dedupe(store,clock):
    p=packet(clock)
    with pytest.raises(Denied):ingest(store,p,sensor_principal='browser',runtime_mode='PHYSICAL')
    a=ingest(store,p,sensor_principal='sensor:camera',runtime_mode='PHYSICAL')
    assert ingest(store,p,sensor_principal='sensor:camera',runtime_mode='PHYSICAL')['duplicate']
    with pytest.raises(Conflict):ingest(store,{**p,'zone':'home'},sensor_principal='sensor:camera',runtime_mode='PHYSICAL')
    assert store.one('SELECT COUNT(*) n FROM observation')['n']==1
    assert a['observation_id']
def test_old_sensor_packet_keeps_original_time(store,clock):
    p=packet(clock)
    ingest(store,p,sensor_principal='sensor:camera',runtime_mode='PHYSICAL')
    ingest(store,{**p,'sequence':0,'zone':'home','captured_at':clock()-100},sensor_principal='sensor:camera',runtime_mode='PHYSICAL')
    o=next(x for x in store.objects() if x['id']=='sample_a')
    assert o['last_zone']=='bench' and o['last_seen_at']==p['captured_at']
def test_sensor_api_is_not_operator_or_motor_credential(brain,clock):
    app=create_app(brain,tokens={TOKEN:'carther'},sensor_tokens={SENSOR:'sensor:camera'},start_idle=False)
    with TestClient(app) as c:
        assert c.post('/observations/sensor',json=packet(clock),headers={'Authorization':'Bearer '+TOKEN}).status_code==401
        assert c.post('/observations/sensor',json=packet(clock),headers={'Authorization':'Bearer '+SENSOR}).status_code==200
        assert c.post('/commands',json={'text':'Open the claw'},headers={'Authorization':'Bearer '+SENSOR}).status_code==401
        assert not brain.robot.calls

def test_inspect_requires_actual_returned_evidence(brain):
    p=brain.command('Inspect Sample A',principal='carther')
    assert p['status']=='proposed'
    brain.robot.execute=lambda op,args: Result('done','fake successful ACK')
    out=brain.approve(p['operation_id'],p['digest'],principal='carther')
    assert out['status']=='unknown'
def test_fake_primitive_ack_does_not_establish_state(brain):
    p=brain.command('Close the claw',principal='carther')
    brain.robot.execute=lambda op,args: Result('done','ACK only')
    out=brain.approve(p['operation_id'],p['digest'],principal='carther')
    assert out['status']=='unknown'

def test_legacy_episodes_are_not_claimed_by_new_executor(brain):
    with brain.store.tx():
        brain.store.conn.execute("INSERT INTO episode(id,goal,request,outcome,started_at) VALUES('legacy','legacy','legacy','proposed',?)",(brain.store.clock(),))
    assert brain.pending()==[]
    brain.stop()
    assert brain.store.one("SELECT outcome FROM episode WHERE id='legacy'")['outcome']=='proposed'

@pytest.mark.parametrize('seed',range(12))
def test_varied_prerequisite_graphs_match_independent_final_state(store,seed):
    rng=random.Random(seed);zone=rng.choice(['ppe_station','shelf_b','home'])
    store.teach('generated fixture','Symbolic rule fixture, not observed physics.',owner='tester',rules=[
      {'subject':'sample_b','prerequisite':'goggles','required_zone':zone}])
    g=Goal('fetch',('sample_b',),'bench');c=Compiler(store);p=c.compile(g)
    v=verify(g,p['steps'],c.fixture(),store.related_rules(g),store.policy)
    assert v.ok
    placements=[s['args']['zone'] for s in p['steps'] if s['op']=='place_at_zone']
    assert placements[-1]=='bench' and v.final_state['positions']['goggles']==zone
    wrong=copy.deepcopy(p['steps'])
    for step in reversed(wrong):
        if step['op']=='place_at_zone':step['args']['zone']='shelf_b';break
    assert not verify(g,wrong,c.fixture(),store.related_rules(g),store.policy).ok

def test_actual_loopback_http_qwen_contract_round_trip(store):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path,data))
            value={'prompt':'TEMPLATE:'+data['messages'][0]['content']} if self.path=='/apply-template' else {'tokens':[1,2,3]} if self.path=='/tokenize' else {'content':'{"steps":[{"op":"gripper_open","args":{}}]}','model':'stub-not-real-Qwen'}
            body=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        q=QwenClient(endpoint=f'http://127.0.0.1:{server.server_port}')
        result=q.complete('plan',store.bundle('Open the claw'),goal=Goal('primitive',primitive='gripper_open').to_dict())
        assert result.contract_hash==q.contract_hash
        assert calls[0][1]['messages'][0]['content'].startswith(q.contract)
        assert calls[-1][1]['prompt']==calls[1][1]['content']
        assert result.model=='stub-not-real-Qwen'
    finally:server.shutdown();server.server_close();thread.join()
