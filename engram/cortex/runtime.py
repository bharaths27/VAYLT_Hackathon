"""Canonical memory/action runtime: prepare != publish != approve != verify.

SQLite transactions govern local state. They do not make physical effects atomic.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .learning import Activity,Dreamer,Skills
from .models import IntentAdvisor,QwenClient,ModelUnavailable
from .planning import CAPS,PHYSICAL,RECALL,STOP_WORDS,Compiler,CompilerGap,asks_memory,parse_goal,safety_reason,validate_step,verify
from .store import Store
from .seeding import synthetic_seed_present
from . import vault_lock
from .types import Conflict,CortexError,Denied,Goal,Missing,Stale,canonical,digest,normalize,text,uid

WHEN=re.compile(r'^\s*(?:when|once|after|as soon as)\s+(.+?),\s*(?:then\s+)?(.+)$',re.I)


class Brain:
    CLOCK_JUMP_S=3600.0  # a larger forward step between sweeps needs operator confirmation
    CATCHUP_MAX=2        # overdue timers woken by such a step; the rest wait for the operator

    def __init__(self,store: Store,robot,*,qwen: QwenClient | None=None,intent: IntentAdvisor | None=None,
                 acquire_lock: bool=True):
        self.store,self.robot=store,robot
        self.qwen=qwen; self.intent=intent or IntentAdvisor(enabled=False)
        self.compiler=Compiler(store); self.skills=Skills(store); self.activity=Activity()
        self.dreamer=Dreamer(store,self.skills,self.activity,qwen,self.intent)
        self._recoverable_preparing=set(); self._effect_lock=threading.Lock(); self._stopped=threading.Event(); self._stop_gen=0; self._file_lock=None
        self.mode='SIMULATED' if robot.state().get('adapter')=='mock' else 'PHYSICAL'
        if self.mode=='PHYSICAL' and not store.policy.live_enabled:
            raise Denied('live adapter requires an explicit trusted enablement and witnessed hardware acceptance')
        if self.mode=='PHYSICAL' and synthetic_seed_present(store):
            raise Denied('this vault contains synthetic seed data (scripts/cortex_seed.py); a physical runtime must start on a separate, unseeded vault (set CORTEX_DB to a new file)')
        if acquire_lock and store.path!=':memory:' and store.file_lock is None:
            self._file_lock=vault_lock.acquire(store.path)
        # Unresolved physical effects must survive a restart, and one process must own the robot: a physical runtime
        # needs a file-backed vault that this process holds the exclusive lock of.
        if self.mode=='PHYSICAL' and (store.path==':memory:' or not (store.file_lock or self._file_lock)):
            raise Denied('a physical runtime needs a durable vault it holds the exclusive lock of: open a file-backed Store (CORTEX_DB)')
        store.runtime_mode=self.mode   # only a runtime that actually starts owns the vault's mode (load_seed checks it)
        self.recover()
        if qwen: store.manifest('qwen','parametric',qwen.manifest())
        store.manifest('intent','parametric',{'model':self.intent.model or None,'enabled':store.policy.cloud_enabled,
                       'provider':'typesafe','role':'advisory_only','live_inference_verified':False})
        store.manifest('runtime','policy',{'policy_hash':store.policy.fingerprint(),'mode':self.mode,'weights_updated':False})

    def close(self):
        if not self.dreamer.stop():
            raise Conflict('idle model call is still returning; retry close after it finishes')
        if self._file_lock: self._file_lock.close(); self._file_lock=None
        self.store.close()

    def recover(self):
        with self.store.tx():
            self._recoverable_preparing={r['id'] for r in self.store.rows("SELECT id FROM episode WHERE outcome='preparing' AND id IN (SELECT id FROM cx_episode)")}
            running=self.store.rows("SELECT id FROM episode WHERE outcome='running' AND id IN (SELECT id FROM cx_episode)")
            for r in running:
                self.store.settle(r['id'],'unknown',('running',),'Restart interrupted execution; no automatic replay')
                self.journal(r['id'],'recovery',{},'unknown','requires operator reconciliation')
            if running: self._latch('restart interrupted execution')
            if self.store.one("SELECT 1 FROM meta WHERE key='cortex_stop'"): self._stopped.set()
            self.store.conn.execute("UPDATE episode SET outcome='expired' WHERE outcome='proposed' AND id IN (SELECT id FROM cx_episode WHERE expires_at<?)",(self.store.clock(),))
            self.store.conn.execute("UPDATE cx_wake SET status='pending' WHERE status='preparing'")
            # Existing task links remain durable; recovery never dispatches physical effects.
            for r in self.store.rows("SELECT id,outcome FROM episode WHERE outcome IN ('unknown','success','failed','aborted','cancelled','rejected')"):
                self.store.finish_task(r['id'],r['outcome'])

    def _advisor(self,stage,state,questions,allowed=False):
        answer=self.intent.evaluate(stage,state,questions,disclosure_allowed=bool(allowed and self.store.policy.cloud_enabled))
        self.store.decision(stage,answer.get('provider','local'),answer.get('model'),answer['status']=='ok',answer['status'])
        return answer

    def command(self,request: str,*,principal: str,idempotency_key: str | None=None,cloud_allowed: bool=False) -> dict:
        request=text(request,'request',2000); principal=text(principal,'principal',100)
        with self.activity.foreground():
            low=normalize(request)
            # A stop wins before anything else, with the same vocabulary as the brain and the console (STOP_WORDS).
            if STOP_WORDS.search(request): return {'kind':'stop',**self.stop()}
            # Cheap deterministic controls/recall precede optional cloud classification.
            if RECALL.search(request):
                return self.store.recall(request,principal=principal)
            if low.startswith('remember '):
                body=re.sub(r'^remember\s+(?:that\s+)?','',request,flags=re.I)
                return self.store.teach('Instruction from '+principal,body,owner=principal,kind='spoken',cloud_ok=cloud_allowed)
            match=WHEN.match(request)
            if match and not request.rstrip().endswith('?'):
                return self.schedule(match[1],match[2],principal=principal)
            # Any other question asks memory ("what do you see" stays an observe proposal, "can you ..." an instruction).
            if asks_memory(request):
                answer=self.store.recall(request,principal=principal)
                if not asks_memory(request.rstrip().rstrip('?')):   # only the "?" made it a question: keep the instruction hint
                    answer['reply']='Answered as a question, so nothing was proposed; to have the robot act, say it without the question mark. '+answer['reply']
                return answer
            # Advisory interpretation cannot remove qualifiers or alter the original request.
            advice=self.intent.route(request,disclosure_allowed=bool(cloud_allowed and self.store.policy.cloud_enabled))
            self.store.decision('route',advice.get('provider','local'),advice.get('model'),advice['status']=='ok',advice['status'])
            if advice['status']=='ok' and (advice['scores'].get('negated',0)>=0.5 or advice['scores'].get('ambiguous',0)>=0.8):
                return {'kind':'proposal','status':'refused','reason':'Intent needs clarification; advisory signal is not permission.'}
            try: goal=parse_goal(request,self.store)
            except CortexError as e: return {'kind':'proposal','status':'refused','reason':str(e),'code':e.code}
            alias_source=self.store.one("SELECT g.source_id FROM cx_goal g JOIN cx_source s ON s.source_id=g.source_id WHERE g.name=? AND s.status='accepted'",(normalize(request),))
            alias_refs=['source:'+alias_source['source_id']] if alias_source else []
            return {'kind':'proposal',**self.propose(goal,request,principal=principal,idempotency_key=idempotency_key,extra_refs=alias_refs,cloud_allowed=cloud_allowed)}

    def propose(self,goal: Goal,request: str,*,principal: str,idempotency_key: str | None=None,
                extra_refs: list[str] | None=None,cloud_allowed: bool=False,wake: tuple | None=None) -> dict:
        goal=Goal.from_dict(goal.to_dict()); principal=text(principal,'principal',100)
        if safety_reason(request): raise Denied(safety_reason(request))
        request=text(request,'request',2000)
        ih=digest({'goal':goal.to_dict(),'request':request,'principal':principal,'extra_refs':extra_refs or []})
        key=principal+':'+text(idempotency_key,'idempotency key',200) if idempotency_key else None
        with self.store.tx():
            ep=None
            if key:
                prior=self.store.one('SELECT id,input_hash FROM cx_episode WHERE request_key=?',(key,))
                if prior:
                    if prior['input_hash']!=ih: raise Conflict('idempotency key reused with changed input')
                    if prior['id'] not in self._recoverable_preparing:
                        if wake: self._link(prior['id'],wake)
                        return self.operation(prior['id'])
                    ep=prior['id']; self._recoverable_preparing.remove(ep)
            t=self.store.clock(); basis=self.store.basis(goal)
            if ep is None:
                ep=uid('ep')
                self.store.conn.execute('INSERT INTO episode(id,goal,request,outcome,started_at) VALUES(?,?,?,?,?)',
                                        (ep,canonical(goal.to_dict()),request,'preparing',t))
                self.store.conn.execute('INSERT INTO cx_episode VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (ep,self.mode,canonical(goal.to_dict()),basis,self.store.policy.fingerprint(),canonical(self._fixture()),key,ih,t+self.store.policy.approval_ttl_s,principal))
                # Where the request came from (a task alias's or a commitment's source), kept from the start: deleting that
                # source finds this episode even when planning failed before any plan cited it.
                for ref in extra_refs or []:
                    if ref.startswith('source:'): self.store.conn.execute('INSERT OR IGNORE INTO cx_episode_source VALUES(?,?)',(ep,ref[len('source:'):]))
            else:
                self.store.conn.execute('UPDATE cx_episode SET basis_hash=?,initial_json=?,expires_at=? WHERE id=?',
                    (basis,canonical(self._fixture()),t+self.store.policy.approval_ttl_s,ep))
        with self.activity.foreground():
            try:
                # A command is an episode, not a source: it is never recalled as knowledge.
                refs=self.skills.provenance(goal,ep,extra_refs,principal=principal)
                bundle=self.store.bundle(request,goal,principal=principal)
                fixture=self._fixture()
                # Optional relevance advice ranks only explicitly cloud-permitted evidence.
                shareable=[e for e in bundle['evidence'] if e.get('cloud_ok')]
                if shareable:
                    self._advisor('relevance',{'request':request,'evidence':[{'id':e.get('id',e.get('chunk_id')),'text':e['text']} for e in shareable[:5]]},
                                  {'sufficient':'Does the supplied evidence directly support this request without missing a material condition?'},cloud_allowed)
                skill=self.skills.find(goal,principal)                  # only a procedure this principal may see
                # A stored procedure is reused only where every step still makes progress; otherwise compose afresh.
                if skill and not verify(goal,skill['definition']['steps'],fixture,self.store.related_rules(goal),self.store.policy).ok: skill=None
                if skill and self.store.cloud_eligible(skill['definition']['refs']):
                    a=self._advisor('skill_selection',{'request':request,'goal':goal.to_dict(),'skill':skill['definition']},
                        {'fits':'Does the skill achieve exactly the specified task without additional effects?'},cloud_allowed)
                    if a['status']=='ok' and a['scores']['fits']<0.6: skill=None
                calls=0; note=''; derived_id=None; source='composed'
                if skill:
                    plan=copy.deepcopy(skill['definition']); source='skill'
                else:
                    plan=None
                    if self.qwen and self.qwen.planning_order=='model_first':
                        calls+=1; plan,note=self._model_plan(goal,bundle,fixture)
                        if plan: source='model'
                    if plan is None:
                        try: plan=self.compiler.compile(goal,fixture); source='fallback' if calls else 'composed'
                        except CompilerGap as gap:
                            # Compiler first: a goal it cannot compose (its own plan fails the independent check, never a
                            # refusal) goes to the local model, whose plan meets the same refusals and the same check.
                            if not self.qwen or calls: raise
                            calls+=1; plan,note=self._model_plan(goal,bundle,fixture)
                            if plan is None: raise gap
                            source='model'
                    plan['refs']=sorted(set(plan['refs']+refs))
                    d=self.store.derive('task_recipe',{'task':goal.to_dict(),'steps':plan['steps'],'mode':'SIMULATED_VERIFICATION'},self.skills.identity_refs(goal,plan['refs']),
                        origin='new_task_synthesis',verified=True,verifier='symbolic-verifier-v1',basis=plan['basis'])
                    derived_id=d['id']
                    # Re-read the newly acquired task recipe: it is the execution input,
                    # not merely a decorative log written next to a transient plan.
                    loaded=self.store.derived(derived_id)
                    if loaded['status']!='VERIFIED': raise Denied('new recipe is not verified')
                    plan['steps']=loaded['payload']['steps']
                    if self.store.cloud_eligible(plan['refs']):
                        advice=self._advisor('candidate_review',{'request':request,'goal':goal.to_dict(),'steps':plan['steps']},
                            {'unsupported':'Does this procedure add an effect not requested by the typed goal?','ambiguous':'Is the specified task materially ambiguous?'},cloud_allowed)
                        if advice['status']=='ok' and max(advice['scores'].values())>=0.8:
                            raise Missing('advisory review raised an unresolved mismatch; clarification required')
                    candidate=self.skills.create(plan,mode='SIMULATED')
                    tested=candidate['validation'] if candidate['status']=='APPROVED' else self.skills.test(candidate['id'],fixture)
                    if tested['ok'] and candidate['status']!='APPROVED' and self.store.policy.auto_publish_verified:
                        self.skills.approve(candidate['id'],candidate['digest'],principal='policy:'+self.store.policy.fingerprint(),automatic=True)
                if self.store.basis(goal)!=basis: raise Stale('memory changed during planning')
                plan['refs']=sorted(set(plan['refs']+refs))
                checked=verify(goal,plan['steps'],fixture,self.store.related_rules(goal),self.store.policy,
                               expected_basis=plan['basis'],actual_basis=self.store.basis(goal))
                if not checked.ok or not self.store.refs_valid(plan['refs']): raise Stale('plan no longer satisfies current evidence')
                plan_digest=digest({k:v for k,v in plan.items() if k!='verification'})
                meta={'digest':plan_digest,'check':checked.to_dict(),'bundle':bundle,'source':source,'note':note,
                      'derived_memory_id':derived_id,'targets':list(goal.targets),'zone':goal.zone,'mode':self.mode,'composition_calls':0 if skill else 1}
                with self.store.tx():
                    self.store.conn.execute("UPDATE episode SET outcome='proposed',plan_json=?,evidence_json=?,planner_calls=?,skill_id=? WHERE id=? AND outcome='preparing'",
                        (canonical(plan),canonical(meta),calls,skill['id'] if skill else None,ep))
                    # A woken proposal becomes approvable only already linked to its commitment.
                    if wake: self._link(ep,wake)
                return self.operation(ep)
            except Exception as e:
                with self.store.tx(): self.store.conn.execute("UPDATE episode SET outcome='rejected',failure=?,finished_at=? WHERE id=? AND outcome='preparing'",(str(e)[:500] if isinstance(e,CortexError) else type(e).__name__,self.store.clock(),ep))
                if isinstance(e,CortexError): raise
                raise CortexError('planning failed: '+type(e).__name__) from None

    def _model_plan(self, goal: Goal, bundle: dict, fixture: dict) -> tuple[dict | None, str]:
        """The local model's plan for this goal, held to the compiler's refusals and its independent check: (plan, '')
        or (None, why not) when the model is unavailable or invalid, or its plan is rejected."""
        try: out=self.qwen.complete('plan',bundle,goal=goal.to_dict())
        except CortexError as e: return None,'Local model unavailable or invalid: '+type(e).__name__
        try: plan=self.compiler.compile(goal,fixture,steps=out.value['steps'])
        except CortexError as e:
            self.store.decision('plan','llama.cpp',out.model,False,'goal verification rejected model plan' if type(e) is Conflict else 'model plan refused: '+type(e).__name__)
            return None,'Model plan failed independent goal verification; composed from accepted rules.'
        self.store.decision('plan','llama.cpp',out.model,True,'verified')
        self.store.manifest('last_qwen_request','parametric',{'contract_sha256':out.contract_hash,'prompt_sha256':out.prompt_hash,
            'input_tokens':out.input_tokens,'model':out.model,'elapsed_ms':out.elapsed_ms,'response_accepted':True})
        plan['refs']=sorted(set(plan['refs']+out.refs))
        return plan,''

    def _fixture(self) -> dict:
        """The verification world: current observations plus what the adapter reports in the claw (absent = unknown)."""
        f=self.compiler.fixture(); st=self.robot.state()
        if 'holding' in st: f.update(holding=st['holding'],holding_known=True)
        return f

    def operation(self,ep: str) -> dict:
        r=self.store.one('SELECT e.*,c.mode,c.owner,c.expires_at FROM episode e JOIN cx_episode c ON c.id=e.id WHERE e.id=?',(ep,))
        if not r: raise Missing('operation not found')
        p=json.loads(r['plan_json']) if r['plan_json'] else None
        m=json.loads(r['evidence_json'] or '{}')
        return {'operation_id':ep,'status':r['outcome'],'outcome':r['outcome'],'request':r['request'],'plan':p,
                'digest':m.get('digest'),'check':m.get('check'),'memory':m.get('bundle'),'source':m.get('source'),
                'planner_note':m.get('note'),'planner_calls':r['planner_calls'],'mode':r['mode'],
                'failure':r['failure'],'owner':r['owner'],'expires_at':r['expires_at'],
                'skill_id':r['skill_id'],'derived_memory_id':m.get('derived_memory_id')}

    def pending(self):
        with self.store.tx():
            self.store.conn.execute("UPDATE episode SET outcome='expired' WHERE outcome='proposed' AND id IN (SELECT id FROM cx_episode WHERE expires_at<=?)",(self.store.clock(),))
        with self.store.tx():
            for r in self.store.rows("SELECT id,outcome FROM episode WHERE outcome IN ('expired','invalidated')"):self.store.finish_task(r['id'],r['outcome'])
        return [self.operation(r['id']) for r in self.store.rows("SELECT id FROM episode WHERE outcome='proposed' AND id IN (SELECT id FROM cx_episode) ORDER BY started_at DESC")]

    def journal(self,ep: str | None,op: str,args: dict,status: str,detail: str='',step: int=0):
        with self.store.tx():
            self.store.conn.execute('INSERT INTO operation_event(operation_id,episode_id,op,args_json,status,detail,at) VALUES(?,?,?,?,?,?,?)',
                (f'{ep or "system"}:{step}',ep,op,canonical(args),status,detail[:1000],self.store.clock()))

    def approve(self,ep: str, supplied_digest: str,*,principal: str) -> dict:
        text(principal,'authenticated approver',100)
        if not self._effect_lock.acquire(blocking=False): raise Conflict('another operation is executing')
        try:
            with self.activity.foreground():
                with self.store.tx():
                    r=self.operation(ep); plan=r['plan']
                    if r['owner']!=principal: raise Denied('approver does not own this operation')
                    if r['status']!='proposed': raise Conflict('operation is not awaiting approval')
                    # The durable latch is the authority; the event and the adapter flag are faster copies of it. An adapter
                    # that does not report its stop state as an explicit False may be stopped: no motion either.
                    adapter=self.robot.state().get('stopped')
                    if self._stopped.is_set() or adapter or self.store.one("SELECT 1 FROM meta WHERE key='cortex_stop'"): raise Denied('stop is latched')
                    if adapter is not False: raise Denied('the adapter does not report its stop state; motion is refused')
                    self._require_reconciled('new motion')
                    if self.store.clock()>=r['expires_at']: raise Stale('proposal expired')
                    actual=digest({k:v for k,v in plan.items() if k!='verification'})
                    if supplied_digest!=actual or supplied_digest!=r['digest']: raise Conflict('approval does not match this exact plan')
                    goal=Goal.from_dict(plan['goal'])
                    changed=self._provenance_changed(r,plan)
                    if changed: raise Stale(changed+' after proposal')
                    if plan['basis']!=self.store.basis(goal) or plan['policy_hash']!=self.store.policy.fingerprint() or not self.store.refs_valid(plan['refs']):
                        raise Stale('rule, binding, source or policy changed after proposal')
                    if self.store.prohibitions(set(goal.targets)|{r['object'] for r in self.store.related_rules(goal)}) and goal.kind=='fetch': raise Denied('current prohibition blocks this operation')
                    # The live world decides too: what the claw holds now is what a claw primitive handles, with its constraints.
                    world=self._fixture()
                    try: self.compiler.refuse(goal,world,self.store.related_rules(goal))
                    except CortexError as e: raise Denied(str(e)) from None
                    v=verify(goal,plan['steps'],world,self.store.related_rules(goal),self.store.policy)
                    if not v.ok: raise Denied('; '.join(v.errors))
                    claimed=self.store.conn.execute("UPDATE episode SET outcome='running' WHERE id=? AND outcome='proposed'",(ep,)).rowcount
                    if claimed!=1: raise Conflict('operation already claimed')
                    now=self.store.clock()
                    self.store.conn.execute('INSERT INTO approval VALUES(?,?,?,?,?,?,?,?)',(uid('apv'),'plan',ep,actual,principal,now,r['expires_at'],now))
                return self._execute(ep,goal,plan)
        finally: self._effect_lock.release()

    def _provenance_changed(self,r: dict,plan: dict) -> str | None:
        """Why the recipe or approved skill a plan came from no longer backs it (None while it does). Checked at
        approval and again before every dispatched step."""
        if r['derived_memory_id']:
            recipe=self.store.derived(r['derived_memory_id'])
            if recipe['status']!='VERIFIED' or recipe['payload'].get('steps')!=plan['steps'] or not self.store.refs_valid(recipe['refs']):
                return 'execution recipe changed or lost its evidence'
        if r['skill_id']:
            skill=self.store.one('SELECT status,definition_json FROM skill_version WHERE id=?',(r['skill_id'],))
            if not skill or skill['status']!='APPROVED' or json.loads(skill['definition_json']).get('steps')!=plan['steps']:
                return 'procedure was revoked or changed'
        return None

    def _execute(self,ep: str,goal: Goal,plan: dict) -> dict:
        steps=[]; outcome='success'; reason=None; seen=set(); tags={o['tag']:o['id'] for o in self.store.objects()}; moved=False
        op=self.operation(ep)
        try:
            for i,s in enumerate(plan['steps']):
                if self._stopped.is_set(): outcome='aborted'; reason='stop requested'; break
                if self.store.basis(goal)!=plan['basis'] or not self.store.refs_valid(plan['refs']):
                    raise Stale('evidence changed while executing')
                validate_step(s,set(tags.values()),self.store.zones())
                # Observe/location steps must actually return a current observation before picking.
                if s['op']=='pick_tagged_object':
                    oid=s['args']['object_id']
                    if oid not in seen: raise Missing('no fresh in-operation observation for pickup')
                    for rule in self.store.related_rules(goal):
                        if rule['subject']==oid:
                            obj=next(o for o in self.store.objects() if o['id']==rule['object'])
                            if not obj['fresh'] or obj['last_zone']!=rule['required_zone']:
                                raise Missing('prerequisite not currently observed at required station')
                self.journal(ep,s['op'],s['args'],'pending',step=i)
                # The recipe or skill behind this plan must still back it at every step: a revocation mid-run stops it.
                changed=self._provenance_changed(op,plan)
                if changed: raise Stale(changed+' while executing')
                # STOP interlock: the last check sits immediately before dispatch (no validation or I/O in between),
                # and the adapter refuses motion once stop() reached it, so a STOP in this gap cannot dispatch the step.
                if self._stopped.is_set():
                    self.journal(ep,s['op'],s['args'],'not_dispatched','stop arrived before dispatch',i); outcome='aborted'; reason='stop requested'; break
                moved=moved or s['op'] in PHYSICAL
                try: result=self.robot.execute(s['op'],s['args'])
                except Exception:
                    self.journal(ep,s['op'],s['args'],'unknown','adapter exception; no automatic retry',i)
                    outcome='unknown'; reason='adapter exception after possible effect'; break
                for oid,proof in getattr(result,'location_evidence',{}).items():
                    current=next((x for x in self.store.objects() if x['id']==oid),None)
                    if current and current['fresh'] and current['last_observation_id']==proof.get('observation_id') and current['last_seen_at']==proof.get('captured_at') and current['last_zone']==proof.get('zone'):
                        seen.add(oid)
                for tag,zone in getattr(result,'observations',[]):
                    oid=tags.get(tag)
                    if oid and self.mode=='SIMULATED':
                        self.store.observe(oid,zone,origin='robot_adapter',mode=self.mode,evidence=f'{ep}:{i}')
                        seen.add(oid)
                reported=str(getattr(result,'status','unknown'))
                self.journal(ep,s['op'],s['args'],'reported_done' if reported=='done' else reported,getattr(result,'detail',''),i)
                steps.append({**s,'status':reported,'detail':getattr(result,'detail','')})
                if reported!='done': outcome='unknown' if reported not in {'failed','rejected'} else 'failed'; reason='adapter outcome: '+reported; break
            if outcome=='success':
                if self.mode=='SIMULATED':
                    state=self.robot.state()
                    if goal.kind=='fetch' and any(state.get('objects',{}).get(o)!=goal.zone for o in goal.targets):
                        outcome='unknown'; reason='reported success did not match final simulated world'
                    elif goal.kind=='goto' and state.get('at_zone')!=goal.zone:
                        outcome='unknown'; reason='destination not established'
                    elif goal.kind=='inspect' and not set(goal.targets)<=seen:
                        outcome='unknown'; reason='inspection produced no matching observed evidence'
                    elif goal.kind=='primitive' and goal.primitive in {'gripper_open','gripper_close'} and state.get('gripper_open') is not (goal.primitive=='gripper_open'):
                        outcome='unknown'; reason='gripper state does not establish the requested effect'
                else:
                    # The real adapter's HTTP/servo ACK is not independent physical proof.
                    outcome='needs_verification'; reason='operator or independent sensor must attest the physical postcondition'
        except CortexError as e: outcome='aborted'; reason=str(e)
        except Exception: outcome='unknown'; reason='unexpected execution error; no retry'
        if outcome in {'failed','aborted'} and moved and self.mode=='PHYSICAL':
            # A dispatched motion step may have partly changed the world: unresolved until the operator attests it.
            outcome='unknown'; reason=f'{reason}; a motion step was dispatched, physical state unverified'
        if outcome in {'unknown','aborted','failed'}:
            self._stopped.set()
            try: self.robot.stop()
            except Exception: reason=(reason or '')+'; stop not confirmed'
        with self.store.tx():
            if outcome in {'unknown','aborted','failed'}: self._latch(f'{ep} ended {outcome}')
            self.store.settle(ep,outcome,('running',),reason)
        return {**self.operation(ep),'steps':steps}

    def verify_outcome(self,ep: str,*,principal: str,ok: bool,note: str):
        note=text(note,'verification evidence',1000); outcome='success' if ok else 'failed'
        # Check and write in one transaction: a second submission finds nothing left to verify.
        with self.store.tx():
            if self.operation(ep)['owner']!=principal: raise Denied('verification principal does not own the operation')
            if not self.store.settle(ep,outcome,('needs_verification','unknown'),'operator observation: '+note): raise Conflict('no unresolved effect to verify')
            self.journal(ep,'verify',{},outcome,principal+': '+note)
        return self.operation(ep)

    def reject(self,ep: str,*,principal: str):
        with self.store.tx():
            if self.operation(ep)['owner']!=principal: raise Denied('not the operation owner')
            if not self.store.settle(ep,'cancelled',('preparing','proposed')): raise Conflict('cannot reject an executing or completed operation')
        return self.operation(ep)

    def stop(self):
        # The physical STOP goes out first, on a path that takes no store lock and no lock the planner holds: a held
        # transaction or stalled storage never delays it for an action already dispatched (the adapter latches it too).
        # The in-memory latch is set before it, so nothing new is dispatched; the durable latch follows, even when the
        # robot call itself failed or ended the process's call (re-raised once the latch is stored). If storing fails,
        # the robot stays stopped, the in-memory latch stays set, and the storage error reaches the caller.
        self._stop_gen+=1; self._stopped.set(); sent=False; crash=None
        try: self.robot.stop(); sent=True
        except Exception: pass
        except BaseException as e: crash=e
        self.activity.touch()
        with self.store.tx():
            self._latch('operator stop')
            for r in self.store.rows("SELECT id FROM episode WHERE outcome IN ('preparing','proposed') AND id IN (SELECT id FROM cx_episode)"):
                self.store.settle(r['id'],'cancelled',('preparing','proposed'),'stop revoked pending proposal')
        if crash is not None: raise crash
        try: confirmed=sent and bool(self.robot.state().get('stopped'))
        except Exception: confirmed=False
        self.journal(None,'stop',{},'confirmed' if confirmed else 'requested')
        return {'stop_requested':True,'stop_confirmed':confirmed,'auto_resume':False}

    def clear_stop(self,*,principal: str):
        principal=text(principal,'operator',100); gen=self._stop_gen
        self._require_reconciled('rearming')
        # The durable latch stays until the adapter confirmed the reset. A reset that fails or times out (perhaps after
        # the adapter re-armed) or that the adapter does not report leaves the vault latched, so a restart still refuses
        # motion, and the robot is stopped again. Motion stays refused meanwhile: approval reads the latch and the event.
        try:
            self.robot.reset_stop(); s=self.robot.state().get('stopped')                  # only an explicit False confirms it
            why=None if s is False else 'it still reports stopped' if s else 'it does not report its stop state'
        except Exception as e: why=type(e).__name__
        if why:
            self._restop(); raise Conflict(f'the adapter did not confirm the reset ({why}); the robot stays stopped and the stop stays latched')
        # Then the latch is cleared in one transaction. A refused COMMIT, or a stop or failure latch that arrived
        # meanwhile, wins: the robot is stopped again and nothing is cleared.
        try:
            with self.store.tx():
                if self._stop_gen!=gen: raise Conflict('a stop arrived while the latch was being cleared; the robot stays stopped')
                self._require_reconciled('rearming')
                self.store.conn.execute("DELETE FROM meta WHERE key='cortex_stop'")
                self.journal(None,'clear_stop',{},'cleared',principal)
        except BaseException:
            self._restop(); raise
        self._stopped.clear()
        if self._stop_gen!=gen: self._stopped.set()          # a stop landing after the commit keeps the event set as well
        self.activity.touch()
        return self.robot.state()

    def _restop(self):
        self._stopped.set()
        try: self.robot.stop()
        except Exception: pass

    def _latch(self,reason: str):
        # The stop latch is durable: recover() restores it, so a restart cannot silently re-arm motion.
        self._stop_gen+=1; self._stopped.set()
        with self.store.tx(): self.store.conn.execute("INSERT OR IGNORE INTO meta VALUES('cortex_stop',?)",(canonical({'at':self.store.clock(),'reason':reason}),))

    def _require_reconciled(self,action: str):
        # Cortex operations only, as operation() and verify_outcome() can see them; ids let the operator reconcile each one.
        ids=[r['id'] for r in self.store.rows("SELECT e.id FROM episode e JOIN cx_episode c ON c.id=e.id WHERE e.outcome IN ('running','unknown','needs_verification') ORDER BY e.started_at,e.id")]
        if ids: raise Denied(f'reconcile unresolved operations before {action}: '+', '.join(ids[:10])+(f' and {len(ids)-10} more' if len(ids)>10 else ''))

    def schedule(self,trigger: str,request: str,*,principal: str,expires_at: float | None=None) -> dict:
        trigger=text(trigger,'trigger',200); request=text(request,'future task',2000)
        if RECALL.search(request): raise Missing('a question is not a future action')
        principal=text(principal,'task owner',100)
        if expires_at is not None:
            from .types import finite
            expires_at=finite(expires_at,'expiry',self.store.clock(),self.store.clock()+366*86400)
        trig=normalize(trigger); goal=normalize(request)
        with self.store.tx():
            prior=self.store.one("SELECT p.id FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.trigger=? AND p.goal=? AND c.owner=? AND p.status='waiting'",(trig,goal,principal))
            if prior: return {'kind':'scheduled','task_id':prior['id'],'created':False,'trigger':trig,'request':request}
            src=self.store.teach('Future instruction',f'When {trigger}, {request}',owner=principal,kind='future',rules=[])
            tid=uid('fut')
            self.store.conn.execute('INSERT INTO prospective_task(id,trigger,goal,request,source_id,status,created_at) VALUES(?,?,?,?,?,?,?)',(tid,trig,goal,request,src['source_id'],'waiting',self.store.clock()))
            self.store.conn.execute('INSERT INTO cx_commitment VALUES(?,?,1,?,1)',(tid,principal,expires_at))
        return {'kind':'scheduled','task_id':tid,'created':True,'trigger':trig,'request':request}

    def schedule_at(self,due_at: float,request: str,*,principal: str) -> dict:
        from .types import finite
        if not self.store.policy.clock_trusted: raise Denied('timed commitments require a trusted UTC clock policy')
        due=finite(due_at,'UTC due time',0,self.store.clock()+366*86400)
        trigger='timer '+digest({'due_at':due,'request':request,'owner':principal})
        with self.store.tx():
            result=self.schedule(trigger,request,principal=principal)
            self.store.conn.execute('INSERT OR IGNORE INTO cx_due VALUES(?,?)',(result['task_id'],due))
        return {**result,'due_at':due}

    def sweep_due(self) -> list[dict]:
        if not self.store.policy.clock_trusted: return []
        now=self.store.clock(); limit=8
        with self.store.tx():
            last=self.store.one("SELECT value FROM meta WHERE key='cortex_last_utc'")
            if self.store.one("SELECT 1 FROM meta WHERE key='cortex_clock_hold'"): raise Stale('UTC clock jumped forward; operator must re-establish trusted time')
            if last and now<float(last['value'])-1: raise Stale('UTC clock moved backward; operator must re-establish trusted time')
            if last and now-float(last['value'])>self.CLOCK_JUMP_S:
                # Bad NTP, an RTC-less boot or a long outage: wake a bounded few overdue timers, hold the rest for the operator.
                jump={'last_utc':float(last['value']),'utc':now}
                self.store.conn.execute("INSERT INTO meta VALUES('cortex_clock_hold',?)",(canonical(jump),))
                self.journal(None,'clock_jump',jump,'held','operator must re-establish trusted time'); limit=self.CATCHUP_MAX
            self.store.conn.execute("INSERT INTO meta VALUES('cortex_last_utc',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
        due=self.store.rows("SELECT p.id,p.trigger,c.owner,c.generation,d.due_at FROM prospective_task p JOIN cx_commitment c ON c.id=p.id JOIN cx_due d ON d.task_id=p.id WHERE p.status='waiting' AND d.due_at<=? ORDER BY d.due_at LIMIT ?",(now,limit))
        results=[]
        for task in due:
            results+=self.trigger(task['trigger'],event_id='due:'+task['id']+':'+str(task['generation']),principal=task['owner'])
        return results

    def reestablish_time(self,*,principal: str,note: str) -> dict:
        """Operator attests the UTC clock is correct again: resets the high-water mark and releases a jump hold."""
        principal=text(principal,'operator',100); note=text(note,'time evidence',1000)
        if not self.store.policy.clock_trusted: raise Denied('timed commitments require a trusted UTC clock policy')
        now=self.store.clock()
        with self.store.tx():
            last=self.store.one("SELECT value FROM meta WHERE key='cortex_last_utc'"); prior=float(last['value']) if last else None
            self.store.conn.execute("INSERT INTO meta VALUES('cortex_last_utc',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
            self.store.conn.execute("DELETE FROM meta WHERE key='cortex_clock_hold'")
            self.journal(None,'clock_reestablished',{'previous_utc':prior,'utc':now},'confirmed',principal+': '+note)
        return {'utc':now,'previous_utc':prior,'reestablished_by':principal}

    def cancel_task(self,tid: str,*,principal: str) -> dict:
        with self.store.tx():
            r=self.store.one('SELECT p.*,c.owner FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.id=?',(tid,))
            if not r: raise Missing('task not found')
            if r['owner']!=principal: raise Denied('not the task owner')
            if r['status']=='done': raise Conflict('cannot cancel an already completed task')
            if not self.store.withdraw_commitment(tid,'commitment cancelled'):
                raise Conflict('effect may have started; stop/reconcile it, do not claim cancellation')
        return {'task_id':tid,'status':'cancelled'}

    def trigger(self,trigger: str,*,event_id: str,principal: str) -> list[dict]:
        trig=normalize(text(trigger,'trigger',200)); text(event_id,'event id',160)
        payload_hash=digest({'trigger':trig,'principal':principal})
        with self.store.tx():
            prior=self.store.one('SELECT * FROM cx_event WHERE id=?',(event_id,))
            if prior and prior['payload_hash']!=payload_hash: raise Conflict('event id reused for different input')
            if not prior:
                self.store.conn.execute('INSERT INTO cx_event VALUES(?,?,?,?,?)',(event_id,trig,payload_hash,'operator:'+principal,self.store.clock()))
                tasks=self.store.rows("SELECT p.*,c.generation,c.expires_at FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.trigger=? AND p.status='waiting' AND c.owner=?",(trig,principal))
                for task in tasks:
                    if task['expires_at'] is not None and self.store.clock()>=task['expires_at']:
                        self.store.conn.execute("UPDATE prospective_task SET status='expired' WHERE id=?",(task['id'],)); continue
                    if not self.store.refs_valid(['source:'+task['source_id']]): continue
                    self.store.conn.execute("UPDATE prospective_task SET status='fired',fired_at=? WHERE id=?",(self.store.clock(),task['id']))
                    self.store.conn.execute('INSERT INTO cx_wake VALUES(?,?,?,?,?,?)',(event_id,task['id'],task['generation'],'pending',None,None))
        return self.reconcile_wakes(event_id=event_id)

    def reconcile_wakes(self,*,event_id: str | None=None) -> list[dict]:
        rows=self.store.rows('SELECT * FROM cx_wake'+(' WHERE event_id=?' if event_id else ''),(event_id,) if event_id else ())
        out=[]
        for w in rows:
            task=self.store.one('SELECT p.*,c.owner,c.generation FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.id=?',(w['task_id'],))
            if w['status']=='linked':
                out.append({'task_id':task['id'],'proposal':self.operation(w['proposal_id'])}); continue
            if w['status']!='pending' or task['status']!='fired' or task['generation']!=w['generation']: continue
            key=(w['event_id'],task['id'],w['generation'])
            with self.store.tx():
                claim=self.store.conn.execute("UPDATE cx_wake SET status='preparing' WHERE event_id=? AND task_id=? AND generation=? AND status='pending'",key).rowcount
            if not claim: continue
            try:
                goal=parse_goal(task['request'],self.store)
                proposal=self.propose(goal,task['request'],principal=task['owner'],
                    idempotency_key=f"wake:{task['id']}:{w['generation']}",extra_refs=['source:'+task['source_id']],wake=key)
                if self.store.one('SELECT status FROM cx_wake WHERE event_id=? AND task_id=? AND generation=?',key)['status']=='linked':
                    out.append({'task_id':task['id'],'proposal':proposal})
            except CortexError as e:
                with self.store.tx():
                    self.store.conn.execute("UPDATE cx_wake SET status='failed',error=? WHERE event_id=? AND task_id=? AND generation=?",(str(e),w['event_id'],task['id'],w['generation']))
                    self.store.conn.execute("UPDATE prospective_task SET status='failed' WHERE id=? AND status='fired'",(task['id'],))
                out.append({'task_id':task['id'],'error':str(e)})
        return out

    def _link(self,ep: str,wake: tuple) -> bool:
        # Caller holds the transaction. Link only a still-fired commitment of this generation; otherwise withdraw the proposal.
        event_id,tid,gen=wake
        cur=self.store.one('SELECT p.status,c.generation FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.id=?',(tid,))
        if not cur or cur['status']!='fired' or cur['generation']!=gen:
            self.store.conn.execute("UPDATE cx_wake SET status='cancelled' WHERE event_id=? AND task_id=? AND generation=? AND status IN ('pending','preparing')",wake)
            self.store.settle(ep,'cancelled',('preparing','proposed'),'commitment withdrawn before link')
            return False
        self.store.conn.execute('UPDATE prospective_task SET proposal_id=? WHERE id=?',(ep,tid))
        self.store.conn.execute("UPDATE cx_wake SET status='linked',proposal_id=? WHERE event_id=? AND task_id=? AND generation=?",(ep,*wake))
        self.store.finish_task(ep,self.store.one('SELECT outcome FROM episode WHERE id=?',(ep,))['outcome'])
        return True

    def metrics(self,principal: str | None=None):
        # With a principal, episode records are that owner's only, as operation reads and /events show them.
        return {'episodes':self.store.rows('SELECT e.id,e.outcome,e.planner_calls,e.skill_id FROM episode e LEFT JOIN cx_episode c ON c.id=e.id WHERE (? IS NULL OR c.owner=?) ORDER BY e.started_at',(principal,principal)),
                'derived':self.store.rows('SELECT kind,status,COUNT(*) n FROM cx_derived GROUP BY kind,status'),
                'skills':self.store.rows('SELECT status,COUNT(*) n FROM skill_version GROUP BY status'),
                'practice':self.store.rows('SELECT status,COUNT(*) n FROM cx_practice GROUP BY status'),
                'mode':self.mode,'weights_updated':False,'epoch':self.store.epoch(),
                'models':self.store.rows('SELECT * FROM cx_manifest')}
