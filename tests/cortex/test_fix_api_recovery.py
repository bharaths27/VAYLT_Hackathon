"""After a STOP the operator can verify and rearm from a screen (review finding 3).

API flow with the FakeRobot in both modes, and the real inspector page driven in node
against a live server: Find -> note -> verify -> Clear stop -> approve again."""
import json,shutil,socket,subprocess,threading,time
from pathlib import Path
import pytest,uvicorn,httpx
from fastapi.testclient import TestClient
from starlette.routing import Match
from engram.cortex.api import create_app
from engram.cortex.store import Store
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.types import Policy
TOKEN='synthetic-test-operator-token-32-characters'
H={'Authorization':'Bearer '+TOKEN}
INSPECTOR=Path(__file__).resolve().parents[2]/'engram/cortex/inspector.html'

class AckOnlyRobot(FakeRobot):
    """The fake world, labelled as a physical adapter: an ACK is not proof, so runs end needs_verification."""
    def state(self):return {**super().state(),'adapter':'fake-physical'}

@pytest.fixture
def physical(arena,tmp_path):
    s=Store(tmp_path/'live.db',policy=Policy(live_enabled=True));s.register_arena(arena);b=Brain(s,AckOnlyRobot(arena))
    assert b.mode=='PHYSICAL'
    yield b
    b.close()

def run(c,path,body):
    p=c.post(path,json=body,headers=H).json()
    return c.post('/operations/'+p['operation_id']+'/approve',json={'digest':p['digest']},headers=H)

def test_physical_stop_verify_clear_approve(physical):
    with TestClient(create_app(physical,tokens={TOKEN:'carther'},start_idle=False)) as c:
        first=run(c,'/plan/goto',{'zone':'bench'}).json();assert first['status']=='needs_verification'
        assert c.post('/stop',headers=H).json()['stop_requested']
        assert c.post('/robot/clear_stop',headers=H).status_code==403                 # unverified physical result blocks rearming
        calls=len(physical.robot.calls)
        assert run(c,'/plan/goto',{'zone':'home'}).status_code!=200 and len(physical.robot.calls)==calls
        assert c.post('/operations/'+first['operation_id']+'/verify',json={'ok':True,'note':''},headers=H).status_code==422
        v=c.post('/operations/'+first['operation_id']+'/verify',json={'ok':True,'note':'Robot seen at bench'},headers=H).json()
        assert v['status']=='success' and v['failure']=='operator observation: Robot seen at bench'
        assert c.post('/robot/clear_stop',headers=H).status_code==200
        again=run(c,'/plan/goto',{'zone':'home'});assert again.status_code==200 and again.json()['status']=='needs_verification'

def test_simulated_unknown_latches_then_verify_failed_clear_approve(brain):
    with TestClient(create_app(brain,tokens={TOKEN:'carther'},start_idle=False)) as c:
        brain.robot.faults['go_to_zone']='unknown'
        first=run(c,'/plan/goto',{'zone':'bench'}).json();assert first['status']=='unknown'      # unknown effect latches stop
        assert run(c,'/plan/goto',{'zone':'home'}).status_code!=200
        assert c.post('/robot/clear_stop',headers=H).status_code==403
        assert c.post('/operations/'+first['operation_id']+'/verify',json={'ok':False,'note':'Robot did not move'},headers=H).json()['status']=='failed'
        assert c.post('/robot/clear_stop',headers=H).status_code==200
        assert run(c,'/plan/goto',{'zone':'home'}).json()['status']=='success'

HARNESS=r'''
const vm=require('node:vm'),fs=require('node:fs');
const [html,base,token,stuck]=process.argv.slice(2);
const reg=new Map(),calls=[];
class El{constructor(id){this._id=id||'';this.children=[];this.textContent='';this.value='';this.parent=null}
  get id(){return this._id} set id(v){this._id=v}
  append(...n){for(const c of n){if(typeof c==='object'){c.parent=this;if(c._id)reg.set(c._id,c)}this.children.push(c)}}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);if(reg.get(this._id)===this)reg.delete(this._id)}
  replaceChildren(...n){for(const c of [...this.children])if(typeof c==='object')c.remove();this.children=[];this.append(...n)}}
const page=fs.readFileSync(html,'utf8');for(const m of page.matchAll(/id="([^"]+)"/g))reg.set(m[1],new El(m[1]));
const document={getElementById:id=>reg.get(id)||null,createElement:()=>new El(),addEventListener(){},hidden:false};
const fetchStub=(path,o={})=>{calls.push([o.method||'GET',path]);const {cache,...rest}=o;return fetch(base+path,rest)};
const ctx=vm.createContext({document,window:{addEventListener(){}},fetch:fetchStub,crypto:globalThis.crypto,URL,setTimeout,clearTimeout,console});
vm.runInContext(page.split('<script type="module">')[1].split('</script>')[0],ctx);
const $=id=>reg.get(id),rows=()=>$('recovery').children.map(r=>r.id),out={};
(async()=>{
  $('key').value=token;await $('connect').onclick();out.connected=$('error').textContent;
  await $('clearStop').onclick();out.refused=$('error').textContent;
  await $('unverified').onclick();out.rows=rows();
  const row=reg.get('verify-'+stuck);await row.children[3].onclick();out.emptyNote=$('error').textContent;
  row.children[2].value='Robot seen at bench by Brian';await row.children[3].onclick();out.afterVerify=rows();out.verifyError=$('error').textContent;
  await $('clearStop').onclick();out.clearError=$('error').textContent;
  $('command').value='Go to home';await $('send').onclick();await $('approval').children[0].onclick();
  out.approveError=$('error').textContent;out.rows2=rows();out.calls=calls;console.log(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1)});
'''

@pytest.mark.skipif(not shutil.which('node'),reason='node is not installed')
def test_inspector_page_verifies_and_rearms_against_live_server(physical,tmp_path):
    app=create_app(physical,tokens={TOKEN:'carther'},start_idle=False)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
    t=threading.Thread(target=server.run,daemon=True);t.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        base=f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base,headers=H) as c:
            stuck=run(c,'/plan/goto',{'zone':'bench'}).json()['operation_id'];c.post('/stop')
        (tmp_path/'harness.js').write_text(HARNESS)
        r=subprocess.run(['node',str(tmp_path/'harness.js'),str(INSPECTOR),base,TOKEN,stuck],capture_output=True,text=True,timeout=60)
        assert r.returncode==0,r.stderr
        out=json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        server.should_exit=True;t.join(10)
    assert out['connected']=='' and out['refused']                                      # clear stop refused while unverified
    assert out['rows']==['verify-'+stuck] and 'Write what you observed' in out['emptyNote']
    assert out['afterVerify']==[] and out['verifyError']=='' and out['clearError']==''
    assert physical.operation(stuck)['status']=='success'
    nxt=[p for m,p in out['calls'] if p.endswith('/approve')][0].split('/')[2]
    assert out['approveError']=='' and out['rows2']==['verify-'+nxt] and physical.operation(nxt)['status']=='needs_verification'
    for method,path in out['calls']:                                                    # every call the page makes is a real route
        scope={'type':'http','method':method,'path':path.split('?')[0]}
        assert any(rt.matches(scope)[0]==Match.FULL for rt in app.routes),(method,path)

NAMED=HARNESS.split('const $=id=>')[0].replace("return fetch(base+path,rest)","return Promise.resolve(new Response(JSON.stringify({code:'DENIED',detail:'reconcile running/unknown operations before rearming: '+stuck}),{status:403}))")+r'''
(async()=>{await reg.get('clearStop').onclick();console.log(JSON.stringify({rows:reg.get('recovery').children.map(r=>r.id),error:reg.get('error').textContent}))})();
'''

@pytest.mark.skipif(not shutil.which('node'),reason='node is not installed')
def test_inspector_lists_operations_named_by_the_clear_stop_refusal(tmp_path):
    (tmp_path/'named.js').write_text(NAMED);stuck='ep_'+'ab'*16
    r=subprocess.run(['node',str(tmp_path/'named.js'),str(INSPECTOR),'http://stub',TOKEN,stuck],capture_output=True,text=True,timeout=60)
    out=json.loads(r.stdout.strip().splitlines()[-1])
    assert out['rows']==['verify-'+stuck] and 'Verify the listed operations' in out['error']

def test_inspector_wires_verify_and_clear_stop_without_node():
    page=INSPECTOR.read_text()
    assert "api('/robot/clear_stop'" in page and "'/verify',{ok:good,note:seen}" in page and 'Write what you observed' in page
