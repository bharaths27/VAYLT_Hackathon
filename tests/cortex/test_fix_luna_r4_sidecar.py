"""scripts/cortex_sidecar.sh never puts the operator token on a command line (luna round-4 review of PR 7).
Stdlib only; the start path is exercised on Linux (the Pi), e.g. under WSL with --noconftest."""
import os,shutil,socket,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
SH=shutil.which('sh')
pytestmark=pytest.mark.skipif(not SH or sys.platform!='linux' or not Path('/usr/bin/env').exists(),reason='the start path is exercised on Linux (the Pi)')

def sidecar(tmp,*args,**env):
    e={k:v for k,v in os.environ.items() if not k.startswith('CORTEX_')}
    e.update({'CORTEX_STATE_DIR':str(tmp/'state'),'CORTEX_PYTHON':sys.executable,**env})
    return subprocess.run([SH,str(ROOT/'scripts/cortex_sidecar.sh'),*args],env=e,capture_output=True,text=True,timeout=90)

def test_the_operator_token_is_never_on_a_command_line(tmp_path):
    log=tmp_path/'argv.log'; fake=tmp_path/'bin'; fake.mkdir()
    # An `env` first on PATH that records every argument it is given, then runs the real one.
    (fake/'env').write_text(chr(10).join(['#!/bin/sh','for a in "$@"; do printf "%s" "$a" >> "'+str(log)+'"; echo >> "'+str(log)+'"; done',
                                          'exec /usr/bin/env "$@"',''])); os.chmod(fake/'env',0o755)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=str(s.getsockname()[1])
    path=str(fake)+os.pathsep+os.environ.get('PATH','')
    try:
        r=sidecar(tmp_path,'start',CORTEX_PORT=port,PATH=path)
        assert r.returncode==0,r.stdout+r.stderr
        token=(tmp_path/'state/token').read_text().strip(); pid=(tmp_path/'state/cortex.pid').read_text().strip()
        assert log.exists() and 'CORTEX_API_TOKEN_FILE=' in log.read_text()            # the recorder saw the launch...
        assert token not in log.read_text()                                             # ...and no argument carried the token
        assert token.encode() not in Path(f'/proc/{pid}/cmdline').read_bytes()
    finally:
        sidecar(tmp_path,'stop',CORTEX_PORT=port,PATH=path)
