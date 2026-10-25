"""Regression tests for luna's round-14 review of PR 7 (each fails with its fix reverted; controls/guards marked)."""
import asyncio,json,threading,time
import pytest
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot,Result
from engram.cortex.store import Store
from engram.cortex.types import *

def approve(b,p,who='carther'): return b.approve(p['operation_id'],p['digest'],principal=who)


# P1-1 (class D, the empty case): a plan approved with an empty claw dispatches no step that carries, needs, puts down or
# grips what the claw holds once something is in it; the expected claw state follows the plan's own pickups and placements.
def loaded_once_running(brain,oid,after_calls=0):
    """Something is put in the claw once the operation runs (after `after_calls` dispatched steps)."""
    real,execute=brain.operation,brain.robot.execute
    def load():
        if brain.robot.holding is None: brain.robot.positions.pop(oid);brain.robot.holding=oid;brain.robot.open=False
    def loading(ep):
        out=real(ep)
        if out['status']=='running' and not after_calls: load()
        return out
    def counting(op,args):
        r=execute(op,args)
        if after_calls and len(brain.robot.calls)==after_calls: load()
        return r
    brain.operation=loading;brain.robot.execute=counting

@pytest.mark.parametrize('request_,op,oid',[('Go to the shelf b','go_to_zone','sample_a'),('Close the claw','gripper_close','sample_b'),
                                            ('Open the claw','gripper_open','sample_b'),('Bring Sample A to the bench.','pick_tagged_object','sample_b')])
def test_a_claw_loaded_after_an_approval_with_it_empty_dispatches_nothing_that_uses_it(brain,request_,op,oid):
    p=brain.command(request_,principal='carther');assert p['status']=='proposed' and not p['plan'].get('held')
    loaded_once_running(brain,oid)
    assert approve(brain,p)['status']=='aborted'
    assert not [c for c in brain.robot.calls if c[0]==op]                    # was: carried it, closed on it, released it, picked over it

def test_a_claw_loaded_between_steps_stops_the_plan_before_its_next_pickup(brain):
    brain.store.teach('SOP','Safety goggles must be at the ppe station before handling Sample B.',owner='carther')
    p=brain.command('Bring Sample B to the bench.',principal='carther');assert [s['op'] for s in p['plan']['steps']].count('pick_tagged_object')==2
    loaded_once_running(brain,'sample_a',after_calls=3)                    # after the goggles were placed
    assert approve(brain,p)['status']=='aborted'
    assert [c[0] for c in brain.robot.calls].count('pick_tagged_object')==1 # was: picked Sample B over Sample A

class StopsReporting(FakeRobot):
    """An adapter that stops reporting its claw once the operation runs."""
    reporting=True
    def state(self):
        s=super().state()
        if not self.reporting: s.pop('holding')
        return s

def test_guard_a_claw_no_longer_reported_dispatches_nothing_that_uses_it(store,arena):
    b=Brain(store,StopsReporting(arena));p=b.command('Go to the shelf b',principal='carther');real=b.operation
    def running(ep):
        out=real(ep)
        if out['status']=='running': b.robot.reporting=False
        return out
    b.operation=running
    assert approve(b,p)['status']=='aborted' and not [c for c in b.robot.calls if c[0]=='go_to_zone']

def test_guard_a_claw_swapped_after_its_pickup_puts_nothing_down(brain):
    p=brain.command('Bring Sample A to the bench.',principal='carther');robot=brain.robot;execute=robot.execute
    def swapping(op,args):                                                  # Sample B ends up in the claw instead of Sample A
        r=execute(op,args)
        if op=='pick_tagged_object': robot.positions['sample_a']=robot.at;robot.positions.pop('sample_b');robot.holding='sample_b'
        return r
    robot.execute=swapping
    assert approve(brain,p)['status']=='aborted' and not [c for c in robot.calls if c[0]=='place_at_zone']   # was: put it down

def test_control_the_plans_own_pickups_and_placements_are_what_it_expects(brain):
    brain.store.teach('SOP','Safety goggles must be at the ppe station before handling Sample B.',owner='carther')
    assert approve(brain,brain.command('Bring Sample B to the bench.',principal='carther'))['status']=='success'


# P2-3 (class A): what an operation writes after a dispatch (the step's arguments, an adapter's echoed reply, the reason it
# ended) is never written for an episode a deleted source purged meanwhile; and a purged operation stops at its next step.
from .test_fix_luna_r7 import ANNOUNCE,dump
class Echo(FakeRobot):
    def __init__(self,arena,where): super().__init__(arena);self.where=where;self.on_stop=None
    def execute(self,op,args):
        r=super().execute(op,args)
        if op=='speak': setattr(r,self.where,'said '+args['text'])
        return r
    def stop(self):
        if self.on_stop: hook,self.on_stop=self.on_stop,None;hook()
        super().stop()

def announcing(tmp_path,arena,clock,where):
    s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,Echo(arena,where))
    src=s.teach('Announcement','To announce the result, say the zqxmarker phrase.',owner='carther',goal_definitions=ANNOUNCE)['source_id']
    return s,b,src

def test_a_deletion_between_a_dispatch_and_its_journal_leaves_no_copy(tmp_path,arena,clock):
    s,b,src=announcing(tmp_path,arena,clock,'detail');p=b.command('announce the result',principal='carther');real=b.journal;deleted=threading.Event()
    def journal(ep,op,args,status,detail='',step=0,**kw):              # the reply is in hand; another thread deletes the source
        if op=='speak' and status!='pending' and not deleted.is_set():
            threading.Thread(target=lambda:(s.revoke_source(src,delete=True),deleted.set()),daemon=True).start();deleted.wait(.5)
        return real(ep,op,args,status,detail,step,**kw)
    b.journal=journal
    approve(b,p);assert deleted.wait(5)                                                         # the deletion ran, before or after
    for name,row in dump(s): assert 'zqxmarker' not in row,(name,row)                       # was: the echoed reply written back
    b.close()

class Reply:
    """An adapter reply read only after the step released the vault's authority; reading it runs the hook, in that window."""
    observations=[]
    def __init__(self,status,detail,hook): self.status,self.detail,self.hook=status,detail,hook
    @property
    def location_evidence(self): self.hook();return {}

class WindowEcho(FakeRobot):
    def __init__(self,arena,where): super().__init__(arena);self.where=where;self.in_window=lambda:None
    def execute(self,op,args):
        r=super().execute(op,args)
        if op!='speak': return r
        status,detail=('said '+args['text'],r.detail) if self.where=='status' else (r.status,'said '+args['text'])
        return Reply(status,detail,self.in_window)

@pytest.mark.parametrize('where',['detail','status'])
def test_a_deletion_right_after_a_dispatch_leaves_no_copy_of_its_reply(tmp_path,arena,clock,where):
    s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,WindowEcho(arena,where))
    src=s.teach('Announcement','To announce the result, say the zqxmarker phrase.',owner='carther',goal_definitions=ANNOUNCE)['source_id']
    p=b.command('announce the result',principal='carther')
    b.robot.in_window=lambda:s.source_state(src)['status']=='accepted' and s.revoke_source(src,delete=True)
    approve(b,p)
    for name,row in dump(s): assert 'zqxmarker' not in row,(name,row)                       # was: written after the purge
    b.close()

def test_a_deletion_before_the_operation_ends_leaves_no_copy_in_its_reason(tmp_path,arena,clock):
    s,b,src=announcing(tmp_path,arena,clock,'status');p=b.command('announce the result',principal='carther')
    b.robot.on_stop=lambda:s.revoke_source(src,delete=True)                                # between its last step and its end
    assert approve(b,p)['status']=='unknown'
    for name,row in dump(s): assert 'zqxmarker' not in row,(name,row)                       # was: the echo in its failure reason
    b.close()

def test_an_operation_purged_while_it_runs_stops_before_its_next_step(tmp_path,arena,clock):
    s,b,src=announcing(tmp_path,arena,clock,'detail')
    p=b.propose(Goal.from_dict(ANNOUNCE['announce the result']),'Speak the phrase',principal='carther')   # no alias ref: not cited
    real=b.operation
    def deleting(ep):
        out=real(ep)
        if out['status']=='running' and s.source_state(src)['status']=='accepted': s.revoke_source(src,delete=True)
        return out
    b.operation=deleting
    assert approve(b,p)['status']=='aborted' and not [c for c in b.robot.calls if c[0]=='speak']
    # The purge itself stops it, before its next step is journalled, whatever else it revoked (here the plan's recipe).
    assert not b.store.rows("SELECT 1 FROM operation_event WHERE episode_id=? AND op='speak'",(p['operation_id'],))   # was: journalled
    for name,row in dump(s): assert 'zqxmarker' not in row,(name,row)
    b.close()


# P1-2 (class B): an idle hypothesis rests on everything its prompt showed, whatever the model cited. Revoking a source it
# was shown (and may quote) withdraws it, and an operator's episode in that prompt keeps it that operator's.
from .test_fix_learn_dream import Stub,idle_brain

def test_an_idle_hypothesis_rests_on_its_whole_prompt_not_on_its_citations(arena,clock):
    b=idle_brain(arena,clock)
    quoted=b.store.teach('Note','The zqxsecret cabinet code is kept with Sample A.',owner='alice')['source_id']
    cited=b.store.teach('Note','Sample B is usually at the bench.',owner='bob')['source_id']
    p=b.command('Go to the shelf b',principal='alice');approve(b,p,'alice')          # her episode enters the dream prompt
    def make(bundle):
        assert 'source:'+quoted in bundle['allowed_refs'] and any('zqxsecret' in c['text'] for c in bundle['source_passages'])
        return [{'text':'The zqxsecret code may matter.','refs':['source:'+cited]}]  # quotes one source, cites another
    b.dreamer.qwen=Stub(make)
    assert b.dreamer.cycle(force=True)['status']=='complete'
    h=b.store.one("SELECT id,refs_json,payload_json FROM cx_derived WHERE kind='hypothesis'");refs=json.loads(h['refs_json'])
    assert 'source:'+quoted in refs and 'episode:'+p['operation_id'] in refs,refs    # was: only what it cited
    assert json.loads(h['payload_json'])['cites']==['source:'+cited]
    shown=lambda who:[d['id'] for d in b.store.derived() if b.store.visible(d['refs'],who)]
    assert h['id'] in shown('alice') and h['id'] not in shown('bob')                 # was: shown to Bob too
    b.store.revoke_source(quoted)
    assert b.store.derived(h['id'])['status']=='STALE'                               # was: still a candidate
    b.close()


# P3-4: a foreground call waits for a background call to let go only while no other foreground call waits; a second one
# is refused at once, never queued behind the first.
from engram.cortex.models import ModelUnavailable,QwenClient
from .test_fix_learn_transport import THINK

def dream_transport(dreaming,release):
    """A model server whose dream holds the model until `release` (its call cannot be cancelled); a plan answers at once."""
    def transport(url,body,headers,timeout):
        if url.endswith('/apply-template'): return {'prompt':'P'+THINK}
        if url.endswith('/tokenize'): return {'tokens':[1,2,3]}
        if 'candidates' in json.dumps(body.get('json_schema')): dreaming.set();release.wait(10);return {'model':'m','content':json.dumps({'candidates':[]})}
        return {'model':'m','content':json.dumps({'steps':[{'op':'gripper_open','args':{}}]})}
    return transport

class GatedLock:
    """The model's lock, a waiting call's acquire held back until `gate` opens: as if it had not yet woken."""
    def __init__(self): self.lock,self.gate,self.held_back=threading.Lock(),threading.Event(),threading.Event()
    def acquire(self,blocking=True,timeout=-1):
        if blocking and timeout!=-1: self.held_back.set();self.gate.wait(10)
        return self.lock.acquire(blocking,timeout)
    def release(self): self.lock.release()

def test_a_foreground_call_as_the_dream_lets_go_never_jumps_the_one_waiting(monkeypatch):
    dreaming,release,results=threading.Event(),threading.Event(),{}
    monkeypatch.setattr(QwenClient,'PREEMPT_WAIT_S',5);q=QwenClient(transport=dream_transport(dreaming,release));q._lock=lock=GatedLock()
    goal=Goal('primitive',primitive='gripper_open').to_dict()
    dream=threading.Thread(target=lambda:q.complete('dream',{'rules':[]}),daemon=True);dream.start();assert dreaming.wait(5)
    first=threading.Thread(target=lambda:results.update(first=q.complete('plan',{},goal=goal)),daemon=True);first.start()
    assert lock.held_back.wait(5)                                              # the first waits for the model
    release.set();dream.join(10);assert not dream.is_alive()                   # the dream let go; the first has not woken
    try:
        with pytest.raises(ModelUnavailable,match='already waiting'): q.complete('plan',{},goal=goal)   # was: took it first
    finally:
        lock.gate.set();first.join(10)
    assert results['first'].value['steps']==[{'op':'gripper_open','args':{}}]   # the one waiting is served

def test_a_second_foreground_call_during_a_blocked_dream_is_refused_at_once(monkeypatch):
    dreaming,release,results=threading.Event(),threading.Event(),{}
    monkeypatch.setattr(QwenClient,'PREEMPT_WAIT_S',5);q=QwenClient(transport=dream_transport(dreaming,release));goal=Goal('primitive',primitive='gripper_open').to_dict()
    dream=threading.Thread(target=lambda:q.complete('dream',{'rules':[]}),daemon=True);dream.start();assert dreaming.wait(5)
    first=threading.Thread(target=lambda:results.update(first=q.complete('plan',{},goal=goal)),daemon=True);first.start()
    end=time.monotonic()+5
    while not q._waiting and time.monotonic()<end: time.sleep(.01)          # the first waits for the dream to let go
    try:
        t0=time.monotonic()
        with pytest.raises(ModelUnavailable,match='already waiting'): q.complete('plan',{},goal=goal)   # was: queued behind it
        assert time.monotonic()-t0<1
    finally:
        release.set();first.join(10);dream.join(10)
    assert results['first'].value['steps']==[{'op':'gripper_open','args':{}}]   # the first is served


# P3-5: a frame's capture age is its age when it arrived, advanced on the monotonic clock: a wall clock stepped back
# never makes an old frame read as new.
from engram.cortex.media import FrameHub

def test_guard_a_frame_stamped_ahead_of_the_host_clock_ages_from_zero():
    wall,mono=[1700000000.0],[100.0]
    hub=FrameHub(lambda:None,clock=lambda:wall[0]);hub.monotonic=lambda:mono[0]
    hub.publish(bytes([0xff,0xd8])+bytes(16)+bytes([0xff,0xd9]),captured_at=wall[0]+.5)    # the camera's clock runs ahead
    mono[0]+=2
    assert hub.metadata()['capture_age_s']==2                                               # never negative

def test_a_wall_clock_stepped_back_never_makes_an_old_frame_read_as_new():
    wall,mono=[1700000000.0],[100.0]
    hub=FrameHub(lambda:None,clock=lambda:wall[0]);hub.monotonic=lambda:mono[0]
    hub.publish(bytes([0xff,0xd8])+bytes(16)+bytes([0xff,0xd9]),captured_at=wall[0]-30)    # thirty seconds old on arrival
    wall[0]-=3600;mono[0]+=10                                                               # the wall clock steps back an hour
    m=hub.metadata();assert m['capture_age_s']==40 and m['receipt_age_s']==10,m            # was: 0, an old frame read as new


# P3-6: a GET, HEAD or OPTIONS request carrying a body (declared, or chunked and so of unknown size, however large) is
# refused without one byte of it read; one without a body is passed on.
from engram.cortex import api
from .test_fix_luna_r7 import TA
def bodyless(method,headers):
    reads,sent=[],[]
    async def receive(): reads.append(1);return {'type':'http.request','body':b'x'*100000,'more_body':True}   # far past MAX_BODY
    async def send(m): sent.append(m)
    async def ok(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]});await send({'type':'http.response.body','body':b''})
    scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,'path':'/health','raw_path':b'/health',
           'root_path':'','scheme':'http','query_string':b'','client':('127.0.0.1',1),'server':('127.0.0.1',80),'headers':headers}
    asyncio.run(api.Guard(ok,credentials=[TA])(scope,receive,send))
    return sent[0]['status'],reads

@pytest.mark.parametrize('method',['GET','HEAD','OPTIONS'])
def test_a_bodyless_method_carrying_a_body_is_refused_unread(method):
    assert bodyless(method,[(b'transfer-encoding',b'chunked')])==(413,[])           # was: passed on, its size never limited
    assert bodyless(method,[(b'content-length',b'10')])==(413,[])
    assert bodyless(method,[])==(200,[])                                             # control: no body, passed on, nothing read
