"""scripts/cortex_sidecar.sh: one start at a time even over a stale lock, the surviving server stays stoppable, and a
start never reports a configuration other than the one running (luna round-7 review of PR 7). Linux (the Pi), e.g. WSL."""
import os,subprocess,sys,time
from pathlib import Path
import pytest
from .test_fix_luna_r6_sidecar import ROOT,SH,env,free_port,kill_port_servers,private_state,run
pytestmark=pytest.mark.skipif(not SH or sys.platform!='linux',reason='the start path is exercised on Linux (the Pi)')
SCRIPT=ROOT/'scripts/cortex_sidecar.sh'

def stale_locks(state):
    """Locks a dead starter left behind: a lock directory naming its pid (as an earlier version made) and a lock file."""
    dead=subprocess.Popen(['true']);dead.wait()
    (state/'start.lock').mkdir();(state/'start.lock/pid').write_text(str(dead.pid));(state/'sidecar.lock').touch()

def servers(port):
    """Live Cortex servers started for this test's unique port."""
    out=[]
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:cmd=Path(f'/proc/{d}/cmdline').read_bytes().split(bytes([0]))
            except OSError:continue
            if b'engram.cortex.api:create_app' in cmd and port.encode() in cmd:out.append(d)
    return out


# P2-6: a starter that paused between seeing a dead holder and reclaiming its lock never takes over a lock another holds.
def test_a_starter_paused_over_a_stale_lock_never_shares_the_lock(tmp_path):
    state=private_state(tmp_path);stale_locks(state);e=env(tmp_path,CORTEX_SIDECAR_LIB='1')
    lib='. "'+str(SCRIPT)+'"; '
    paused=lib+'kill() { command kill "$@"; r=$?; [ "$1" != -0 ] || sleep .6; return $r; }; start_lock && echo GOT && sleep 1.5'
    first=subprocess.Popen([SH,'-c',paused],env=e,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True);time.sleep(.2)
    second=subprocess.Popen([SH,'-c',lib+'start_lock && echo GOT && sleep 1.5'],env=e,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    outs=[p.communicate(timeout=30) for p in (first,second)]
    assert sum('GOT' in o for o,_ in outs)==1,outs                                         # was: both held it
    assert any('another start is in progress' in err for _,err in outs),outs

def test_guard_concurrent_starts_over_a_stale_lock_leave_one_server_that_stop_removes(tmp_path):
    # The race itself is timing-bound end to end; the test above forces it. This pins the outcome luna named.
    state=private_state(tmp_path);stale_locks(state);port=free_port()
    procs=[subprocess.Popen([SH,str(SCRIPT),'start'],env=env(tmp_path,CORTEX_PORT=port),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(3)]
    outs=[p.communicate(timeout=90) for p in procs]
    try:
        assert sum('started (pid' in o for o,_ in outs)==1,outs
        pid=(state/'cortex.pid').read_text().strip();assert servers(port)==[pid],(servers(port),outs)
        r=run(tmp_path,'stop',CORTEX_PORT=port)
        assert r.returncode==0 and f'stopped (pid {pid})' in r.stdout,r.stdout+r.stderr
        assert not servers(port)                                                            # nothing left running
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port)

def test_guard_the_server_never_holds_the_start_lock(tmp_path):
    port=free_port()
    try:
        assert run(tmp_path,'start',CORTEX_PORT=port).returncode==0
        pid=(tmp_path/'state/cortex.pid').read_text().strip();lock=os.path.realpath(tmp_path/'state/sidecar.lock')
        fds=[]
        for fd in os.listdir(f'/proc/{pid}/fd'):
            try:fds.append(os.readlink(f'/proc/{pid}/fd/{fd}'))
            except OSError:pass
        assert lock not in fds,fds
        r=run(tmp_path,'start',CORTEX_PORT=port);assert r.returncode==0 and 'already running' in r.stdout,r.stdout+r.stderr
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port)


# P3-8: a start with another port is refused and names where the sidecar really runs; status reports that too.
def test_a_second_start_with_another_port_is_refused_and_names_the_running_one(tmp_path):
    port,other=free_port(),free_port()
    try:
        assert run(tmp_path,'start',CORTEX_PORT=port).returncode==0
        pid=(tmp_path/'state/cortex.pid').read_text().strip()
        r=run(tmp_path,'start',CORTEX_PORT=other)
        assert r.returncode==1 and ':'+port in r.stderr and 'stop it first' in r.stderr and 'started' not in r.stdout,r.stdout+r.stderr
        assert (tmp_path/'state/cortex.pid').read_text().strip()==pid and not servers(other)
        assert ':'+port in run(tmp_path,'status',CORTEX_PORT=other).stdout                 # where it runs, not where asked
        r=run(tmp_path,'start',CORTEX_PORT=port);assert r.returncode==0 and 'already running' in r.stdout,r.stdout+r.stderr
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port);kill_port_servers(other)
