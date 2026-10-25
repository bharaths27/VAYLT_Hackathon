"""scripts/cortex_sidecar.sh: the old brain's database is refused even before either file exists, and status reaches
its own health endpoint past an environment proxy (luna round-8 review of PR 7). Linux (the Pi), e.g. WSL."""
import subprocess,sys
import pytest
from .test_fix_luna_r6_sidecar import ROOT,SH,env,free_port,kill_port_servers,run
pytestmark=pytest.mark.skipif(not SH or sys.platform!='linux',reason='the start path is exercised on Linux (the Pi)')
SCRIPT=ROOT/'scripts/cortex_sidecar.sh'


# P2-5: two paths naming one file that does not exist yet are the same file (directly or through a symlinked directory).
@pytest.mark.parametrize('through_link',[False,True])
def test_the_brain_database_guard_compares_paths_that_do_not_exist_yet(tmp_path,through_link):
    real=tmp_path/'dbs';real.mkdir();(tmp_path/'link').symlink_to(real,target_is_directory=True)
    cortex,brain=(tmp_path/'link'/'shared.db' if through_link else real/'shared.db'),real/'shared.db'   # neither exists
    port=free_port()
    try:
        r=run(tmp_path,'start',CORTEX_DB=str(cortex),ENGRAM_DB=str(brain),CORTEX_PORT=port)
        assert r.returncode==1 and 'same file' in r.stderr,(r.stdout,r.stderr)                         # was: started on it
        assert not brain.exists()
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port)


# P3-8: the health check is a loopback request and never goes through an environment proxy.
def test_status_reaches_its_own_health_endpoint_past_an_environment_proxy(tmp_path):
    port,dead=free_port(),free_port()                                                    # nothing listens at the proxy
    try:
        assert run(tmp_path,'start',CORTEX_PORT=port).returncode==0
        e={k:v for k,v in env(tmp_path,CORTEX_PORT=port).items() if k.lower()!='no_proxy'}
        e.update(http_proxy=f'http://127.0.0.1:{dead}',HTTP_PROXY=f'http://127.0.0.1:{dead}')
        r=subprocess.run([SH,str(SCRIPT),'status'],env=e,capture_output=True,text=True,timeout=60)
        assert r.returncode==0 and '"service":"cortex"' in r.stdout and 'did not answer' not in r.stdout,(r.stdout,r.stderr)
    finally:
        run(tmp_path,'stop',CORTEX_PORT=port);kill_port_servers(port)
