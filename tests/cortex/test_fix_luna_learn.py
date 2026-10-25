"""Regression tests for the luna LEARN review of PR 7 (each fails on 3cd807c)."""
import json
import pytest
from engram.cortex.learning import Dreamer
from engram.cortex.models import IntentAdvisor,QwenClient,model_context,prompt_refs
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot
from engram.cortex.store import Store
from engram.cortex.types import *
from .test_fix_learn_dream import SOP,idle_brain


# P1 as stated is not reproducible: an applicable skill reaches the prompt only as id/name/digest (store.bundle),
# never its definition, steps or refs, so no source text arrives that way. Hardening: those rows cannot be cited
# by prompt_refs, so they no longer enter the model context at all; the prompt holds only citable memory.
def test_model_context_holds_only_citable_memory_not_skill_rows(arena,clock):
    s=Store(clock=clock,policy=Policy(auto_publish_verified=True));s.register_arena(arena);b=Brain(s,FakeRobot(arena))
    src=s.teach('Lab rule',SOP,owner='op')
    assert b.command('Bring Sample B to bench',principal='op')['skill_id'] is None      # composed, then auto-published
    goal=Goal('fetch',('sample_b',),'bench'); bundle=s.bundle('Bring Sample B to bench',goal)
    assert [set(k) for k in bundle['applicable_skills']]==[{'id','name','digest'}]      # the premise: no definition text
    ctx=model_context(bundle)
    assert 'applicable_skills' not in ctx and bundle['applicable_skills'][0]['id'] not in json.dumps(ctx)
    assert 'source:'+src['source_id'] in prompt_refs(ctx)
    b.close()


# P2: the rolling practice cap leaves a backlog, so the watermark must not hide the starved goals.
def test_practice_cap_keeps_the_backlog_open_until_the_window_rolls(arena,clock):
    b=idle_brain(arena,clock); b.store.teach('SOP',SOP,owner='op')
    with b.store.tx():
        for i in range(Dreamer.LIVE_CAP):
            b.store.conn.execute('INSERT INTO cx_practice VALUES(?,?,?,?,?,?,?,?)',(f'recent{i}','{}','{}','{}','PASS','symbolic-verifier-v1','SIMULATED',clock()))
    first=b.dreamer.cycle()
    assert first['status']=='complete' and not first['practice_ids']                    # capped this cycle
    clock.advance(Dreamer.PRACTICE_WINDOW_S+1)                                             # those cases leave the rolling window
    second=b.dreamer.cycle()
    assert second['status']=='complete' and second['practice_ids']                       # was: no_new_evidence, starved for good
    b.close()


# P2: a plan request needs a recognized typed goal; refused before any model request, never a permissive grammar.
@pytest.mark.parametrize('goal',[None,{},{'kind':'dance'},{'kind':'primitive','primitive':'self_destruct'}])
def test_plan_without_a_typed_goal_is_refused_before_any_model_call(goal):
    calls=[]
    q=QwenClient(transport=lambda url,body,headers,timeout:calls.append(url) or {})
    with pytest.raises(Missing,match='typed goal'): q.complete('plan',{'facts':[]},goal=goal)
    assert not calls


# P3: the hosted intent model id comes only from the environment; no constructor override.
def test_intent_model_id_has_no_constructor_override(monkeypatch):
    monkeypatch.setenv('TYPESAFE_INTENT_MODEL','env-model')
    with pytest.raises(TypeError): IntentAdvisor(model='caller-chosen-model')
    assert IntentAdvisor(enabled=True,api_key='TEST').model=='env-model'
