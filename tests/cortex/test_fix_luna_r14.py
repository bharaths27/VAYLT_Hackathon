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
