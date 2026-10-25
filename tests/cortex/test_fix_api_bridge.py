"""Console bridge: blocked --apply and lazy import (review findings 1, 4)."""
import importlib.util,json,os,shutil,subprocess,sys,textwrap
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def load_patch():
    spec=importlib.util.spec_from_file_location('cortex_console_patch',ROOT/'scripts/cortex_console_patch.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def console_copy(tmp,patched=True):
    """The real console server, copied; optionally with the generated bridge diff written in."""
    shutil.copytree(ROOT/'console/server',tmp/'console/server',ignore=shutil.ignore_patterns('__pycache__'));(tmp/'console/web').mkdir()
    if patched:
        for path,old,new in load_patch().transforms(tmp):(tmp/path).write_text(new)
    return tmp/'console'

def env(**kw):
    e={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','CORTEX_BRIDGE','CORTEX_API_TOKEN','ENGRAM_BRAIN_URL'}}
    e.update(kw);return e

def test_apply_is_refused_and_writes_nothing(tmp_path):
    console_copy(tmp_path,patched=False)
    before={p:p.read_bytes() for p in (tmp_path/'console/server').rglob('*.py')}
    r=subprocess.run([sys.executable,str(ROOT/'scripts/cortex_console_patch.py'),'--repo',str(tmp_path),'--apply'],capture_output=True,text=True,timeout=60)
    assert r.returncode!=0 and 'blocked until the console authenticates its mutating routes' in r.stderr and 'CXV6' in r.stderr
    assert {p:p.read_bytes() for p in (tmp_path/'console/server').rglob('*.py')}==before
    assert not list(tmp_path.rglob('*.pre-cortex'))
    review=subprocess.run([sys.executable,str(ROOT/'scripts/cortex_console_patch.py'),'--repo',str(tmp_path)],capture_output=True,text=True,timeout=60)
    assert review.returncode==0 and '+++ b/console/server/brain.py' in review.stdout

def test_patched_console_starts_without_flag_or_repo_on_path(tmp_path):
    con=console_copy(tmp_path)
    code='import server.brain as b, server.adapters.memory as m; assert b._c is None and type(m.make_memory()).__name__=="MockMemory"'
    r=subprocess.run([sys.executable,'-c',code],cwd=con,env=env(ENGRAM_MEMORY_URL='mock'),capture_output=True,text=True,timeout=60)
    assert r.returncode==0,r.stderr
    r=subprocess.run([sys.executable,'-c','import server.brain'],cwd=con,env=env(ENGRAM_BRAIN_URL='http://127.0.0.1:8001'),capture_output=True,text=True,timeout=60)
    assert r.returncode==0,r.stderr
