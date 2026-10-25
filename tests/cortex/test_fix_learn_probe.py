"""cortex_model_probe.py records WHERE a model call failed, not just the exception type (finding 11, E)."""
import hashlib,importlib.util,json,sys,threading,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
import engram.cortex.models as M
ROOT=Path(__file__).resolve().parents[2]
THINK='<think>\n\n</think>\n\n'
STEPS=[{'op':'locate_tag','args':{'object_id':'goggles'}},{'op':'pick_tagged_object','args':{'object_id':'goggles'}},{'op':'place_at_zone','args':{'zone':'ppe_station'}},
       {'op':'locate_tag','args':{'object_id':'sample_b'}},{'op':'pick_tagged_object','args':{'object_id':'sample_b'}},{'op':'place_at_zone','args':{'zone':'bench'}}]

def run_probe(tmp_path,monkeypatch,mode,extra=()):
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            code,value=200,{}
            if self.path=='/apply-template': value={'prompt':'P'+('' if mode=='nojinja' else THINK)}
            elif self.path=='/tokenize': value={'tokens':[1,2,3]}
            elif mode=='http500': code,value=500,{'error':'boom'}
            else: value={'content':json.dumps({'steps':STEPS}),'model':'/home/pi/models/q.gguf','stop_type':'eos','timings':{'predicted_n':40},'truncated':False}
            body=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    srv=HTTPServer(('127.0.0.1',0),H);threading.Thread(target=srv.serve_forever,daemon=True).start()
    spec=importlib.util.spec_from_file_location('cortex_model_probe',ROOT/'scripts/cortex_model_probe.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    out=tmp_path/f'{mode}.json'
    monkeypatch.setattr(sys,'argv',['probe','--qwen','--url',f'http://127.0.0.1:{srv.server_port}','--out',str(out),*extra])
    slot=getattr(M,'_SLOTS',{}).get('local'); end=time.monotonic()+5   # earlier tests may still drain the local slot
    while slot and not slot.acquire(blocking=False) and time.monotonic()<end: time.sleep(.01)
    if slot: slot.release()
    try: mod.main()
    finally: srv.shutdown();srv.server_close()
    return json.loads(out.read_text())['qwen'], json.loads(out.read_text())

def test_probe_records_template_stage_when_thinking_is_not_disabled(tmp_path,monkeypatch,capsys):
    runs,_=run_probe(tmp_path,monkeypatch,'nojinja')
    assert runs[0]['status']=='failed' and runs[0]['trace']['stage']=='template' and runs[0]['trace']['thinking_disabled'] is False
    assert '--jinja' in runs[0]['reason']

def test_probe_records_http_status_and_completion_stage(tmp_path,monkeypatch,capsys):
    runs,_=run_probe(tmp_path,monkeypatch,'http500')
    assert runs[0]['trace']['stage']=='completion' and runs[0]['trace']['http_status']==500 and runs[0]['trace']['thinking_disabled'] is True

def test_probe_records_stop_type_timings_and_model_file_name_not_path(tmp_path,monkeypatch,capsys):
    gguf=tmp_path/'q.gguf'; gguf.write_bytes(b'not a real model')
    runs,doc=run_probe(tmp_path,monkeypatch,'ok',('--model-file',str(gguf)))
    r=runs[0]
    assert r['status']=='response_received' and r['model']=='q.gguf' and r['candidate_model_file_sha256']==hashlib.sha256(b'not a real model').hexdigest() and r['model_identity_verified'] is False
    assert r['trace']['stop_type']=='eos' and r['trace']['timings']=={'predicted_n':40} and r['trace']['thinking_disabled'] is True
    assert str(tmp_path) not in json.dumps(doc['model_file'])
