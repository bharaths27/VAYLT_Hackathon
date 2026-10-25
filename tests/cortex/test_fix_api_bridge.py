"""Console bridge: blocked --apply, lazy import, honest SIMULATED label, 4xx refusals (review findings 1, 2, 4, 6)."""
import importlib.util,json,os,shutil,subprocess,sys,textwrap
from pathlib import Path
import pytest
from engram.cortex.console_adapter import console_status
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

@pytest.mark.parametrize('res,label',[({'outcome':'success','mode':'SIMULATED'},'SIMULATED'),({'outcome':'success'},'NEEDS_VERIFICATION'),
    ({'outcome':'success','mode':'PHYSICAL'},'VERIFIED'),({'outcome':'needs_verification','mode':'PHYSICAL'},'NEEDS_VERIFICATION'),
    ({'outcome':'unknown','mode':'SIMULATED'},'NEEDS_VERIFICATION'),({'outcome':'aborted','mode':'SIMULATED'},'STOPPED'),({'outcome':'failed','mode':'PHYSICAL'},'FAILED')])
def test_console_status_never_promotes_simulated(res,label):
    assert console_status(res)==label

DRIVER=textwrap.dedent('''
    import json,os,sys,time
    from pathlib import Path
    tmp=sys.argv[1];TOK='synthetic-bridge-token-32-characters-x'
    os.environ.update(CORTEX_BRIDGE='1',ENGRAM_BRAIN_URL='http://127.0.0.1:8010',CORTEX_API_TOKEN=TOK,ENGRAM_ROBOT_URL='mock',
                      ENGRAM_MEMORY_URL='mock',ENGRAM_DATA_DIR=tmp,ENGRAM_MODELS_DIR=tmp)
    import httpx
    from fastapi.testclient import TestClient
    from engram.cortex.store import Store
    from engram.cortex.runtime import Brain
    from engram.cortex.simulation import FakeRobot
    from engram.cortex.api import create_app
    from engram.cortex.console_adapter import AsyncCortexMemory,authorized_client
    arena=json.loads(Path(sys.argv[2]).read_text())
    s=Store(Path(tmp)/'cx.db');s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    cortex=create_app(b,tokens={TOK:'carther'},start_idle=False)
    import server.brain as B,server.main as M
    tr=httpx.ASGITransport(app=cortex)                      # in-process Cortex; the patched brain.py still decides the label
    B._c=authorized_client(B.URL,timeout=60.0,transport=tr)
    M.memory=M.engine.memory=AsyncCortexMemory(B.URL,transport=tr)
    out={}
    with TestClient(M.app,raise_server_exceptions=False) as con:
        op=con.post('/commands',json={'text':'Bring Sample A to bench'}).json()['operation']
        out['approve']=con.post('/operations/'+op['id']+'/approve',json={'digest':op['digest']}).status_code
        for _ in range(100):
            o=next(x for x in con.get('/operations').json() if x['id']==op['id'])
            if o['status'] not in ('APPROVED','EXECUTING'):break
            time.sleep(.05)
        out.update(status=o['status'],mode=o.get('mode'),cortex=b.operation(op['id'])['status'])
        out['evidence']=con.get('/evidence/'+op['id']).status_code
        out['rule_fields']=con.post('/rules',json={'text':'Sample A goes to bench','zone':'bench'}).status_code
        out['skill_approve']=con.post('/skills/skl_x/approve').status_code
    b.close();print(json.dumps(out))
''')

def test_bridged_console_labels_simulated_and_refuses_cleanly(tmp_path):
    con=console_copy(tmp_path);(tmp_path/'driver.py').write_text(DRIVER)
    r=subprocess.run([sys.executable,str(tmp_path/'driver.py'),str(tmp_path),str(ROOT/'fixtures/cortex/arena.json')],cwd=con,
                     env=env(PYTHONPATH=os.pathsep.join([str(ROOT),str(con)])),capture_output=True,text=True,timeout=120)
    assert r.returncode==0,r.stderr[-3000:]
    out=json.loads(r.stdout.strip().splitlines()[-1])
    assert out['approve']==200 and out['cortex']=='success'
    assert out['status']=='SIMULATED' and out['mode']=='SIMULATED'        # was VERIFIED: a mock run shown as robot-verified
    assert out['evidence']==200                                            # was 500: ledger ids fall back to the console ledger
    assert out['rule_fields']==422 and out['skill_approve']==409           # were 500: explicit refusals
