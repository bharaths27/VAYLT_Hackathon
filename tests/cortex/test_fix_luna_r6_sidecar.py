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

def private_state(tmp):
    state=tmp/'state';state.mkdir();os.chmod(state,0o700)
    token=state/'token';token.write_text('synthetic-test-operator-token-for-the-sidecar-0123456789'+chr(10));os.chmod(token,0o600)
    return state


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
        run(tmp_path,'stop',CORTEX_PORT=port)

def test_guard_a_lock_left_by_a_dead_starter_is_reclaimed(tmp_path):
    state=private_state(tmp_path);lock=state/'start.lock';lock.mkdir()
    dead=subprocess.Popen(['true']);dead.wait();(lock/'pid').write_text(str(dead.pid))
    port=free_port()
    try:
        r=run(tmp_path,'start',CORTEX_PORT=port);assert r.returncode==0 and 'started' in r.stdout,r.stdout+r.stderr
        assert not lock.exists()
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port)


# P2-6 as stated is not reproducible: "$state/" against "$repo"/* already matches the root itself. Pinned here.
def test_guard_the_repository_root_itself_is_refused_as_state_dir(tmp_path):
    repo=tmp_path/'repo';(repo/'scripts').mkdir(parents=True)
    script=repo/'scripts/cortex_sidecar.sh';script.write_bytes((ROOT/'scripts/cortex_sidecar.sh').read_bytes())
    os.chmod(repo,0o700)
    r=subprocess.run([SH,str(script),'start'],env=env(tmp_path,CORTEX_STATE_DIR=str(repo)),capture_output=True,text=True,timeout=60)
    assert r.returncode==1 and 'outside the repository' in r.stderr,r.stderr
    assert not (repo/'token').exists()
