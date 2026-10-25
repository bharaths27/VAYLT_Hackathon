"""scripts/cortex_sidecar.sh resolves paths before its guards (luna API review of PR 7; each fails on 3cd807c).

Stdlib only, so it also runs under a bare Linux/WSL python with --noconftest."""
import os,shutil,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
SH=shutil.which('sh')
pytestmark=pytest.mark.skipif(not SH,reason='no POSIX sh')

def repo_copy(tmp):
    """The script in a bare repository (no lab arena): any configuration that passes every guard dies at the
    last check, 'lab arena not found', before anything is written or started."""
    script=tmp/'repo/scripts/cortex_sidecar.sh';script.parent.mkdir(parents=True)
    script.write_bytes((ROOT/'scripts/cortex_sidecar.sh').read_bytes().replace(bytes([13,10]),bytes([10])))
    return script

def start(script,cwd,**env):
    e={k:v for k,v in os.environ.items() if not k.startswith('CORTEX_') and k!='ENGRAM_DB'}
    e.update(CORTEX_PYTHON=sys.executable,**env)
    return subprocess.run([SH,str(script),'start'],cwd=cwd,env=e,capture_output=True,text=True,timeout=60)

def alias(kind,target,link):
    try:
        if kind=='symlink':os.symlink(target,link)
        else:os.link(target,link)
    except (OSError,NotImplementedError):pytest.skip(kind+' unavailable here')


# P2: the DB guard compares the resolved file, not the name it was given.
@pytest.mark.parametrize('kind',['symlink','hardlink'])
def test_db_alias_of_the_brain_database_is_refused(tmp_path,kind):
    script=repo_copy(tmp_path); brain=tmp_path/'repo/engram.db'; brain.write_bytes(b'')
    (tmp_path/'elsewhere').mkdir(); db=tmp_path/'elsewhere/cortex.db'; alias(kind,brain,db)
    r=start(script,tmp_path,CORTEX_STATE_DIR=str(tmp_path/'state'),CORTEX_DB=str(db))
    assert r.returncode==1 and "old brain's database" in r.stderr and not (tmp_path/'state').exists(),r.stderr

def test_db_resolving_to_any_engram_db_is_refused(tmp_path):
    script=repo_copy(tmp_path); (tmp_path/'hub').mkdir(); other=tmp_path/'hub/engram.db'; other.write_bytes(b'')   # not this repository's
    (tmp_path/'elsewhere').mkdir(); db=tmp_path/'elsewhere/cortex.db'; alias('symlink',other,db)
    r=start(script,tmp_path,CORTEX_STATE_DIR=str(tmp_path/'state'),CORTEX_DB=str(db))
    assert r.returncode==1 and 'resolves to' in r.stderr and "old brain's database" in r.stderr and not (tmp_path/'state').exists(),r.stderr

def test_db_alias_of_a_configured_brain_database_is_refused(tmp_path):
    script=repo_copy(tmp_path); (tmp_path/'hub').mkdir(); brain=tmp_path/'hub/brain.sqlite'; brain.write_bytes(b'')
    (tmp_path/'elsewhere').mkdir(); db=tmp_path/'elsewhere/cortex.db'; alias('symlink',brain,db)
    r=start(script,tmp_path,CORTEX_STATE_DIR=str(tmp_path/'state'),CORTEX_DB=str(db),ENGRAM_DB=str(brain))
    assert r.returncode==1 and 'same file as the old brain' in r.stderr and not (tmp_path/'state').exists(),r.stderr


# P2: the repository check compares canonical paths, so neither a relative nor a symlinked state dir lands inside.
@pytest.mark.parametrize('kind',['relative','symlink'])
def test_state_dir_resolving_into_the_repository_is_refused(tmp_path,kind):
    script=repo_copy(tmp_path); repo=tmp_path/'repo'
    if kind=='relative': state,cwd='.state',repo
    else:
        (repo/'state').mkdir(); alias('symlink',repo/'state',tmp_path/'outside'); state,cwd=str(tmp_path/'outside'),tmp_path
    r=start(script,cwd,CORTEX_STATE_DIR=state)
    assert r.returncode==1 and 'outside the repository' in r.stderr,r.stderr
    assert not list(repo.rglob('token')) and not (repo/'.state').exists()

def test_canonical_config_outside_the_repository_passes_every_guard(tmp_path):
    script=repo_copy(tmp_path)                       # control: only the missing arena stops it
    r=start(script,tmp_path,CORTEX_STATE_DIR=str(tmp_path/'state'))
    assert r.returncode==1 and 'lab arena not found' in r.stderr and not (tmp_path/'state').exists(),r.stderr
