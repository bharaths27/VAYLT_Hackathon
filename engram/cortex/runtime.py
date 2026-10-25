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
from .planning import CAPS,PHYSICAL,RECALL,Compiler,parse_goal,safety_reason,validate_step,verify
from .store import Store
from . import vault_lock
from .types import Conflict,CortexError,Denied,Goal,Missing,Stale,canonical,digest,normalize,text,uid

WHEN=re.compile(r'^\s*(?:when|once|after|as soon as)\s+(.+?),\s*(?:then\s+)?(.+)$',re.I)


class Brain:
    def __init__(self,store: Store,robot,*,qwen: QwenClient | None=None,intent: IntentAdvisor | None=None,
                 acquire_lock: bool=True):
        self.store,self.robot=store,robot
        self.qwen=qwen; self.intent=intent or IntentAdvisor(enabled=False)
        self.compiler=Compiler(store); self.skills=Skills(store); self.activity=Activity()
        self.dreamer=Dreamer(store,self.skills,self.activity,qwen,self.intent)
        self._recoverable_preparing=set(); self._effect_lock=threading.Lock(); self._stopped=threading.Event(); self._file_lock=None
        self.mode='SIMULATED' if robot.state().get('adapter')=='mock' else 'PHYSICAL'
        store.runtime_mode=self.mode
        if self.mode=='PHYSICAL' and not store.policy.live_enabled:
            raise Denied('live adapter requires an explicit trusted enablement and witnessed hardware acceptance')
        if acquire_lock and store.path!=':memory:':
            self._file_lock=vault_lock.acquire(store.path)
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
                self.store.conn.execute("UPDATE episode SET outcome='unknown',failure='Restart interrupted execution; no automatic replay',finished_at=? WHERE id=?",(self.store.clock(),r['id']))
                self.journal(r['id'],'recovery',{},'unknown','requires operator reconciliation')
            if running: self._stopped.set()
            self.store.conn.execute("UPDATE episode SET outcome='expired' WHERE outcome='proposed' AND id IN (SELECT id FROM cx_episode WHERE expires_at<?)",(self.store.clock(),))
            self.store.conn.execute("UPDATE cx_wake SET status='pending' WHERE status='preparing'")
            # Existing task links remain durable; recovery never dispatches physical effects.
            for r in self.store.rows("SELECT id,outcome FROM episode WHERE outcome IN ('unknown','success','failed','aborted','cancelled','rejected')"):
                self._finish_task(r['id'],r['outcome'])

    def _advisor(self,stage,state,questions,allowed=False):
        answer=self.intent.evaluate(stage,state,questions,disclosure_allowed=bool(allowed and self.store.policy.cloud_enabled))
        self.store.decision(stage,answer.get('provider','local'),answer.get('model'),answer['status']=='ok',answer['status'])
        return answer

    def command(self,request: str,*,principal: str,idempotency_key: str | None=None,cloud_allowed: bool=False) -> dict:
        request=text(request,'request',2000); principal=text(principal,'principal',100)
        with self.activity.foreground():
            low=normalize(request)
            if low in {'stop','stop now','halt','emergency stop'}: return {'kind':'stop',**self.stop()}
            # Cheap deterministic controls/recall precede optional cloud classification.
            if RECALL.search(request):
                return self.store.recall(request)
            if low.startswith('remember '):
                body=re.sub(r'^remember\s+(?:that\s+)?','',request,flags=re.I)
                return self.store.teach('Instruction from '+principal,body,owner=principal,kind='spoken',cloud_ok=cloud_allowed)
            match=WHEN.match(request)
            if match and not request.rstrip().endswith('?'):
                return self.schedule(match[1],match[2],principal=principal)
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
                extra_refs: list[str] | None=None,cloud_allowed: bool=False) -> dict:
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
                        return self.operation(prior['id'])
                    ep=prior['id']; self._recoverable_preparing.remove(ep)
            t=self.store.clock(); basis=self.store.basis(goal)
            if ep is None:
                ep=uid('ep')
                self.store.conn.execute('INSERT INTO episode(id,goal,request,outcome,started_at) VALUES(?,?,?,?,?)',
                                        (ep,canonical(goal.to_dict()),request,'preparing',t))
                self.store.conn.execute('INSERT INTO cx_episode VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (ep,self.mode,canonical(goal.to_dict()),basis,self.store.policy.fingerprint(),canonical(self.compiler.fixture()),key,ih,t+self.store.policy.approval_ttl_s,principal))
            else:
                self.store.conn.execute('UPDATE cx_episode SET basis_hash=?,initial_json=?,expires_at=? WHERE id=?',
                    (basis,canonical(self.compiler.fixture()),t+self.store.policy.approval_ttl_s,ep))
        with self.activity.foreground():
            try:
                instruction=self.store.teach('Task instruction',request,owner=principal,kind='request',rules=[],cloud_ok=cloud_allowed)
                refs=['source:'+instruction['source_id']]+(extra_refs or [])
                bundle=self.store.bundle(request,goal)
                fixture=self.compiler.fixture()
                # Optional relevance advice ranks only explicitly cloud-permitted evidence.
                shareable=[e for e in bundle['evidence'] if e.get('cloud_ok')]
                if shareable:
                    self._advisor('relevance',{'request':request,'evidence':[{'id':e.get('id',e.get('chunk_id')),'text':e['text']} for e in shareable[:5]]},
                                  {'sufficient':'Does the supplied evidence directly support this request without missing a material condition?'},cloud_allowed)
                skill=self.skills.find(goal)
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
                        try:
                            calls+=1; out=self.qwen.complete('plan',bundle,goal=goal.to_dict())
                            v=verify(goal,out.value['steps'],fixture,self.store.related_rules(goal),self.store.policy)
                            self.store.decision('plan','llama.cpp',out.model,v.ok,'verified' if v.ok else 'goal verification rejected model plan')
                            if v.ok:
                                base=self.compiler.compile(goal,fixture)
                                plan={**base,'steps':out.value['steps'],'refs':sorted(set(base['refs']+out.refs)),'verification':v.to_dict()}; source='model'
                                self.store.manifest('last_qwen_request','parametric',{'contract_sha256':out.contract_hash,'prompt_sha256':out.prompt_hash,
                                    'input_tokens':out.input_tokens,'model':out.model,'elapsed_ms':out.elapsed_ms,'response_accepted':True})
                            else: note='Model plan failed independent goal verification; composed from accepted rules.'
                        except CortexError as e:
                            note='Local model unavailable or invalid: '+type(e).__name__
                    if plan is None:
                        plan=self.compiler.compile(goal,fixture); source='fallback' if calls else 'composed'
                    plan['refs']=sorted(set(plan['refs']+refs))
                    d=self.store.derive('task_recipe',{'task':goal.to_dict(),'steps':plan['steps'],'mode':'SIMULATED_VERIFICATION'},plan['refs'],
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
                return self.operation(ep)
            except Exception as e:
                with self.store.tx(): self.store.conn.execute("UPDATE episode SET outcome='rejected',failure=?,finished_at=? WHERE id=? AND outcome='preparing'",(str(e)[:500] if isinstance(e,CortexError) else type(e).__name__,self.store.clock(),ep))
                if isinstance(e,CortexError): raise
                raise CortexError('planning failed: '+type(e).__name__) from None

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
            for r in self.store.rows("SELECT id,outcome FROM episode WHERE outcome IN ('expired','invalidated')"):self._finish_task(r['id'],r['outcome'])
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
                    if self._stopped.is_set() or self.robot.state().get('stopped'): raise Denied('stop is latched')
                    if self.store.clock()>=r['expires_at']: raise Stale('proposal expired')
                    actual=digest({k:v for k,v in plan.items() if k!='verification'})
                    if supplied_digest!=actual or supplied_digest!=r['digest']: raise Conflict('approval does not match this exact plan')
                    goal=Goal.from_dict(plan['goal'])
                    if r['derived_memory_id']:
                        recipe=self.store.derived(r['derived_memory_id'])
                        if recipe['status']!='VERIFIED' or recipe['payload'].get('steps')!=plan['steps'] or not self.store.refs_valid(recipe['refs']):
                            raise Stale('execution recipe changed or lost its evidence')
                    if plan['basis']!=self.store.basis(goal) or plan['policy_hash']!=self.store.policy.fingerprint() or not self.store.refs_valid(plan['refs']):
                        raise Stale('rule, binding, source or policy changed after proposal')
                    if self.store.prohibitions(set(goal.targets)|{r['object'] for r in self.store.related_rules(goal)}) and goal.kind=='fetch': raise Denied('current prohibition blocks this operation')
                    v=verify(goal,plan['steps'],self.compiler.fixture(),self.store.related_rules(goal),self.store.policy)
                    if not v.ok: raise Denied('; '.join(v.errors))
                    claimed=self.store.conn.execute("UPDATE episode SET outcome='running' WHERE id=? AND outcome='proposed'",(ep,)).rowcount
                    if claimed!=1: raise Conflict('operation already claimed')
                    now=self.store.clock()
                    self.store.conn.execute('INSERT INTO approval VALUES(?,?,?,?,?,?,?,?)',(uid('apv'),'plan',ep,actual,principal,now,r['expires_at'],now))
                return self._execute(ep,goal,plan)
        finally: self._effect_lock.release()

    def _execute(self,ep: str,goal: Goal,plan: dict) -> dict:
        steps=[]; outcome='success'; reason=None; seen=set(); tags={o['tag']:o['id'] for o in self.store.objects()}
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
        if outcome in {'unknown','aborted','failed'}:
            self._stopped.set()
            try: self.robot.stop()
            except Exception: reason=(reason or '')+'; stop not confirmed'
        with self.store.tx():
            self.store.conn.execute('UPDATE episode SET outcome=?,failure=?,finished_at=? WHERE id=?',(outcome,reason,self.store.clock(),ep))
            self._finish_task(ep,outcome)
        return {**self.operation(ep),'steps':steps}

    def verify_outcome(self,ep: str,*,principal: str,ok: bool,note: str):
        text(note,'verification evidence',1000)
        r=self.operation(ep)
        if r['owner']!=principal: raise Denied('verification principal does not own the operation')
        if r['status'] not in {'needs_verification','unknown'}: raise Conflict('no unresolved effect to verify')
        with self.store.tx():
            outcome='success' if ok else 'failed'
            self.store.conn.execute('UPDATE episode SET outcome=?,failure=?,finished_at=? WHERE id=?',(outcome,'operator observation: '+note,self.store.clock(),ep))
            self.journal(ep,'verify',{},outcome,principal+': '+note)
            self._finish_task(ep,outcome)
        return self.operation(ep)

    def reject(self,ep: str,*,principal: str):
        with self.store.tx():
            r=self.operation(ep)
            if r['owner']!=principal: raise Denied('not the operation owner')
            if r['status'] not in {'preparing','proposed'}: raise Conflict('cannot reject an executing or completed operation')
            self.store.conn.execute("UPDATE episode SET outcome='cancelled',finished_at=? WHERE id=?",(self.store.clock(),ep))
            self._finish_task(ep,'cancelled')
        return self.operation(ep)

    def stop(self):
        self._stopped.set(); self.activity.touch(); confirmed=False
        try:
            self.robot.stop(); confirmed=bool(self.robot.state().get('stopped'))
        except Exception: pass
        with self.store.tx():
            self.store.conn.execute("UPDATE episode SET outcome='cancelled',failure='stop revoked pending proposal' WHERE outcome IN ('preparing','proposed') AND id IN (SELECT id FROM cx_episode)")
            self.journal(None,'stop',{},'confirmed' if confirmed else 'requested')
        return {'stop_requested':True,'stop_confirmed':confirmed,'auto_resume':False}

    def clear_stop(self,*,principal: str):
        text(principal,'operator',100)
        if self.store.one("SELECT 1 FROM episode WHERE outcome IN ('running','unknown','needs_verification')"): raise Denied('reconcile running/unknown operations before rearming')
        self.robot.reset_stop(); self._stopped.clear(); self.activity.touch()
        return self.robot.state()

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
        now=self.store.clock()
        with self.store.tx():
            last=self.store.one("SELECT value FROM meta WHERE key='cortex_last_utc'")
            if last and now<float(last['value'])-1: raise Stale('UTC clock moved backward; operator must re-establish trusted time')
            self.store.conn.execute("INSERT INTO meta VALUES('cortex_last_utc',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
        due=self.store.rows("SELECT p.id,p.trigger,c.owner,c.generation,d.due_at FROM prospective_task p JOIN cx_commitment c ON c.id=p.id JOIN cx_due d ON d.task_id=p.id WHERE p.status='waiting' AND d.due_at<=? ORDER BY d.due_at LIMIT 8",(now,))
        results=[]
        for task in due:
            results+=self.trigger(task['trigger'],event_id='due:'+task['id']+':'+str(task['generation']),principal=task['owner'])
        return results

    def cancel_task(self,tid: str,*,principal: str) -> dict:
        with self.store.tx():
            r=self.store.one('SELECT p.*,c.owner FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.id=?',(tid,))
            if not r: raise Missing('task not found')
            if r['owner']!=principal: raise Denied('not the task owner')
            if r['status']=='done': raise Conflict('cannot cancel an already completed task')
            if r['proposal_id']:
                ep=self.operation(r['proposal_id'])
                if ep['status'] in {'running','unknown','needs_verification'}:
                    raise Conflict('effect may have started; stop/reconcile it, do not claim cancellation')
                self.store.conn.execute("UPDATE episode SET outcome='cancelled',failure='commitment cancelled' WHERE id=? AND outcome IN ('proposed','preparing')",(r['proposal_id'],))
            self.store.conn.execute("UPDATE prospective_task SET status='cancelled' WHERE id=?",(tid,))
            self.store.conn.execute('UPDATE cx_commitment SET generation=generation+1 WHERE id=?',(tid,))
            self.store.conn.execute("UPDATE cx_wake SET status='cancelled' WHERE task_id=? AND status IN ('pending','preparing')",(tid,))
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
            with self.store.tx():
                claim=self.store.conn.execute("UPDATE cx_wake SET status='preparing' WHERE event_id=? AND task_id=? AND generation=? AND status='pending'",(w['event_id'],task['id'],w['generation'])).rowcount
            if not claim: continue
            proposal=None
            try:
                goal=parse_goal(task['request'],self.store)
                proposal=self.propose(goal,task['request'],principal=task['owner'],
                    idempotency_key=f"wake:{task['id']}:{w['generation']}",extra_refs=['source:'+task['source_id']])
                with self.store.tx():
                    current=self.store.one('SELECT p.status,c.generation FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.id=?',(task['id'],))
                    if current['status']!='fired' or current['generation']!=w['generation']:
                        if proposal['status'] in {'preparing','proposed'}: self.reject(proposal['operation_id'],principal=task['owner'])
                        self.store.conn.execute("UPDATE cx_wake SET status='cancelled' WHERE event_id=? AND task_id=? AND generation=?",(w['event_id'],task['id'],w['generation']))
                        continue
                    self.store.conn.execute('UPDATE prospective_task SET proposal_id=? WHERE id=?',(proposal['operation_id'],task['id']))
                    self.store.conn.execute("UPDATE cx_wake SET status='linked',proposal_id=? WHERE event_id=? AND task_id=? AND generation=?",(proposal['operation_id'],w['event_id'],task['id'],w['generation']))
                out.append({'task_id':task['id'],'proposal':proposal})
            except CortexError as e:
                with self.store.tx():
                    self.store.conn.execute("UPDATE cx_wake SET status='failed',error=? WHERE event_id=? AND task_id=? AND generation=?",(str(e),w['event_id'],task['id'],w['generation']))
                    self.store.conn.execute("UPDATE prospective_task SET status='failed' WHERE id=? AND status='fired'",(task['id'],))
                out.append({'task_id':task['id'],'error':str(e)})
        return out

    def _finish_task(self,ep: str,outcome: str):
        state={'success':'done','failed':'failed','rejected':'failed','unknown':'unknown','aborted':'unknown','cancelled':'cancelled','expired':'expired','invalidated':'failed'}.get(outcome)
        if state:
            self.store.conn.execute('UPDATE prospective_task SET status=? WHERE proposal_id=? AND status NOT IN (\'cancelled\',\'done\')',(state,ep))

    def metrics(self):
        return {'episodes':self.store.rows('SELECT id,outcome,planner_calls,skill_id FROM episode ORDER BY started_at'),
                'derived':self.store.rows('SELECT kind,status,COUNT(*) n FROM cx_derived GROUP BY kind,status'),
                'skills':self.store.rows('SELECT status,COUNT(*) n FROM skill_version GROUP BY status'),
                'practice':self.store.rows('SELECT status,COUNT(*) n FROM cx_practice GROUP BY status'),
                'mode':self.mode,'weights_updated':False,'epoch':self.store.epoch(),
                'models':self.store.rows('SELECT * FROM cx_manifest')}
