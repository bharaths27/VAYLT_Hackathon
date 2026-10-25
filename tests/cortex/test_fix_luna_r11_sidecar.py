"""scripts/cortex_sidecar.sh: a stop holds the start lock, so no start runs unseen beside it, and it removes only the files
that still name the process it stopped (luna round-11 review of PR 7). Linux (the Pi), e.g. WSL."""
import subprocess,sys,threading,time
import pytest
from .test_fix_luna_r6_sidecar import SH,env,free_port,hold_lock,kill_port_servers,private_state,run
from .test_fix_luna_r7_sidecar import SCRIPT,servers
pytestmark=pytest.mark.skipif(not SH or sys.platform!='linux',reason='the start path is exercised on Linux (the Pi)')

# A stop that pauses right after it sent the server its TERM (PAUSE seconds): the window a concurrent start used.
SLOW_KILL='kill() { command kill "$@"; r=$?; case $1 in -*) ;; *) echo TERM-SENT; sleep "${PAUSE:-8}" ;; esac; return $r; }; '

def stand_in(state,port):
    """The running sidecar, as running() sees it: a live process whose arguments name the Cortex app; reaped once it ends."""
    p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)','engram.cortex.api:create_app',port])
    (state/'cortex.pid').write_text(str(p.pid)+chr(10));(state/'cortex.conf').write_text('stand-in'+chr(10))
    threading.Thread(target=p.wait,daemon=True).start()
    return p

def slow_stop(tmp_path,port,pause):
    lib='. "'+str(SCRIPT)+'"; '
    return subprocess.Popen([SH,'-c',lib+SLOW_KILL+'stop'],env=env(tmp_path,CORTEX_SIDECAR_LIB='1',CORTEX_PORT=port,PAUSE=str(pause)),
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)


# P2-9: a start while a stop is in progress is refused; no server is left running that the PID file does not name.
def test_a_start_during_a_stop_never_leaves_a_server_running_unseen(tmp_path):
    state=private_state(tmp_path);port=free_port();old=stand_in(state,port)
    stopper=slow_stop(tmp_path,port,8)
    try:
        assert stopper.stdout.readline().strip()=='TERM-SENT'                               # the stop is under way
        start=run(tmp_path,'start',CORTEX_PORT=port)
        out,err=stopper.communicate(timeout=60)
        named=(state/'cortex.pid').read_text().strip() if (state/'cortex.pid').exists() else None
        assert servers(port)==([named] if named else []),(servers(port),named,start.stdout,start.stderr,out,err)   # was: unseen
        assert start.returncode==1 and 'in progress' in start.stderr and 'started' not in start.stdout,(start.stdout,start.stderr)
        assert stopper.returncode==0 and 'stopped (pid' in out,(out,err)
    finally:
        stopper.kill();old.kill();kill_port_servers(port)

def test_a_stop_removes_only_files_that_still_name_the_process_it_stopped(tmp_path):
    state=private_state(tmp_path);port=free_port();old=stand_in(state,port);other=subprocess.Popen(['sleep','60'])
    stopper=slow_stop(tmp_path,port,2)
    try:
        assert stopper.stdout.readline().strip()=='TERM-SENT'
        (state/'cortex.pid').write_text(str(other.pid)+chr(10))                            # replaced meanwhile, outside the lock
        out,err=stopper.communicate(timeout=60)
        assert stopper.returncode==0 and (state/'cortex.pid').read_text().strip()==str(other.pid),(out,err)   # was: removed
        assert other.poll() is None                                                          # and never signalled
    finally:
        stopper.kill();other.kill();old.kill();kill_port_servers(port)

def test_a_stop_waits_for_a_start_in_progress_then_stops_what_runs(tmp_path):
    state=private_state(tmp_path);port=free_port();old=stand_in(state,port);holder=hold_lock(state)   # a start in progress
    stopper=subprocess.Popen([SH,str(SCRIPT),'stop'],env=env(tmp_path,CORTEX_PORT=port),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        time.sleep(1.5)
        assert stopper.poll() is None and servers(port)==[str(old.pid)]                      # was: stopped at once, mid-start
        holder.kill();holder.wait()
        out,err=stopper.communicate(timeout=60)
        assert stopper.returncode==0 and f'stopped (pid {old.pid})' in out,(out,err)
        assert not (state/'cortex.pid').exists()
    finally:
        stopper.kill();holder.kill();old.kill();kill_port_servers(port)
