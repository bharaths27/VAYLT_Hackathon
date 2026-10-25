"""scripts/cortex_sidecar.sh start path (luna round-2 review of PR 7; each fails on d22c851).

Stdlib only; the start path is exercised on Linux (the Pi), e.g. under WSL with --noconftest."""
import json,os,shutil,socket,stat,subprocess,sys,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
SH=shutil.which('sh')
pytestmark=pytest.mark.skipif(not SH or sys.platform!='linux',reason='the start path is exercised on Linux (the Pi)')

def free_port(family=socket.AF_INET,host='127.0.0.1'):
    with socket.socket(family) as s:s.bind((host,0));return str(s.getsockname()[1])

def sidecar(tmp,*args,**env):
    e={k:v for k,v in os.environ.items() if not k.startswith('CORTEX_')}
    e.update({'CORTEX_STATE_DIR':str(tmp/'state'),'CORTEX_PYTHON':sys.executable,**env})
    return subprocess.run([SH,str(ROOT/'scripts/cortex_sidecar.sh'),*args],env=e,capture_output=True,text=True,timeout=90)


# P2: success means OUR process answers with OUR credential, not any 200 on the port.
def test_start_fails_when_another_service_holds_the_port(tmp_path):
    class Impostor(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            body=json.dumps({'ok':True,'service':'cortex'}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    srv=HTTPServer(('127.0.0.1',0),Impostor);port=str(srv.server_port);threading.Thread(target=srv.serve_forever,daemon=True).start()
    try:
        r=sidecar(tmp_path,'start',CORTEX_PORT=port)
        assert r.returncode!=0 and 'started' not in r.stdout,r.stdout+r.stderr
    finally:
        srv.shutdown();srv.server_close();sidecar(tmp_path,'stop',CORTEX_PORT=port)


# P2: an existing directory is never chmodded; only a private one we own is adopted.
def test_an_existing_shared_state_dir_is_refused_and_left_alone(tmp_path):
    shared=tmp_path/'shared';shared.mkdir();os.chmod(shared,0o777);port=free_port()
    try:
        r=sidecar(tmp_path,'start',CORTEX_STATE_DIR=str(shared),CORTEX_PORT=port)
        assert r.returncode==1 and 'mode 700' in r.stderr,r.stdout+r.stderr
        assert stat.S_IMODE(shared.stat().st_mode)==0o777 and not (shared/'token').exists()
    finally: sidecar(tmp_path,'stop',CORTEX_STATE_DIR=str(shared),CORTEX_PORT=port)


# P2: the token path is never followed through a symlink, neither written nor chmodded.
def test_a_symlinked_token_path_is_refused_and_its_target_untouched(tmp_path):
    state=tmp_path/'state';state.mkdir();os.chmod(state,0o700)
    target=tmp_path/'repo_file';target.write_text('not a token');os.chmod(target,0o644);os.symlink(target,state/'token');port=free_port()
    try:
        r=sidecar(tmp_path,'start',CORTEX_PORT=port)
        assert r.returncode==1 and 'symlink' in r.stderr,r.stdout+r.stderr
        assert target.read_text()=='not a token' and stat.S_IMODE(target.stat().st_mode)==0o644
    finally: sidecar(tmp_path,'stop',CORTEX_PORT=port)


# P3: the printed tunnel reaches the address the sidecar binds.
def test_ipv6_loopback_prints_a_matching_tunnel(tmp_path):
    try:port=free_port(socket.AF_INET6,'::1')
    except OSError:pytest.skip('no IPv6 loopback here')
    try:
        r=sidecar(tmp_path,'start',CORTEX_HOST='::1',CORTEX_PORT=port)
        assert r.returncode==0 and f'ssh -L {port}:[::1]:{port}' in r.stdout,r.stdout+r.stderr
    finally: sidecar(tmp_path,'stop',CORTEX_HOST='::1',CORTEX_PORT=port)
