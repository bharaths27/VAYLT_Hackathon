"""scripts/cortex_sidecar.sh: starts are serialized, a failing start touches only its own child, and the repository
root itself is never a state directory (luna round-6 review of PR 7). Stdlib only; Linux (the Pi), e.g. WSL."""
import os,shutil,socket,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
SH=shutil.which('sh')
pytestmark=pytest.mark.skipif(not SH or sys.platform!='linux',reason='the start path is exercised on Linux (the Pi)')

def env(tmp,**extra):
    e={k:v for k,v in os.environ.items() if not k.startswith('CORTEX_')}
    e.update({'CORTEX_STATE_DIR':str(tmp/'state'),'CORTEX_PYTHON':sys.executable,**extra});return e

def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return str(s.getsockname()[1])

def run(tmp,*args,**extra):
    return subprocess.run([SH,str(ROOT/'scripts/cortex_sidecar.sh'),*args],env=env(tmp,**extra),capture_output=True,text=True,timeout=90)

def kill_port_servers(port):
    """Self-cleaning even when a mutant loses the PID file: kill any sidecar started for this test's unique port."""
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:cmd=Path(f'/proc/{d}/cmdline').read_bytes().split(bytes([0]))
            except OSError:continue
            if b'engram.cortex.api:create_app' in cmd and port.encode() in cmd:
                try:os.kill(int(d),9)
                except OSError:pass

def private_state(tmp):
    state=tmp/'state';state.mkdir();os.chmod(state,0o700)
    token=state/'token';token.write_text('synthetic-test-operator-token-for-the-sidecar-0123456789'+chr(10));os.chmod(token,0o600)
    return state

def hold_lock(state):
    """A live start in progress: a process holding the start lock (flock on the state directory's sidecar.lock)."""
    p=subprocess.Popen([sys.executable,'-c',"import fcntl,sys,time;f=open(sys.argv[1],'a');fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);time.sleep(60)",
                        str(state/'sidecar.lock')],stdout=subprocess.PIPE,text=True)
    assert p.stdout.readline().strip()=='held'
    return p


# P2-7: two starts at once leave exactly one server, named by the PID file, and the other start never kills it.
def test_concurrent_starts_leave_exactly_one_server(tmp_path):
    state=private_state(tmp_path);port=free_port()                                 # both go straight for the server
    procs=[subprocess.Popen([SH,str(ROOT/'scripts/cortex_sidecar.sh'),'start'],env=env(tmp_path,CORTEX_PORT=port),
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    outs=[p.communicate(timeout=90) for p in procs]
    try:
        assert sum('started (pid' in o for o,_ in outs)==1,outs
        assert any('another start is in progress' in e or 'already running' in o for o,e in outs),outs
        pid=(state/'cortex.pid').read_text().strip()
        assert b'engram.cortex.api:create_app' in Path(f'/proc/{pid}/cmdline').read_bytes()   # the PID file names the live server
        assert run(tmp_path,'status',CORTEX_PORT=port).returncode==0
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port)

def test_a_failing_start_leaves_a_process_it_did_not_start_alone(tmp_path):
    import json,threading,time
    from http.server import BaseHTTPRequestHandler,HTTPServer
    class Squatter(BaseHTTPRequestHandler):                                   # holds the port: our uvicorn fails to bind
        def log_message(self,*a):pass
        def do_GET(self):self.send_response(404);self.end_headers()
    srv=HTTPServer(('127.0.0.1',0),Squatter);port=str(srv.server_port);threading.Thread(target=srv.serve_forever,daemon=True).start()
    state=private_state(tmp_path);other=subprocess.Popen(['sleep','60'])        # a process this start did not create
    try:
        p=subprocess.Popen([SH,str(ROOT/'scripts/cortex_sidecar.sh'),'start'],env=env(tmp_path,CORTEX_PORT=port),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        pidf=state/'cortex.pid';end=time.monotonic()+20
        while not (pidf.exists() and pidf.read_text().strip()) and time.monotonic()<end:time.sleep(.005)
        pidf.write_text(str(other.pid))                                         # the PID file now names someone else
        out,err=p.communicate(timeout=90)
        assert p.returncode!=0 and 'started' not in out,(out,err)
        assert other.poll() is None and pidf.read_text().strip()==str(other.pid)  # not killed, not removed
    finally:
        other.kill();srv.shutdown();srv.server_close();kill_port_servers(port)

def test_a_start_while_another_holds_the_lock_refuses_and_spawns_nothing(tmp_path):
    state=private_state(tmp_path);port=free_port();holder=hold_lock(state)               # a live start in progress
    try:
        r=run(tmp_path,'start',CORTEX_PORT=port)
        assert r.returncode==1 and 'another start is in progress' in r.stderr,r.stdout+r.stderr
        assert not (state/'cortex.pid').exists()
    finally:
        holder.kill();holder.wait();kill_port_servers(port)

def test_guard_start_with_captured_output_returns_and_the_daemon_holds_no_caller_pipe(tmp_path):
    port=free_port()
    try:
        r=run(tmp_path,'start',CORTEX_PORT=port)                                  # capture_output=True, timeout=90
        assert r.returncode==0,r.stdout+r.stderr
        pid=(tmp_path/'state/cortex.pid').read_text().strip()
        for fd in ('0','1','2'):                                                 # stdin /dev/null; stdout, stderr: its log
            assert not os.readlink(f'/proc/{pid}/fd/{fd}').startswith('pipe:'),fd
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port)

def test_the_start_lock_admits_exactly_one_of_two_racers(tmp_path):
    private_state(tmp_path)
    racer='. "'+str(ROOT/'scripts/cortex_sidecar.sh')+'"; start_lock && echo GOT && sleep .3'   # holds the lock briefly
    for i in range(30):
        ps=[subprocess.Popen([SH,'-c',racer],env=env(tmp_path,CORTEX_SIDECAR_LIB='1'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        outs=[p.communicate(timeout=30) for p in ps]
        assert sum('GOT' in o for o,_ in outs)==1,(i,outs)

def test_guard_a_lock_file_left_behind_does_not_block_a_start(tmp_path):
    state=private_state(tmp_path);(state/'sidecar.lock').touch()                         # left by an earlier start
    port=free_port()
    try:
        r=run(tmp_path,'start',CORTEX_PORT=port);assert r.returncode==0 and 'started' in r.stdout,r.stdout+r.stderr
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port)


# P2-6 as stated is not reproducible: "$state/" against "$repo"/* already matches the root itself. Pinned here.
def test_guard_the_repository_root_itself_is_refused_as_state_dir(tmp_path):
    repo=tmp_path/'repo';(repo/'scripts').mkdir(parents=True)
    script=repo/'scripts/cortex_sidecar.sh';script.write_bytes((ROOT/'scripts/cortex_sidecar.sh').read_bytes())
    os.chmod(repo,0o700)
    r=subprocess.run([SH,str(script),'start'],env=env(tmp_path,CORTEX_STATE_DIR=str(repo)),capture_output=True,text=True,timeout=60)
    assert r.returncode==1 and 'outside the repository' in r.stderr,r.stderr
    assert not (repo/'token').exists()
