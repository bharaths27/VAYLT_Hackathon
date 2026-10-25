"""Authenticated additive API for Cortex. Defaults to a separate MOCK database.

Run from the repository root with uvicorn engram.cortex.api:create_app --factory
--host 127.0.0.1 --port 8010. On the Pi use scripts/cortex_sidecar.sh (loopback only, reached
with ssh -L); no LAN exposure has been reviewed.
No submitted branch, voice stack, or motor service is modified by importing this.
"""
from __future__ import annotations
import asyncio,copy,hmac,json,logging,os,time
from contextlib import AsyncExitStack,asynccontextmanager
from pathlib import Path
from typing import Any,Literal
from fastapi import FastAPI,Depends,Header,HTTPException,Request
from fastapi.responses import JSONResponse,FileResponse,Response,StreamingResponse
from pydantic import BaseModel,ConfigDict,Field
from .types import *
from .store import Store
from .runtime import Brain
from .planning import CAPS,PHYSICAL,parse_goal,safety_reason
from .models import IntentAdvisor,QwenClient
from .simulation import FakeRobot
from .media import FrameHub,LocalJPEG

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
class Source(Input):
    title:str=Field(min_length=1,max_length=200)
    text:str=Field(min_length=1,max_length=200000)
    kind:str='document'
    rules:list[dict]|None=None
    supersedes:str|None=None
    cloud_ok:bool=False
    goal_definitions:dict|None=None
    prohibitions:list[str]|None=None
    facts:list[dict]|None=None
class Command(Input):
    text:str=Field(min_length=1,max_length=2000)
    who:str|None=None # compatibility only; never used as authenticated identity
    cloud_allowed:bool=False
    idempotency_key:str|None=Field(default=None,max_length=200)
class Query(Input):
    goal:str=Field(min_length=1,max_length=2000)
class Recall(Input):
    question:str=Field(min_length=1,max_length=2000)
    kinds:list[str]|None=None
class Decision(Input):
    digest:str=Field(min_length=64,max_length=64)
    approved_by:str|None=None # ignored in favor of authenticated identity
class Verify(Input):
    ok:bool
    note:str=Field(min_length=1,max_length=1000)
class Future(Input):
    trigger:str=Field(min_length=1,max_length=200)
    request:str=Field(min_length=1,max_length=2000)
    expires_at:float|None=None
class Timed(Input):
    due_at:float
    request:str=Field(min_length=1,max_length=2000)
class Trigger(Input):
    trigger:str=Field(min_length=1,max_length=200)
    event_id:str=Field(min_length=1,max_length=160)
class Resolution(Input):
    reason:str=Field(min_length=1,max_length=1000)
class Observation(Input):
    object_id:str
    zone:str
    captured_at:float|None=None
    note:str=Field(min_length=1,max_length=1000)
class SensorInput(Input):
    object_id:str
    zone:str
    captured_at:float
    boot_id:str
    sequence:int
    frame_id:str
    calibration_id:str
class PlanInput(Input):
    request:str=Field(min_length=1,max_length=2000)
class ZoneInput(Input):
    zone:str='home'
class GoalInput(Input):
    goal:dict
    request:str=Field(min_length=1,max_length=2000)
    idempotency_key:str|None=None
class Turn(Input):
    state:dict
    cloud_allowed:bool=False


MAX_BODY=300000
SAFE_METHODS={'GET','HEAD','OPTIONS'}
SWEEP_S,SWEEP_MAX_S,REPEAT_S=1.0,60.0,600.0
log=logging.getLogger('engram.cortex.api')

async def sweep_forever(b:Brain):
    """Timed-task scheduler that never dies. Any failure (clock rollback, 'database is locked')
    is logged, retried with exponential backoff SWEEP_S..SWEEP_MAX_S, and recorded as a decision
    once per REPEAT_S while the same failure repeats. Due tasks only ever become proposals."""
    delay,last,noted=SWEEP_S,None,0.0
    while True:
        try:await asyncio.to_thread(b.sweep_due);delay,last=SWEEP_S,None
        except Exception as e:
            code=e.code if isinstance(e,CortexError) else type(e).__name__
            log.warning('timed-task sweep failed: %s; retrying in %.1f s',code,delay)
            if code!=last or time.monotonic()-noted>=REPEAT_S:
                try:await asyncio.to_thread(b.store.decision,'clock_sweep','local',None,False,code);last,noted=code,time.monotonic()
                except Exception:log.exception('could not record the sweep failure')
            await asyncio.sleep(delay);delay=min(delay*2,SWEEP_MAX_S);continue
        await asyncio.sleep(delay)

async def stop_task(task):
    task.cancel();await asyncio.gather(task,return_exceptions=True)

class Guard:
    """Outermost pure-ASGI gate. A cross-site mutation, or one without a credential of its route's role (a sensor
    credential on a sensor route, an operator credential on every other), is refused before ONE body byte is read;
    every body is counted as it arrives (declared or chunked) and refused past MAX_BODY. Route dependencies still
    decide the principal; this only checks that a credential of the right role exists."""
    def __init__(self,app,*,credentials,sensor_credentials=(),sensor_paths=frozenset()):
        self.app=app;self.credentials=[c.encode() for c in credentials]
        self.sensor_credentials=[c.encode() for c in sensor_credentials];self.sensor_paths=frozenset(sensor_paths)
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        h={k.decode('latin-1').lower():v.decode('latin-1') for k,v in scope['headers']}
        if scope['method'] not in SAFE_METHODS:
            # Refuse cross-site browser mutations; serve through a paired same-origin TLS proxy.
            if h.get('sec-fetch-site')=='cross-site':return await refuse(send,403,'cross-site mutation refused')
            auth=h.get('authorization','');sensor=scope.get('path') in self.sensor_paths
            role=self.sensor_credentials if sensor else self.credentials
            if not (auth.startswith('Bearer ') and any(hmac.compare_digest(auth[7:].encode(),c) for c in role)):
                return await refuse(send,401,'trusted sensor credential required' if sensor else 'operator credential required')
        try:declared=int(h.get('content-length','0'))
        except ValueError:return await refuse(send,400,'invalid length')
        if declared>MAX_BODY:return await refuse(send,413,'input too large')
        body=b'';more=True
        while more:
            m=await receive()
            if m['type']!='http.request':return
            body+=m.get('body',b'');more=m.get('more_body',False)
            if len(body)>MAX_BODY:return await refuse(send,413,'input too large')
        replayed=False
        async def replay():
            nonlocal replayed
            if replayed:return await receive()
            replayed=True;return {'type':'http.request','body':body,'more_body':False}
        await self.app(scope,replay,send)

async def refuse(send,status,detail):
    data=json.dumps({'detail':detail}).encode()
    await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),(b'content-length',str(len(data)).encode()),
        (b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer'),(b'connection',b'close')]})
    await send({'type':'http.response.body','body':data})


def present(result:dict)->dict:
    """Old iPad card shape, while retaining the exact internal signed goal contract."""
    out=copy.deepcopy(result)
    if out.get('plan'):
        plan=out['plan'];plan['goal_contract']=plan['goal'];plan['goal']=out.get('request','Task')
        if out.get('check') is not None:
            out['check']['physical']=any(s['op'] in PHYSICAL for s in plan.get('steps',[]))
    if 'status' in out:out['outcome']=out['status']
    return out


def _configured_brain()->Brain:
    arena_path=Path(os.environ.get('ENGRAM_ARENA','fixtures/lab/arena.json'))
    if not arena_path.is_file():raise Missing('set ENGRAM_ARENA to the reviewed arena file')
    arena=json.loads(arena_path.read_text())
    live=os.environ.get('CORTEX_LIVE','0')=='1'
    policy=Policy(live_enabled=live,cloud_enabled=os.environ.get('CORTEX_CLOUD','0')=='1',
        auto_publish_verified=os.environ.get('CORTEX_AUTO_PUBLISH','0')=='1',
        idle_after_s=float(os.environ.get('CORTEX_IDLE_SECONDS','60')),
        clock_trusted=os.environ.get('CORTEX_CLOCK_TRUSTED','0')=='1')
    path=Path(os.environ.get('CORTEX_DB','cortex-demo.db')).expanduser()
    path.parent.mkdir(parents=True,exist_ok=True)
    s=Store(path,policy=policy);s.register_arena(arena)
    if live:
        from .integration import upstream_robot
        robot=upstream_robot(s,arena)
    else:robot=FakeRobot(arena)
    qwen=QwenClient(endpoint=os.environ.get('CORTEX_LLM_URL','http://127.0.0.1:8080'),
        contract_path=os.environ.get('CORTEX_CONTRACT') or None,
        input_budget=int(os.environ.get('CORTEX_INPUT_TOKENS','2048')),
        timeout_s=float(os.environ.get('CORTEX_LLM_TIMEOUT','120'))) if os.environ.get('CORTEX_QWEN','0')=='1' else None
    return Brain(s,robot,qwen=qwen,intent=IntentAdvisor(enabled=policy.cloud_enabled))


def env_token() -> str:
    """The operator token: read from CORTEX_API_TOKEN_FILE when set (so it never sits on a command line), else from
    CORTEX_API_TOKEN."""
    path=os.environ.get('CORTEX_API_TOKEN_FILE')
    return Path(path).read_text(encoding='utf-8').strip() if path else os.environ.get('CORTEX_API_TOKEN','')

def create_app(brain:Brain|None=None,*,tokens:dict[str,str]|None=None,frame_hub:FrameHub|None=None,
               start_idle:bool|None=None,sensor_tokens:dict[str,str]|None=None,admins:set[str]|None=None)->FastAPI:
    supplied=brain is not None
    # Operators who may revoke, delete or revise any operator's source: explicit, none by default (CORTEX_ADMINS).
    admins=set(admins) if admins is not None else {p.strip() for p in os.environ.get('CORTEX_ADMINS','').split(',') if p.strip()}
    keys=tokens if tokens is not None else {env_token():os.environ.get('CORTEX_OPERATOR','operator')}
    if not keys or any(len(k)<32 or not v.strip() for k,v in keys.items()):
        raise Denied('configure a random server-side CORTEX_API_TOKEN of at least 32 characters')
    # An explicit set, even an empty one (sensors disabled), is the caller's; only an absent one falls back to the environment.
    skeys=sensor_tokens if sensor_tokens is not None else ({os.environ['CORTEX_SENSOR_TOKEN']:'sensor:camera'} if os.environ.get('CORTEX_SENSOR_TOKEN') else {})
    if any(len(k)<32 or not v.startswith('sensor:') or k in keys for k,v in skeys.items()):
        raise Denied('sensor credentials must be distinct, scoped and at least 32 characters')
    b=brain or _configured_brain()
    hub=frame_hub
    if hub is None and os.environ.get('CORTEX_CAMERA_URL'):
        hub=FrameHub(LocalJPEG(os.environ['CORTEX_CAMERA_URL']),fps=float(os.environ.get('CORTEX_CAMERA_FPS','10')))
    enable_idle=(os.environ.get('CORTEX_IDLE','1')=='1') if start_idle is None else start_idle
    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as cleanup: # every registered step runs at shutdown, even if one raises
            if not supplied:cleanup.callback(b.close)
            cleanup.callback(b.dreamer.stop)
            # Reconcile durable wakes into proposals only; never resume a physical effect.
            await asyncio.to_thread(b.reconcile_wakes)
            if b.store.policy.clock_trusted:cleanup.push_async_callback(stop_task,asyncio.create_task(sweep_forever(b)))
            if enable_idle:b.dreamer.start()
            if hub:hub.start();cleanup.callback(hub.stop)
            yield
    app=FastAPI(title='ENGRAM Cortex Memory Runtime',version='0.2.0',lifespan=lifespan)
    app.state.brain=b;app.state.frame_hub=hub
    def operator(authorization:str|None=Header(default=None)):
        if not authorization or not authorization.startswith('Bearer '):raise HTTPException(401,'operator credential required')
        candidate=authorization[7:]
        for secret,principal in keys.items():
            if hmac.compare_digest(candidate.encode(),secret.encode()):return principal
        raise HTTPException(401,'invalid operator credential')
    def sensor(authorization:str|None=Header(default=None)):
        if authorization and authorization.startswith('Bearer '):
            for key,identity in skeys.items():
                if hmac.compare_digest(authorization[7:].encode(),key.encode()):return identity
        raise HTTPException(401,'trusted sensor credential required')
    @app.post('/observations/sensor')
    def sensor_observe(body:SensorInput,principal=Depends(sensor)):
        from .sensors import ingest
        return ingest(b.store,body.model_dump(),sensor_principal=principal,runtime_mode=b.mode)
    @app.exception_handler(CortexError)
    async def error(req,exc):
        code=403 if isinstance(exc,Denied) else 404 if isinstance(exc,Missing) and str(exc).endswith('not found') else 409
        return JSONResponse(status_code=code,content={'code':exc.code,'detail':str(exc)})
    @app.middleware('http')
    async def headers(req,call_next):
        response=await call_next(req)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        # No response may be framed: a hostile page cannot overlay the inspector's approval controls (clickjacking).
        response.headers['X-Frame-Options']='DENY'
        csp=response.headers.get('Content-Security-Policy')
        response.headers['Content-Security-Policy']=(csp+'; ' if csp else '')+"frame-ancestors 'none'"
        return response
    app.add_middleware(Guard,credentials=list(keys),sensor_credentials=list(skeys),sensor_paths={'/observations/sensor'}) # added last = outermost
    adapter_label=b.robot.state().get('adapter')
    @app.get('/health')
    def health():return {'ok':True,'robot':adapter_label,'mode':b.mode,'service':'cortex'}
    @app.get('/cortex',include_in_schema=False)
    def inspector():return FileResponse(Path(__file__).with_name('inspector.html'))
    @app.get('/memory/package')
    def package(principal=Depends(operator)):
        p=b.qwen.contract_path if b.qwen else Path(__file__).with_name('ENGRAM.md')
        body=p.read_text()
        import hashlib
        return {'path':str(p),'sha256':hashlib.sha256(body.encode()).hexdigest(),'contract':body,'model_prompt_enabled':bool(b.qwen)}
    @app.post('/memory/sources')
    def teach(body:Source,principal=Depends(operator)):
        if body.kind=='future':raise Denied('the "future" kind is reserved for commitments; schedule one with POST /prospective')
        if body.supersedes:own_source(body.supersedes,principal)      # a revision retires the old source: its owner's act
        with b.activity.foreground():return b.store.teach(body.title,body.text,owner=principal,**body.model_dump(exclude={'title','text'}))
    def own_source(sid,principal):
        """A source is revoked, deleted or revised by its owner, or by an explicitly configured administrator; another
        operator's source (a commitment included) is refused."""
        if b.store.source_state(sid)['origin']!=principal and principal not in admins:raise Denied('the source belongs to another operator')
    @app.post('/memory/sources/{sid}/revoke')
    def revoke(sid:str,principal=Depends(operator)):
        own_source(sid,principal)
        with b.activity.foreground():b.store.revoke_source(sid)
        return {'source_id':sid,'status':'revoked'}
    @app.delete('/memory/sources/{sid}')
    def delete(sid:str,principal=Depends(operator)):
        own_source(sid,principal)
        with b.activity.foreground():b.store.revoke_source(sid,delete=True)
        return {'source_id':sid,'status':'deleted'}
    @app.post('/memory/rules/{aid}/resolve')
    def resolve(aid:str,body:Resolution,principal=Depends(operator)):
        with b.activity.foreground():b.store.resolve_rule(aid,principal=principal,reason=body.reason)
        return {'ok':True}
    @app.post('/memory/query')
    def query(body:Query,principal=Depends(operator)):
        with b.activity.foreground():
            try:g=parse_goal(body.goal,b.store)
            except CortexError:g=None
            return b.store.bundle(body.goal,g,principal=principal)
    @app.post('/memory/recall')
    def recall(body:Recall,principal=Depends(operator)):
        with b.activity.foreground():return b.store.recall(body.question,body.kinds,principal=principal)
    @app.get('/memory/objects')
    def objects(principal=Depends(operator)):return b.store.objects()
    @app.get('/memory/facts')
    def facts(principal=Depends(operator)):return b.store.rules()
    @app.get('/memory/derived')
    def derived(principal=Depends(operator)):return [d for d in b.store.derived() if b.store.visible(d['refs'],principal)]
    @app.get('/evidence/{cid}')
    def evidence(cid:str,principal=Depends(operator)):return b.store.evidence(cid,principal=principal)
    @app.post('/observations/operator')
    def observe(body:Observation,principal=Depends(operator)):
        if b.mode=='PHYSICAL':raise Denied('physical sensor observations must enter through the reviewed hardware integration, not a browser assertion')
        return {'id':b.store.observe(body.object_id,body.zone,origin='operator:'+principal,mode='OPERATOR',captured_at=body.captured_at,evidence=body.note)}
    @app.post('/commands')
    def command(body:Command,principal=Depends(operator)):
        return present(b.command(body.text,principal=principal,idempotency_key=body.idempotency_key,cloud_allowed=body.cloud_allowed))
    # Every planning request is foreground work for its whole length, as /commands is: idle dreaming yields to it.
    @app.post('/plan')
    def plan(body:PlanInput,principal=Depends(operator)):
        with b.activity.foreground():return present(b.command(body.request,principal=principal))
    @app.post('/plan/rest')
    @app.post('/plan/goto')
    def goto(body:ZoneInput,principal=Depends(operator)):
        with b.activity.foreground():return present(b.propose(Goal('goto',zone=body.zone),'Go to '+body.zone,principal=principal))
    @app.post('/goals/propose')
    def goal(body:GoalInput,principal=Depends(operator)):
        with b.activity.foreground():return propose_goal(body,principal)
    def propose_goal(body:GoalInput,principal:str):
        selected=Goal.from_dict(body.goal)
        if safety_reason(body.request):raise Denied(safety_reason(body.request))
        try:parsed=parse_goal(body.request,b.store)
        except Missing:parsed=None
        if parsed is not None:
            if parsed!=selected:raise Conflict('typed goal conflicts with the supported request')
            return present(b.propose(selected,body.request,principal=principal,idempotency_key=body.idempotency_key))
        # Unparsed text is never shown as the goal: the card shows the typed goal; the text is an operator note.
        r=b.propose(selected,describe(selected),principal=principal,idempotency_key=body.idempotency_key)
        if not b.store.one("SELECT 1 FROM operation_event WHERE episode_id=? AND op='operator_note' AND detail=?",(r['operation_id'],body.request[:1000])):
            b.journal(r['operation_id'],'operator_note',{},'noted',body.request)
        return {**present(r),'operator_note':body.request}
    def describe(g:Goal)->str:
        names={o['id']:o['name'] for o in b.store.objects()};what=' and '.join(names.get(t,t) for t in g.targets)
        return {'fetch':f'Bring {what} to {g.zone}','goto':f'Go to {g.zone}','inspect':f'Inspect {what}'}.get(g.kind) or f'{g.primitive} {canonical(g.args)}'
    def owned(eid,principal):
        o=b.operation(eid)
        if o['owner']!=principal:raise Denied('operation belongs to another operator')
        return o
    @app.get('/operations/pending')
    def pending(principal=Depends(operator)):return [present(o) for o in b.pending() if o['owner']==principal]
    @app.get('/operations/{eid}')
    def operation(eid:str,principal=Depends(operator)):return present(owned(eid,principal))
    @app.post('/operations/{eid}/approve')
    def approve(eid:str,body:Decision,principal=Depends(operator)):return present(b.approve(eid,body.digest,principal=principal))
    @app.post('/operations/{eid}/reject')
    def reject(eid:str,principal=Depends(operator)):return present(b.reject(eid,principal=principal))
    @app.post('/operations/{eid}/verify')
    def verify(eid:str,body:Verify,principal=Depends(operator)):return present(b.verify_outcome(eid,principal=principal,ok=body.ok,note=body.note))
    @app.post('/stop')
    def stop(principal=Depends(operator)):return b.stop()
    @app.post('/robot/clear_stop')
    def clear(principal=Depends(operator)):return b.clear_stop(principal=principal)
    @app.get('/robot/state')
    def state(principal=Depends(operator)):return b.robot.state()
    @app.get('/capabilities')
    def capabilities(principal=Depends(operator)):
        return [{'name':name,'args':{k:t.__name__ for k,t in args.items()},'physical':name in PHYSICAL,'acceptance':b.mode} for name,args in CAPS.items()]
    # A procedure built from another operator's commitment stays that operator's, as its derived memories do.
    @app.get('/skills')
    def skills(principal=Depends(operator)):return [s for s in b.skills.list() if b.store.visible(s['definition'].get('refs',[]),principal)]
    @app.get('/skills/{sid}')
    def get_skill(sid:str,principal=Depends(operator)):return seen_skill(sid,principal)
    def seen_skill(sid,principal):
        """A procedure this principal may see; one hidden from them (built on another's commitment or episode) is, for
        reading, testing, approving and revoking alike, not there."""
        s=b.skills.get(sid)
        if not b.store.visible(s['definition'].get('refs',[]),principal):raise Missing('skill not found in the Cortex registry')
        return s
    @app.post('/skills/{sid}/test')
    def test_skill(sid:str,principal=Depends(operator)):
        seen_skill(sid,principal)
        with b.activity.foreground():return b.skills.test(sid)
    @app.post('/skills/{sid}/approve')
    def approve_skill(sid:str,body:Decision,principal=Depends(operator)):
        seen_skill(sid,principal)
        return b.skills.approve(sid,body.digest,principal=principal)
    @app.post('/skills/{sid}/revoke')
    def revoke_skill(sid:str,principal=Depends(operator)):
        seen_skill(sid,principal);b.skills.revoke(sid);return {'id':sid,'status':'REVOKED'}
    @app.get('/prospective')
    def tasks(principal=Depends(operator)):
        return b.store.rows('SELECT p.* FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE c.owner=? ORDER BY p.created_at',(principal,))
    @app.post('/prospective')
    def future(body:Future,principal=Depends(operator)):
        return b.schedule(body.trigger,body.request,principal=principal,expires_at=body.expires_at)
    @app.post('/prospective/{tid}/cancel')
    def cancel(tid:str,principal=Depends(operator)):return b.cancel_task(tid,principal=principal)
    @app.post('/prospective/timed')
    def timed(body:Timed,principal=Depends(operator)):
        return b.schedule_at(body.due_at,body.request,principal=principal)
    @app.post('/events/trigger')
    def trigger(body:Trigger,principal=Depends(operator)):
        return [{**x,'proposal':present(x['proposal'])} if 'proposal' in x else x for x in b.trigger(body.trigger,event_id=body.event_id,principal=principal)]
    @app.get('/events')
    def events(limit:int=100,principal=Depends(operator)):
        return b.store.rows('SELECT o.* FROM operation_event o LEFT JOIN cx_episode e ON e.id=o.episode_id WHERE e.owner=? OR o.episode_id IS NULL ORDER BY o.seq DESC LIMIT ?',(principal,max(1,min(limit,200))))
    @app.post('/dream/compile')
    def dream(principal=Depends(operator)):return b.dreamer.cycle(force=True)
    @app.get('/dream/status')
    def dream_status(principal=Depends(operator)):
        return {'enabled':enable_idle,'recent':b.store.rows('SELECT * FROM cx_dream ORDER BY started_at DESC LIMIT 10'),'activity':b.activity.snapshot()}
    @app.post('/turn/advice')
    def turn(body:Turn,principal=Depends(operator)):
        return b.intent.turn(body.state,disclosure_allowed=bool(body.cloud_allowed and b.store.policy.cloud_enabled))
    @app.get('/metrics')
    def metrics(principal=Depends(operator)):return b.metrics(principal)
    @app.get('/camera/status')
    def camera_status(principal=Depends(operator)):return hub.metadata() if hub else {'available':False,'reason':'not configured'}
    @app.get('/frame.jpg')
    def frame(principal=Depends(operator)):
        f=None
        if hub:
            hub.touch();f=hub.latest() # a viewer: the poller runs while frames are being requested
            if not f or hub.receipt_age(f)>2:f=hub.latest(after=f.seq if f else 0,wait_s=1)
        if not f or hub.receipt_age(f)>2:raise HTTPException(503,'camera frame unavailable/stale')
        return Response(f.jpeg,media_type='image/jpeg',headers={'X-Frame-Sequence':str(f.seq),'X-Frame-Received-At':str(f.received_at),'X-Frame-Captured-At':str(f.captured_at) if f.captured_at else 'unknown'})
    @app.get('/video.mjpg')
    async def video(req:Request,principal=Depends(operator)):
        if not hub:raise HTTPException(503,'camera not configured')
        async def frames():
            seq=0;hub.watch() # the poller runs while at least one stream is attached
            try:
                while not await req.is_disconnected():
                    f=await asyncio.to_thread(hub.latest,after=seq,wait_s=.5)
                    if not f:continue
                    # A stale frame is skipped but still passed: the next read waits for a newer one, never re-reads it at once.
                    seq=f.seq
                    if hub.receipt_age(f)<2:
                        yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(f.jpeg)).encode()+b'\r\nX-Frame-Sequence: '+str(seq).encode()+b'\r\n\r\n'+f.jpeg+b'\r\n'
            finally:hub.unwatch()
        return StreamingResponse(frames(),media_type='multipart/x-mixed-replace; boundary=frame')
    @app.post('/clock/reestablish')
    def reestablish_clock(body:Resolution,principal=Depends(operator)):return b.reestablish_time(principal=principal,note=body.reason)
    return app
