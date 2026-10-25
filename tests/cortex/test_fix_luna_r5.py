"""Regression tests for luna's round-5 review of PR 7 (each fails with its fix reverted; controls marked)."""
import json,threading,time
import pytest
from engram.cortex.integration import GuardedUpstream
from engram.cortex.learning import Dreamer
from engram.cortex.planning import Compiler,verify
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot,Result
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_dream import SOP,idle_brain

RULE_TEXT='Safety goggles must be at the ppe station before handling Sample B.'


# P1-1: conditional, hazard and permission wording fails closed: scoped to what it names, else to everything.
FAIL_CLOSED=[
    ('Shelf B can be used if a supervisor approves.',{'shelf_b'}),
    ('Shelf B may be used once a supervisor approves.',{'shelf_b'}),
    ('Shelf B is to be used only with permission.',{'shelf_b'}),
    ('Sample B is unsafe to move.',{'sample_b'}),
    ('Sample B is dangerous to handle.',{'sample_b'}),
    ('It is not safe to carry Sample A.',{'sample_a'}),
    ('Sample A can only be moved by a supervisor.',{'sample_a'}),
    ('Sample B cannot be moved without approval.',{'sample_b'}),
    ('The tray is not allowed on shelf B.',{'tray','shelf_b'}),
    ('Moving the goggles requires approval.',{'goggles'}),
    ('Sample A is fragile.',{'sample_a'}),
    ('Sample B may only be moved when approved.',{'sample_b'}),
    ('If the tray is wet, do not use it.',{'tray'}),
    ('Provided a supervisor is present, Sample A may be moved.',{'sample_a'}),
    ('Sample B is flammable.',{'sample_b'}),
    ('Everything may be moved if approved.',{'*'}),
    ('Nothing is safe to move today.',{'*'}),
    ('Lunch is served daily except Sunday.',{'*'}),
]
@pytest.mark.parametrize('sentence,scope',FAIL_CLOSED)
def test_conditional_hazard_and_permission_wording_fails_closed(store,sentence,scope):
    x=store.teach('Note',sentence,owner='carther')
    mentions=set().union(*[set(json.loads(r['mentions_json'])) for r in store.rows('SELECT mentions_json FROM cx_unresolved WHERE source_id=?',(x['source_id'],))])
    assert x['unresolved_rules'] and scope<=mentions,(sentence,mentions)
    goal=Goal('goto',zone='shelf_b') if 'shelf_b' in scope else Goal('fetch',(next(iter(sorted(scope-{'*'})),'sample_a'),),'bench')
    with pytest.raises(Missing,match='structured review'): Compiler(store).compile(goal)

@pytest.mark.parametrize('sentence',['Sample A is stored in fridge 2.','The tray is on the bench.','Lena prefers labels in blue ink.',
                                     'Safety goggles are kept at the ppe station.'])
def test_control_plain_notes_add_no_constraint(store,sentence):
    assert not store.teach('Note',sentence,owner='carther')['unresolved_rules']

def test_control_a_reviewers_ban_covers_a_hazard_sentence_about_exactly_that_object(store):
    assert not store.teach('Note','Sample B is unsafe to move.',owner='carther',rules=[],prohibitions=['sample_b'])['unresolved_rules']
    with pytest.raises(CortexError,match='prohibits'): Compiler(store).compile(Goal('fetch',('sample_b',),'bench'))


# P1-2: one lock serializes the STOP latch, reset and motion submission (barrier-controlled races).
class Upstream:
    def __init__(self):
        self.log=[];self.mu=threading.Lock();self.reset_gate=None;self.reset_entered=threading.Event();self.exec_gate=None
    def record(self,e):
        with self.mu:self.log.append(e)
    def execute(self,op,args):
        self.record('submitted')
        if self.exec_gate:self.exec_gate.wait(5)
        return Result('done')
    def stop(self):
        self.record('stop')
        if self.exec_gate:self.exec_gate.set()
    def reset_stop(self):
        self.reset_entered.set()
        if self.reset_gate:self.reset_gate.wait(5)
        self.record('reset')
    def state(self):return {}

def test_no_motion_is_submitted_after_stop_returns(store):
    up=Upstream();g=GuardedUpstream(up,store);checked,resume=threading.Event(),threading.Event()
    class PausingLatch(threading.Event):
        def is_set(self):
            v=super().is_set()
            if not checked.is_set():checked.set();resume.wait(5)     # the submitting thread pauses right after its check
            return v
    g._stopped=PausingLatch()
    t=threading.Thread(target=lambda:g.execute('go_to_zone',{'zone':'bench'}));t.start()
    assert checked.wait(5)
    s=threading.Thread(target=lambda:(g.stop(),up.record('stop_returned')));s.start()
    s.join(.5)                                                       # unserialized, STOP has returned by now
    resume.set();t.join(5);s.join(5)
    log=up.log;sub=[i for i,e in enumerate(log) if e=='submitted']
    assert 'stop_returned' in log and all(i<log.index('stop_returned') for i in sub),log   # nothing submitted after STOP returned
    assert max(i for i,e in enumerate(log) if e=='stop')>max(sub,default=-1),log         # and a stop reached the robot after it

def test_a_reset_racing_a_newer_stop_cannot_clear_it(store):
    up=Upstream();g=GuardedUpstream(up,store);g.stop();up.reset_gate=threading.Event()
    r=threading.Thread(target=g.reset_stop);r.start();assert up.reset_entered.wait(5)   # clearing the older stop
    s=threading.Thread(target=g.stop);s.start();s.join(.5)                               # a newer STOP arrives meanwhile
    up.reset_gate.set();r.join(5);s.join(5)
    assert g.execute('go_to_zone',{'zone':'bench'}).status=='rejected'                   # still in force
    assert up.log[-1]=='stop',up.log                                                     # the robot got it after the reset

def test_control_stop_still_interrupts_a_command_already_submitted(store):
    up=Upstream();up.exec_gate=threading.Event();g=GuardedUpstream(up,store);out=[]
    t=threading.Thread(target=lambda:out.append(g.execute('go_to_zone',{'zone':'bench'})));t.start()
    end=time.monotonic()+5
    while 'submitted' not in up.log and time.monotonic()<end:time.sleep(.01)
    s=threading.Thread(target=g.stop);s.start();s.join(5);t.join(5)
    assert not s.is_alive() and not t.is_alive() and up.log.index('stop')>up.log.index('submitted')
    assert g.execute('go_to_zone',{'zone':'bench'}).status=='rejected'


# P2-3: a commitment is its owner's, never general retrieval.
def test_commitments_stay_out_of_general_retrieval(brain):
    brain.schedule('the tray arrives','bring Sample A to the bench',principal='alice')
    assert not [h for h in brain.store.search('tray arrives bring Sample A bench') if 'tray arrives' in h['text']]
    assert 'tray arrives' not in json.dumps(brain.command('Where is the tray?',principal='bob'))
    chunk=brain.store.one("SELECT c.id FROM source_chunk c JOIN source s ON s.id=c.source_id WHERE s.kind='future'")['id']
    with pytest.raises(Missing): brain.store.evidence(chunk,principal='bob')
    assert brain.store.evidence(chunk,principal='alice')['text'].startswith('When the tray arrives')
    assert 'bring Sample A to the bench' in brain.command('What is pending?',principal='alice')['reply']


# P2-4: a planning bundle reads only the newest derived memories, in SQL.
def test_bundle_reads_only_the_newest_derived_memories(store,monkeypatch):
    with store.tx():
        store.conn.execute("INSERT INTO episode(id,goal,request,outcome,started_at) VALUES('anchor','{}','x','proposed',1)")
        for i in range(500):
            store.conn.execute('INSERT INTO cx_derived VALUES(?,?,?,?,?,?,?,?,?,?)',(f'mem{i}','summary','VERIFIED','{}','["episode:anchor"]','',f'fp{i}','test','episodic-summary-v1',i))
    seen=[];rows=store.rows
    def counting(sql,args=()):
        out=rows(sql,args)
        if 'cx_derived' in sql:seen.append(len(out))
        return out
    monkeypatch.setattr(store,'rows',counting)
    store.bundle('Bring Sample A to bench',Goal('fetch',('sample_a',),'bench'))
    assert seen and max(seen)<=32,max(seen)


# P2-5: arena fields are real types; malformed values are refused.
@pytest.mark.parametrize('field,value',[('graspable','false'),('graspable','true'),('graspable',0),('graspable',1),('graspable',None),
                                        ('width_mm','wide'),('width_mm',float('nan'))])
def test_malformed_arena_fields_are_refused(arena,field,value):
    arena['objects'][0][field]=value;s=Store()
    with pytest.raises(CortexError):s.register_arena(arena)
    s.close()

def test_control_a_real_false_graspable_is_kept(arena):
    arena['objects'][0]['graspable']=False;s=Store();s.register_arena(arena)       # sample_a
    c=Compiler(s);g=Goal('fetch',('sample_a',),'bench')
    steps=[{'op':'locate_tag','args':{'object_id':'sample_a'}},{'op':'pick_tagged_object','args':{'object_id':'sample_a'}},{'op':'place_at_zone','args':{'zone':'bench'}}]
    v=verify(g,steps,c.fixture(symbolic=True),[],s.policy)
    assert not v.ok and 'not graspable' in ' '.join(v.errors)
    s.close()


# P2-6: deleting a source removes every copy of its text, in the vault and on disk.
def test_deleting_a_source_removes_every_copy_of_its_text(tmp_path,arena,clock):
    mark='zqxmarker';s=Store(tmp_path/'v.db',clock=clock);s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    src=s.teach('Note',f'Tray must be at the bench before handling Sample A. The goggles are fragile {mark}. The tray code is {mark}.',owner='carther',
                facts=[{'subject':'tray','predicate':'colour','object':mark}],goal_definitions={f'prepare {mark}':Goal('fetch',('sample_a',),'bench').to_dict()})
    p=b.command('Bring Sample A to the shelf b.',principal='carther')               # its bundle quotes the source
    assert mark in json.dumps(p) and s.rows('SELECT reason FROM cx_unresolved')
    t=b.schedule(f'the {mark} arrives','bring Sample A to the bench',principal='carther')
    future=s.one('SELECT source_id FROM prospective_task WHERE id=?',(t['task_id'],))['source_id']
    s.revoke_source(src['source_id'],delete=True);s.revoke_source(future,delete=True)
    for (name,) in [tuple(r.values()) for r in s.rows("SELECT name FROM sqlite_master WHERE type='table'")]:
        for row in s.rows(f'SELECT * FROM "{name}"'):
            assert mark not in json.dumps(row,default=str).lower(),(name,row)
    b.close()
    for f in tmp_path.iterdir():
        assert mark.encode() not in f.read_bytes(),f.name


# P2-7: a full practice cap defers to its earliest expiry instead of logging a run every tick.
def test_a_full_practice_cap_defers_without_a_run_per_tick(arena,clock):
    b=idle_brain(arena,clock);b.store.teach('SOP',SOP,owner='op')
    with b.store.tx():
        for i in range(Dreamer.LIVE_CAP):
            b.store.conn.execute('INSERT INTO cx_practice VALUES(?,?,?,?,?,?,?,?)',(f'recent{i}','{}','{}','{}','PASS','symbolic-verifier-v1','SIMULATED',clock()))
    first=b.dreamer.cycle();assert first['status']=='complete' and not first['practice_ids']
    for _ in range(20):b.dreamer.cycle()                                              # the worker's ticks
    b.dreamer.start(interval_s=.01);time.sleep(.3);assert b.dreamer.stop()            # and the worker itself
    assert b.store.one('SELECT COUNT(*) n FROM cx_dream')['n']==1
    clock.advance(Dreamer.PRACTICE_WINDOW_S+1)                                          # the earliest case left the window
    practiced=lambda:b.store.one('SELECT COUNT(*) n FROM cx_practice')['n']
    b.dreamer.start(interval_s=.01);end=time.monotonic()+5                             # the worker resumes the practice
    while practiced()<=Dreamer.LIVE_CAP and time.monotonic()<end:time.sleep(.02)
    assert b.dreamer.stop() and practiced()>Dreamer.LIVE_CAP
    b.close()


# P3-8: invalidating a woken proposal settles its commitment in the same transaction.
def test_invalidation_settles_the_linked_commitment_at_once(brain):
    rule=brain.store.teach('Lab rule',RULE_TEXT,owner='carther')
    t=brain.schedule('the tray arrives','bring Sample B to the bench',principal='carther')
    p=brain.trigger('the tray arrives',event_id='ev1',principal='carther')[0]['proposal']
    task=lambda:brain.store.one('SELECT status FROM prospective_task WHERE id=?',(t['task_id'],))['status']
    assert task()=='fired'
    brain.store.teach('Lab rule v2','Safety goggles must be at shelf B before handling Sample B.',owner='carther',supersedes=rule['source_id'])
    assert brain.store.one('SELECT outcome FROM episode WHERE id=?',(p['operation_id'],))['outcome']=='invalidated' and task()=='failed'
