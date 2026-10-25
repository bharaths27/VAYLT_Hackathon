"""scripts/cortex_sidecar.sh: a relative ENGRAM_DB is refused, since the brain resolves it against its own working
directory, not the caller's (luna round-12 review of PR 7). Linux (the Pi), e.g. WSL."""
import subprocess,sys
import pytest
from .test_fix_luna_r6_sidecar import SH,env,free_port,kill_port_servers,run
from .test_fix_luna_r7_sidecar import SCRIPT,servers
pytestmark=pytest.mark.skipif(not SH or sys.platform!='linux',reason='the start path is exercised on Linux (the Pi)')


# P2-8: started from outside the brain's directory, a relative ENGRAM_DB naming the brain's live database is refused.
def test_a_relative_brain_database_is_refused_wherever_the_sidecar_is_started(tmp_path):
    brain_dir,elsewhere=tmp_path/'brain',tmp_path/'elsewhere';brain_dir.mkdir();elsewhere.mkdir();port=free_port()
    e=env(tmp_path,CORTEX_DB=str(brain_dir/'live.db'),ENGRAM_DB='live.db',CORTEX_PORT=port)   # the brain runs in brain_dir
    try:
        r=subprocess.run([SH,str(SCRIPT),'start'],env=e,cwd=elsewhere,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=90)
        assert r.returncode==1 and 'absolute path' in r.stderr and 'started' not in r.stdout,(r.stdout,r.stderr)   # was: started on it
        assert not servers(port) and not (brain_dir/'live.db').exists()
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port)
