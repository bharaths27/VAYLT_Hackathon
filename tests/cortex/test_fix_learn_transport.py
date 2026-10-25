"""Regression tests for model HTTP transport: no proxy (6), separate pools (7), no stacking (D), wire key order (A)."""
import json,socket,threading,time
from http.server import BaseHTTPRequestHandler,HTTPServer
import pytest
import urllib.request
import engram.cortex.models as M
from engram.cortex.models import IntentAdvisor,ModelUnavailable,QwenClient,request_json
from engram.cortex.types import *

THINK='<think>\n\n</think>\n\n'
def chatml(messages): # what llama-server's Qwen3 jinja template does with enable_thinking=False
    return ''.join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages)+'<|im_start|>assistant\n'+THINK
PLAN_A=[{'op':'locate_tag','args':{'object_id':'sample_a'}},{'op':'pick_tagged_object','args':{'object_id':'sample_a'}},{'op':'place_at_zone','args':{'zone':'bench'}}]

class Fake:
    def __init__(self,steps=PLAN_A,template=chatml,model='stub'):
        self.calls=[];self.steps=steps;self.template=template;self.model=model
    def __call__(self,url,body,headers,timeout):
        self.calls.append((url.rsplit('/',1)[-1],body))
        if url.endswith('/apply-template'):return {'prompt':self.template(body['messages'])}
        if url.endswith('/tokenize'):return {'tokens':list(range(50))}
        return {'model':self.model,'content':json.dumps({'steps':self.steps}),'stop_type':'eos','timings':{'predicted_n':9}}

def serve(handler_fn):
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            raw=self.rfile.read(int(self.headers['Content-Length']))
            code,value=handler_fn(self.path,raw)
            body=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    srv=HTTPServer(('127.0.0.1',0),H);t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
    return srv

def wait_local_slot_free(deadline=5):
    end=time.monotonic()+deadline
    while time.monotonic()<end:
        if M._SLOTS['local'].acquire(blocking=False): M._SLOTS['local'].release(); return True
        time.sleep(.01)
    return False

# --- 6: no environment proxy ----------------------------------------------------------------
def test_local_model_request_ignores_http_proxy(monkeypatch):
    seen=[]
    proxy=socket.socket();proxy.bind(('127.0.0.1',0));proxy.listen(1);proxy.settimeout(3)
    def accept():
        try:c,_=proxy.accept()
        except OSError:return
        seen.append(c.recv(65536));body=b'{"prompt":"FROM-PROXY"}'
        c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body);c.close()
    threading.Thread(target=accept,daemon=True).start()
    srv=serve(lambda path,raw:(200,{'prompt':'FROM-SERVER'}))
    for k in ('NO_PROXY','no_proxy'): monkeypatch.delenv(k,raising=False)
    monkeypatch.setenv('HTTP_PROXY',f'http://127.0.0.1:{proxy.getsockname()[1]}'); monkeypatch.setenv('http_proxy',f'http://127.0.0.1:{proxy.getsockname()[1]}')
    monkeypatch.setattr(urllib.request,'proxy_bypass',lambda host:False)
    try:
        r=request_json(f'http://127.0.0.1:{srv.server_port}/apply-template',{'messages':[{'role':'user','content':'PRIVATE SOP TEXT'}]},{},3)
        assert r=={'prompt':'FROM-SERVER'} and not seen
    finally: srv.shutdown();srv.server_close();proxy.close()

# --- 7: a stalled cloud advisor never blocks the local model ---------------------------------
def test_stalled_advisor_never_makes_qwen_capacity_busy(monkeypatch):
    release=threading.Event()
    def blocking(url,payload,headers,timeout):
        if 'typesafe' in url: release.wait(5); raise OSError('dns stall')
        return Fake()(url,payload,headers,timeout)
    monkeypatch.setattr(M,'_request_json_blocking',blocking)
    try:
        monkeypatch.setenv('TYPESAFE_INTENT_MODEL','test-intent-model')
        adv=IntentAdvisor(enabled=True,api_key='TEST_NOT_A_KEY',timeout_s=.1)
        for _ in range(3): assert adv.route('synthetic',disclosure_allowed=True)['status']=='unavailable'
        out=QwenClient(timeout_s=2).complete('plan',{},goal=Goal('fetch',('sample_a',),'bench').to_dict())
        assert out.value['steps']==PLAN_A
    finally: release.set()

# --- D: never stack a new local request on an abandoned one ----------------------------------
def test_abandoned_local_request_is_not_stacked(monkeypatch):
    release=threading.Event(); sent=[]
    def blocking(url,payload,headers,timeout):
        sent.append(url.rsplit('/',1)[-1])
        if url.endswith('/completion') and len(sent)<=3: release.wait(5)
        return Fake()(url,payload,headers,timeout)
    monkeypatch.setattr(M,'_request_json_blocking',blocking)
    q=QwenClient(timeout_s=.2); g=Goal('fetch',('sample_a',),'bench').to_dict()
    try:
        with pytest.raises(ModelUnavailable,match='deadline'): q.complete('plan',{},goal=g)
        start=time.monotonic()
        with pytest.raises(ModelUnavailable,match='not stacking'): q.complete('plan',{},goal=g)
        assert time.monotonic()-start<.15 and sent==['apply-template','tokenize','completion']
    finally: release.set()
    assert wait_local_slot_free()
    assert q.complete('plan',{},goal=g).value['steps']==PLAN_A

# --- A: request key order preserved on the wire (op before args) -------------------------------
def test_wire_keeps_schema_property_order_op_before_args():
    bodies={}
    def handle(path,raw):
        bodies[path]=raw.decode()
        if path=='/apply-template':return 200,{'prompt':'P'+THINK}
        if path=='/tokenize':return 200,{'tokens':[1,2]}
        return 200,{'content':json.dumps({'steps':PLAN_A}),'model':'m'}
    srv=serve(handle)
    try:
        assert wait_local_slot_free()
        QwenClient(endpoint=f'http://127.0.0.1:{srv.server_port}').complete('plan',{},goal=Goal('fetch',('sample_a',),'bench').to_dict())
    finally: srv.shutdown();srv.server_close()
    raw=bodies['/completion']; schema=raw[raw.index('"json_schema"'):]
    assert schema.index('"op"')<schema.index('"args"')
    assert raw.index('"prompt"')<raw.index('"n_predict"')                                     # not sorted

