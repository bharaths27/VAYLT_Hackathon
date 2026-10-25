import copy,json,threading,time
import pytest
from engram.cortex.types import *
from engram.cortex.store import Store
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.planning import Compiler
from engram.cortex.learning import Skills
from engram.cortex.models import ModelResult

def test_candidate_never_reused_before_validation_publication(store,rule):
    goal=Goal('fetch',('sample_b',),'bench'); skills=Skills(store)
    s=skills.create(Compiler(store).compile(goal))
    assert s['status']=='CANDIDATE' and skills.find(goal) is None
    with pytest.raises(Denied): skills.approve(s['id'],s['digest'],principal='carther')
    assert skills.test(s['id'])['ok']
    assert skills.find(goal) is None
    skills.approve(s['id'],s['digest'],principal='carther')
    assert skills.find(goal)['id']==s['id']

def test_automatic_publication_only_when_configured(store,rule):
    skills=Skills(store); s=skills.create(Compiler(store).compile(Goal('fetch',('sample_b',),'bench')))
    skills.test(s['id'])
    with pytest.raises(Denied): skills.approve(s['id'],s['digest'],principal='policy:'+store.policy.fingerprint(),automatic=True)

def test_skill_generation_and_reuse_without_model_weights(arena,clock):
    s=Store(policy=Policy(auto_publish_verified=True),clock=clock);s.register_arena(arena)
    b=Brain(s,FakeRobot(arena)); s.teach('Rule','Safety goggles must be at ppe station before handling Sample B.',owner='carther')
    p=b.command('Bring Sample B to bench',principal='carther')
    assert p['source']=='composed' and any(x['status']=='APPROVED' for x in b.skills.list())
    assert not b.robot.calls
    q=b.command('Bring Sample B to bench',principal='carther')
    assert q['source']=='skill' and q['planner_calls']==0
    assert not b.robot.calls
    b.close()

def test_changed_rule_revokes_old_skill_and_pending_recipe(store,rule):
    g=Goal('fetch',('sample_b',),'bench'); skills=Skills(store); x=skills.create(Compiler(store).compile(g)); skills.test(x['id']); skills.approve(x['id'],x['digest'],principal='carther')
    store.teach('New','Safety goggles must be at shelf B before handling Sample B.',owner='carther',supersedes=rule['source_id'])
    assert skills.find(g) is None and skills.get(x['id'])['status']=='REVOKED'

def test_skill_integrity_and_published_digest(store,rule):
    skills=Skills(store); x=skills.create(Compiler(store).compile(Goal('fetch',('sample_b',),'bench'))); skills.test(x['id'])
    with pytest.raises(Conflict): skills.approve(x['id'],'0'*64,principal='carther')
    d=x['definition'];d['steps'][-1]['args']['zone']='home'
    with store.tx():store.conn.execute('UPDATE skill_version SET definition_json=? WHERE id=?',(canonical(d),x['id']))
    with pytest.raises(CortexError): skills.approve(x['id'],x['digest'],principal='carther')

def test_idle_dream_self_proposes_practice_without_motors(brain):
    brain.store.teach('SOP','Safety goggles must be at ppe station before handling Sample B.',owner='carther')
    out=brain.dreamer.cycle(force=True)
    assert out['status']=='complete' and out['physical_actions']==0
    assert out['practice_ids'] and out['derived_ids']
    assert not brain.robot.calls
    assert brain.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='relationship'")['n']>0
    assert all(x['status']!='APPROVED' for x in brain.skills.list())

def test_no_dream_while_foreground_active(brain):
    with brain.activity.foreground(): assert brain.dreamer.cycle(force=True)['status']=='not_idle'
    assert not brain.robot.calls

def test_repeated_idle_pass_deduplicates(brain):
    brain.store.teach('SOP','Safety goggles must be at ppe station before handling Sample B.',owner='carther')
    first=brain.dreamer.cycle(force=True)
    counts=[brain.store.one('SELECT COUNT(*) n FROM '+table)['n'] for table in ('cx_derived','cx_practice','cx_skill')]
    second=brain.dreamer.cycle(force=True)
    assert [brain.store.one('SELECT COUNT(*) n FROM '+table)['n'] for table in ('cx_derived','cx_practice','cx_skill')]==counts

def test_dream_hypotheses_remain_candidates_and_invalid_refs_rejected(brain):
    src=brain.store.teach('SOP','Safety goggles must be at ppe station before handling Sample B.',owner='carther')
    class StubModel:
        def complete(self,purpose,bundle,**kwargs):
            return ModelResult({'candidates':[
                {'text':'Maybe inspection reduces errors.','refs':[bundle['allowed_refs'][0]]},
                {'text':'Secret permission','refs':['source:invented']}]},'stub','not-Qwen',0,'hash',1,'prompt')
    brain.dreamer.qwen=StubModel()
    r=brain.dreamer.cycle(force=True)
    rows=brain.store.rows("SELECT * FROM cx_derived WHERE kind='hypothesis'")
    assert len(rows)==1 and rows[0]['status']=='CANDIDATE'
    assert brain.store.one('SELECT COUNT(*) n FROM assertion')['n']==1

def test_new_foreground_discards_late_dream_model_result(brain):
    brain.store.teach('SOP','Safety goggles must be at ppe station before handling Sample B.',owner='carther')
    entered=threading.Event();release=threading.Event()
    class SlowModel:
        def complete(self,purpose,bundle,**kwargs):
            entered.set();release.wait(2)
            return ModelResult({'candidates':[{'text':'Late hypothesis','refs':[bundle['allowed_refs'][0]]}]},'stub','test',0,'hash',1,'prompt')
    brain.dreamer.qwen=SlowModel();result=[]
    t=threading.Thread(target=lambda:result.append(brain.dreamer.cycle(force=True)));t.start()
    assert entered.wait(2)
    with brain.activity.foreground(): release.set();t.join(3)
    assert result[0]['status']=='yielded'
    assert brain.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='hypothesis'")['n']==0

def test_summary_keeps_simulated_origin(brain):
    p=brain.command('Bring Sample A to bench',principal='carther');brain.approve(p['operation_id'],p['digest'],principal='carther')
    r=brain.dreamer.cycle(force=True)
    summary=brain.store.one("SELECT payload_json FROM cx_derived WHERE kind='summary'")
    assert json.loads(summary['payload_json'])['evidence_class']=='SIMULATED'

def test_idle_worker_really_runs_without_a_manual_cycle(arena):
    s=Store(policy=Policy(idle_after_s=.01));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    s.teach('SOP','Safety goggles must be at ppe station before handling Sample B.',owner='carther')
    b.dreamer.start(.02)
    for _ in range(100):
        if s.one('SELECT COUNT(*) n FROM cx_practice')['n']:break
        time.sleep(.01)
    assert s.one('SELECT COUNT(*) n FROM cx_practice')['n']>0
    assert not b.robot.calls
    b.close()

def test_idle_consolidation_eventually_visits_older_episodes(arena,clock):
    s=Store(clock=clock,policy=Policy(dream_batch=2));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    for i in range(6):
        p=b.command('Open the claw',principal='carther');b.approve(p['operation_id'],p['digest'],principal='carther');clock.advance(1)
    for _ in range(4):b.dreamer.cycle(force=True)
    assert s.one('SELECT COUNT(*) n FROM cx_consolidated')['n']==6
    b.close()

def test_idle_association_can_use_general_memory_without_robot_rules(brain):
    src=brain.store.teach('Owner preference','The operator prefers blue labels.',owner='carther',facts=[{'subject':'operator','predicate':'prefers','object':'blue labels'}])
    captured=[]
    class StubModel:
        def complete(self,purpose,bundle,**kwargs):
            captured.append(bundle)
            return ModelResult({'candidates':[{'text':'Blue labels may make inspection easier for this operator.','refs':['source:'+src['source_id']]}]},'stub','test',0,'hash',1,'prompt')
    brain.dreamer.qwen=StubModel();out=brain.dreamer.cycle(force=True)
    assert out['status']=='complete' and captured[0]['declared_knowledge']
    hypotheses=brain.store.rows("SELECT * FROM cx_derived WHERE kind='hypothesis'")
    assert len(hypotheses)==1 and hypotheses[0]['status']=='CANDIDATE'
    assert brain.store.one('SELECT COUNT(*) n FROM assertion')['n']==1
    assert not brain.robot.calls
