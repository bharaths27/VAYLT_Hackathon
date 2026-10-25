"""scripts/cortex_sidecar.sh never sends the operator credential to a listener it has not shown to be its own child
(luna round-3 review of PR 7). Stdlib only; the start path is exercised on Linux (the Pi), e.g. under WSL."""
import json,os,shutil,subprocess,sys,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
SH=shutil.which('sh')
pytestmark=pytest.mark.skipif(not SH or sys.platform!='linux',reason='the start path is exercised on Linux (the Pi)')

def sidecar(tmp,*args,**env):
    e={k:v for k,v in os.environ.items() if not k.startswith('CORTEX_')}
    e.update({'CORTEX_STATE_DIR':str(tmp/'state'),'CORTEX_PYTHON':sys.executable,**env})
    return subprocess.run([SH,str(ROOT/'scripts/cortex_sidecar.sh'),*args],env=e,capture_output=True,text=True,timeout=90)

def test_no_bearer_reaches_a_listener_that_is_not_our_child(tmp_path):
    seen=[]
    class Squatter(BaseHTTPRequestHandler):            # holds the port, claims to be Cortex, records every credential
        def log_message(self,*a):pass
        def do_GET(self):
            seen.append(self.headers.get('Authorization'))
            body=json.dumps({'ok':True,'service':'cortex'}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    srv=HTTPServer(('127.0.0.1',0),Squatter);port=str(srv.server_port);threading.Thread(target=srv.serve_forever,daemon=True).start()
    try:
        r=sidecar(tmp_path,'start',CORTEX_PORT=port)
        assert r.returncode!=0 and 'started' not in r.stdout,r.stdout+r.stderr
        assert not any(seen),seen                       # not even one request carried the operator bearer
    finally:
        srv.shutdown();srv.server_close();sidecar(tmp_path,'stop',CORTEX_PORT=port)
