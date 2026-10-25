"""scripts/cortex_sidecar.sh: loopback-only, never live, token file 0600 and never printed."""
import os,shutil,socket,stat,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
SH=shutil.which('sh')
pytestmark=pytest.mark.skipif(not SH,reason='no POSIX sh')

def sidecar(tmp,*args,script=None,**env):
    e={k:v for k,v in os.environ.items() if not k.startswith('CORTEX_')}
    e.update(CORTEX_STATE_DIR=str(tmp/'state'),CORTEX_PYTHON=sys.executable,**env)
    return subprocess.run([SH,str(script or ROOT/'scripts/cortex_sidecar.sh'),*args],env=e,capture_output=True,text=True,timeout=90)

@pytest.mark.parametrize('env,reason',[({'CORTEX_HOST':'0.0.0.0'},'loopback only'),({'CORTEX_HOST':'192.168.1.20'},'loopback only'),
    ({'CORTEX_LIVE':'1'},'never drives the robot'),({'CORTEX_CLOUD':'1'},'local-only'),({'CORTEX_QWEN':'1'},'does not call Qwen'),
    ({'CORTEX_CAMERA_URL':'http://127.0.0.1:8200/robot/frame'},'without a camera'),({'CORTEX_DB':'/home/starrasp/engram_hub/engram.db'},"old brain's database")])
def test_unsafe_configuration_is_refused_before_anything_is_written(tmp_path,env,reason):
    script=tmp_path/'repo/scripts/cortex_sidecar.sh';script.parent.mkdir(parents=True)
    script.write_bytes((ROOT/'scripts/cortex_sidecar.sh').read_bytes().replace(b'\r\n',b'\n'))   # a Windows checkout may carry CRLF
    r=sidecar(tmp_path,'start',script=script,**env)
    assert r.returncode==1 and reason in r.stderr and not (tmp_path/'state').exists()

@pytest.mark.skipif(sys.platform!='linux',reason='process control is exercised on Linux (the Pi)')
def test_start_status_stop_on_loopback(tmp_path):
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=str(s.getsockname()[1])
    try:
        r=sidecar(tmp_path,'start',CORTEX_PORT=port);assert r.returncode==0,r.stderr
        token=(tmp_path/'state/token').read_text().strip()
        assert len(token)>=32 and token not in r.stdout+r.stderr and stat.S_IMODE((tmp_path/'state/token').stat().st_mode)==0o600
        st=sidecar(tmp_path,'status',CORTEX_PORT=port);assert st.returncode==0 and '"mode":"SIMULATED"' in st.stdout
        assert sidecar(tmp_path,'start',CORTEX_PORT=port).stdout.startswith('cortex-sidecar: already running')
    finally:
        assert sidecar(tmp_path,'stop',CORTEX_PORT=port).returncode==0
    assert sidecar(tmp_path,'status',CORTEX_PORT=port).returncode==3 and not (tmp_path/'state/cortex.pid').exists()
