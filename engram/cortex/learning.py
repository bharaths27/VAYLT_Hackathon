"""Self-authored procedures and idle consolidation, with independent verification.

This is memory/skill improvement, not reinforcement-learning weight updates.
"""
from __future__ import annotations

import collections
import copy
import json
import threading
import time
from contextlib import contextmanager
from typing import Callable

from .models import IntentAdvisor, ModelUnavailable, QwenClient
from .planning import PHYSICAL, Compiler, verify
from .store import Store
from .types import Conflict, CortexError, Denied, Goal, Missing, Stale, canonical, digest, text, uid


class Skills:
    def __init__(self, store: Store): self.store=store

    def get(self, sid: str) -> dict:
        r=self.store.one('SELECT v.*,c.goal_key,c.basis_hash,c.policy_hash,c.validation_json,c.mode FROM skill_version v JOIN cx_skill c ON c.id=v.id WHERE v.id=?',(sid,))
        if not r: raise Missing('skill not found in the Cortex registry')
        r['definition']=json.loads(r.pop('definition_json'))
        r['validation']=json.loads(r.pop('validation_json') or 'null')
        return r

    def list(self) -> list[dict]:
        self.store.invalidate()
        return [self.get(r['id']) for r in self.store.rows('SELECT id FROM cx_skill ORDER BY id')]

    def create(self, plan: dict, *, episode_id: str | None=None, mode: str='SIMULATED') -> dict:
        goal=Goal.from_dict(plan['goal'])
        if plan['basis']!=self.store.basis(goal): raise Stale('source changed while composing skill')
        refs=plan.get('refs',[])
        if not refs or not self.store.refs_valid(refs): raise Missing('skill needs valid provenance')
        definition={k:plan[k] for k in ('goal','steps','rules_used','evidence','refs','basis','policy_hash')}
        definition['refs']=self.identity_refs(goal,refs)
        definition['effect_contract']='Only the exact typed goal; physical execution always needs current approval.'
        d=digest(definition)
        with self.store.tx():
            old=self.store.one("SELECT id FROM skill_version WHERE digest=? AND status!='REVOKED'",(d,))
            if old: return self.get(old['id'])
            sid=uid('skl'); name=f"{goal.kind}:{','.join(goal.targets) or goal.primitive or ''}:{goal.zone or ''}"
            version=1+self.store.one('SELECT COUNT(*) n FROM skill_version WHERE name=?',(name,))['n']
            self.store.conn.execute('INSERT INTO skill_version(id,name,version,status,definition_json,digest,derived_from,created_at) VALUES(?,?,?,?,?,?,?,?)',
                                    (sid,name,version,'CANDIDATE',canonical(definition),d,episode_id,self.store.clock()))
            self.store.conn.execute('INSERT INTO cx_skill VALUES(?,?,?,?,?,?)',(sid,goal.key(),plan['basis'],plan['policy_hash'],None,mode))
        return self.get(sid)

    def test(self, sid: str, fixture: dict | None=None) -> dict:
        s=self.get(sid); d=s['definition']; g=Goal.from_dict(d['goal'])
        if s['status'] not in {'CANDIDATE','TESTED'}: raise Conflict('only a candidate/tested skill may be validated')
        if digest(d)!=s['digest'] or not self.store.refs_valid(d['refs']): raise Stale('skill or its evidence changed')
        compiler=Compiler(self.store)
        f=copy.deepcopy(fixture or compiler.fixture(symbolic=True))
        # Validate on multiple controlled arrangements, not merely its authored example.
        variants=[f]
        zones=sorted(self.store.zones())
        # Each variant starts every object the plan handles away from where the plan puts it: a state that needs
        # the procedure, never one that already satisfies it (where handling it again is a no-progress effect).
        moves,cur={},None
        for st in d['steps']:
            if st['op']=='pick_tagged_object': cur=st['args']['object_id']
            elif st['op']=='place_at_zone' and cur: moves.setdefault(cur,set()).add(st['args']['zone']); cur=None
        for shift in (0,1):
            variant=copy.deepcopy(f)
            for i,oid in enumerate(sorted(variant['objects'])):
                ring=[zones[(i+shift+k)%len(zones)] for k in range(len(zones))]
                variant['positions'][oid]=next((z for z in ring if z not in moves.get(oid,())),ring[0])
            # The robot starts somewhere else too, away from the goal's station: a goto must actually travel. Each
            # hypothetical arrangement starts with an empty claw (the skill's precondition; live use re-verifies).
            variant.update(holding=None,holding_known=True)
            variant['at_zone']=next((z for z in zones[shift:]+zones[:shift] if z!=g.zone),None)
            variants.append(variant)
        results=[]
        for v in variants:
            results.append({**verify(g,d['steps'],v,self.store.related_rules(g),self.store.policy,
                                     expected_basis=d['basis'],actual_basis=self.store.basis(g)).to_dict(),'start_zone':v.get('at_zone')})
        # No effect step is no competence for a fetch or goto: such a procedure only "passes" where the goal already
        # held (e.g. composed while the target sat at its destination), so it never becomes a skill.
        if g.kind in {'fetch','goto'} and not any(st['op'] in PHYSICAL for st in d['steps']):
            results.append({'ok':False,'errors':['no effect step: a no-op procedure passes only where the goal already holds'],'start_zone':None})
        passed=all(r['ok'] for r in results)
        record={'ok':passed,'mode':'SIMULATED','verifier':'symbolic-verifier-v1','cases':results,
                'assumptions':'Registered primitives succeed in the symbolic environment; this is not physical competence evidence.',
                'definition_digest':s['digest'],'basis':self.store.basis(g)}
        with self.store.tx():
            self.store.conn.execute('UPDATE cx_skill SET validation_json=? WHERE id=?',(canonical(record),sid))
            self.store.conn.execute('UPDATE skill_version SET status=? WHERE id=?',('TESTED' if passed else 'CANDIDATE',sid))
        return record

    def approve(self,sid: str, supplied_digest: str, *, principal: str, automatic: bool=False) -> dict:
        text(principal,'approver',100)
        if automatic and (not self.store.policy.auto_publish_verified or principal!='policy:'+self.store.policy.fingerprint()):
            raise Denied('automatic skill publication is not enabled by trusted startup policy')
        with self.store.tx():
            s=self.get(sid); d=s['definition']; g=Goal.from_dict(d['goal'])
            if s['status']!='TESTED' or not s['validation'] or not s['validation']['ok']:
                raise Denied('independent verification must pass before publication')
            if supplied_digest!=s['digest'] or digest(d)!=s['digest']: raise Conflict('skill digest changed')
            if self.store.basis(g)!=d['basis'] or d['policy_hash']!=self.store.policy.fingerprint() or not self.store.refs_valid(d['refs']):
                raise Stale('skill dependencies changed')
            self.store.conn.execute("UPDATE skill_version SET status='APPROVED',approved_at=?,approved_by=? WHERE id=?",(self.store.clock(),principal,sid))
        return self.get(sid)

    def revoke(self,sid: str):
        self.get(sid)
        with self.store.tx(): self.store.conn.execute("UPDATE skill_version SET status='REVOKED' WHERE id=?",(sid,))

    def identity_refs(self, goal: Goal, refs: list[str]) -> list[str]:
        """The refs that identify a procedure: the sources and rules it rests on, which revocation follows. What only
        entered the prompt changes with every request: episodes are dropped, and a derived memory (often this very
        system's last recipe) is replaced by the sources and rules it cites, so revoking those still reaches the
        procedure. A goal with no other provenance keeps its anchor, the first request that asked for it."""
        core,todo,seen=set(),list(refs),set()
        while todo:
            r=todo.pop()
            if r in seen or len(seen)>self.store.policy.max_nodes: continue
            seen.add(r)
            if r.startswith('derived:'):
                row=self.store.one('SELECT refs_json FROM cx_derived WHERE id=?',(r[8:],))
                todo+=json.loads(row['refs_json']) if row else [r]
            elif not r.startswith('episode:'): core.add(r)
        if core: return sorted(core)
        anchor=self.store.one('SELECT value FROM meta WHERE key=?',('cx_goal_anchor:'+goal.key(),))
        return ['episode:'+anchor['value']] if anchor and 'episode:'+anchor['value'] in refs else sorted(set(refs))

    def provenance(self, goal: Goal, episode_id: str, extra_refs: list[str] | None=None) -> list[str]:
        """Refs a command adds to its plan. Commands are episodes, not sources, so a recipe or
        skill is keyed on goal + basis (rule/source refs), never on the request. A goal with no
        rule or source cites the FIRST episode that asked for it, so repeats share one recipe."""
        extra=list(extra_refs or [])
        if extra or self.store.related_rules(goal): return extra
        key='cx_goal_anchor:'+goal.key()    # primary-key lookup, not a scan of past episodes
        with self.store.tx():
            self.store.conn.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',(key,episode_id))
            return ['episode:'+self.store.one('SELECT value FROM meta WHERE key=?',(key,))['value']]

    def find(self,goal: Goal) -> dict | None:
        # Hot path: an indexed lookup by goal, each candidate validated below; no vault rescan.
        for r in self.store.rows("SELECT c.id FROM cx_skill c JOIN skill_version s ON s.id=c.id WHERE c.goal_key=? AND s.status='APPROVED' ORDER BY s.approved_at DESC",(goal.key(),)):
            s=self.get(r['id']); d=s['definition']
            if (digest(d)==s['digest'] and d['basis']==self.store.basis(goal) and d['policy_hash']==self.store.policy.fingerprint()
                    and s['validation'] and s['validation']['definition_digest']==s['digest'] and self.store.refs_valid(d['refs'])):
                return s
        return None


class Activity:
    """Process-local interactive activity; no inference is allowed to own the stop path."""
    def __init__(self, monotonic: Callable=time.monotonic):
        self.clock=monotonic; self.lock=threading.Lock(); self.busy=0; self.generation=0; self.last=self.clock()

    def touch(self):
        with self.lock: self.generation+=1; self.last=self.clock()

    @contextmanager
    def foreground(self):
        with self.lock: self.busy+=1; self.generation+=1; self.last=self.clock()
        try: yield
        finally:
            with self.lock: self.busy-=1; self.last=self.clock()

    def snapshot(self):
        with self.lock: return self.busy,self.generation,self.clock()-self.last


class Dreamer:
    LIVE_CAP=128                 # per-kind cap on LIVE rows (stale/deleted rows do not count)
    PRACTICE_WINDOW_S=86400.0    # practice cap is rolling: cases created in the last day
    BACKOFF_S=(30,60,120,300,600,1800,3600)
    MODEL_ATTEMPTS=5             # failed model attempts on one input before it is explicitly abandoned

    def __init__(self, store: Store, skills: Skills, activity: Activity,
                 qwen: QwenClient | None=None, intent: IntentAdvisor | None=None, *,
                 model_window_s: float=600, model_calls_per_window: int | None=None):
        self.store,self.skills,self.activity=store,skills,activity
        self.qwen,self.intent=qwen,intent
        self._stop=threading.Event(); self._thread=None; self._cycle_lock=threading.Lock()
        # Real model-call cap: at most N idle model calls per rolling window (default N =
        # policy.max_model_calls_per_dream), plus no repeat of an input already answered.
        self.model_window_s=model_window_s; self.model_calls_per_window=model_calls_per_window

    def _model_state(self) -> dict:
        """Vault-persisted model bookkeeping, so a restart neither resets the call cap nor forgets pending input:
        call stamps (store clock) in the rolling window, the last input answered, and the input still pending."""
        row=self.store.one("SELECT value FROM meta WHERE key='cx_dream_model'")
        return json.loads(row['value']) if row else {'calls':[],'answered':None,'input':None,'attempts':0}

    def _save_model_state(self, st: dict):
        with self.store.tx():
            self.store.conn.execute("INSERT INTO meta VALUES('cx_dream_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(canonical(st),))

    def start(self, interval_s: float=5):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        def worker():
            while not self._stop.wait(interval_s):
                try: self.cycle()
                except Exception as e:
                    self.store.decision('dream_worker','local',None,False,type(e).__name__)
        self._thread=threading.Thread(target=worker,name='engram-idle-dreamer',daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=1)
        return not (self._thread and self._thread.is_alive())

    def cycle(self, *, force: bool=False) -> dict:
        if not self._cycle_lock.acquire(blocking=False): return {'status':'busy'}
        try: return self._cycle(force=force)
        finally: self._cycle_lock.release()

    def _model_cap(self) -> int:
        return self.store.policy.max_model_calls_per_dream if self.model_calls_per_window is None else self.model_calls_per_window

    def _model_budget(self) -> float | None:
        """None while an idle model call is allowed now; else when the oldest call in the rolling window leaves it."""
        st=self._model_state(); now=self.store.clock()
        calls=[t for t in st['calls'] if now-t<self.model_window_s]    # a stamp ahead of a stepped-back clock still counts
        if calls!=st['calls']: st['calls']=calls; self._save_model_state(st)
        return None if len(calls)<self._model_cap() else min(calls,default=now)+self.model_window_s

    def _backoff(self, failed: bool):
        with self.store.tx():
            if not failed:
                self.store.conn.execute("DELETE FROM meta WHERE key='cx_dream_backoff'"); return
            row=self.store.one("SELECT value FROM meta WHERE key='cx_dream_backoff'")
            n=(json.loads(row['value'])['failures'] if row else 0)+1
            value=canonical({'failures':n,'until':self.store.clock()+self.BACKOFF_S[min(n,len(self.BACKOFF_S))-1]})
            self.store.conn.execute("INSERT INTO meta VALUES('cx_dream_backoff',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,))

    def _cycle(self, *, force: bool=False) -> dict:
        busy,generation,idle=self.activity.snapshot()
        if busy or (not force and idle<self.store.policy.idle_after_s): return {'status':'not_idle'}
        # Only Cortex episodes count: a legacy-engine 'running' row is not this robot's activity.
        if self.store.one("SELECT 1 FROM episode e JOIN cx_episode c ON c.id=e.id WHERE e.outcome='running'"): return {'status':'robot_busy'}
        back=self.store.one("SELECT value FROM meta WHERE key='cx_dream_backoff'")
        if not force and back and self.store.clock()<json.loads(back['value'])['until']: return {'status':'backoff'}
        watermark=digest({'epoch':self.store.epoch(),'episodes':self.store.rows("SELECT e.id,e.outcome FROM episode e JOIN cx_episode c ON c.id=e.id ORDER BY e.started_at DESC LIMIT 16"),
                          'commitments':self.store.rows('SELECT id,status FROM prospective_task ORDER BY created_at DESC LIMIT 16')})
        last=self.store.one("SELECT value FROM meta WHERE key='cx_dream_watermark'")
        backlog=self.store.one("SELECT 1 FROM episode e JOIN cx_episode c ON c.id=e.id WHERE e.outcome IN ('success','failed','unknown','aborted') AND NOT EXISTS (SELECT 1 FROM cx_consolidated k WHERE k.episode_id=e.id AND k.outcome=e.outcome) LIMIT 1")
        # Practice deferred by its rolling cap, or a model input deferred by the call budget, may resume now.
        due=any(self.store.clock()>=float(w['value']) for w in self.store.rows("SELECT value FROM meta WHERE key IN ('cx_dream_practice_wait','cx_dream_model_wait')"))
        if not force and not backlog and last and last['value']==watermark and not due: return {'status':'no_new_evidence'}
        started=time.monotonic(); run=uid('dream'); made=[]; practiced=[]; errors=[]; leftover=False; model_pending=model_failed=False; practice_wait=model_wait=None
        with self.store.tx(): self.store.conn.execute('INSERT INTO cx_dream VALUES(?,?,?,?,?)',(run,self.store.clock(),None,'running','{}'))
        def interrupted():
            b,g,_=self.activity.snapshot()
            return self._stop.is_set() or b!=0 or g!=generation
        def still_idle():
            # Starting NEW work also needs the time budget; a result already computed is kept
            # unless foreground activity interrupted it.
            return not interrupted() and time.monotonic()-started<self.store.policy.dream_budget_s
        try:
            self.store.invalidate()
            # Bounded incremental summaries/lessons retain episode provenance and evidence class.
            episodes=self.store.rows("SELECT e.*,c.mode,c.goal_json FROM episode e JOIN cx_episode c ON c.id=e.id WHERE e.outcome IN ('success','failed','unknown','aborted') AND NOT EXISTS (SELECT 1 FROM cx_consolidated k WHERE k.episode_id=e.id AND k.outcome=e.outcome) ORDER BY e.started_at,e.id LIMIT ?",(self.store.policy.dream_batch,))
            for ep in episodes:
                if not still_idle(): leftover=True; break
                kind='summary' if ep['outcome']=='success' else 'lesson'
                payload={'episode_id':ep['id'],'request':ep['request'],'outcome':ep['outcome'],
                         'evidence_class':ep['mode'],'failure':ep['failure'],
                         'note':'Recorded outcome; do not infer a new physical capability.'}
                m=self.store.derive(kind,payload,['episode:'+ep['id']],origin='idle_consolidation',
                                    verified=kind=='summary',verifier='episodic-summary-v1' if kind=='summary' else None)
                made.append(m['id'])
                with self.store.tx():self.store.conn.execute('INSERT INTO cx_consolidated VALUES(?,?) ON CONFLICT(episode_id) DO UPDATE SET outcome=excluded.outcome',(ep['id'],ep['outcome']))
            # Task proposals are generated from current memory, not a static answer table.
            compiler=Compiler(self.store)
            rules_now=self.store.rules()
            refs_now=sorted(set('source:'+r['source_id'] for r in rules_now))
            if self.intent and self.store.policy.cloud_enabled and self.store.cloud_eligible(refs_now) and still_idle():
                advice=self.intent.evaluate('dream_priority',{'rules':[{'subject':r['subject'],'prerequisite':r['object'],'station':r['required_zone']} for r in rules_now[:8]]},
                    {'practice':'Would practicing a new combination of these supported prerequisite rules be useful?','conflict':'Do these constraints appear contradictory?'},disclosure_allowed=True)
                self.store.decision('dream_priority',advice.get('provider','local'),advice.get('model'),advice['status']=='ok',advice['status'])
                if advice['status']=='ok' and (advice['scores']['conflict']>=0.8 or advice['scores']['practice']<0.2):
                    errors.append('Advisory practice veto; no permission or factual conclusion inferred.')
                    rules_now=[]
            zones=sorted(self.store.zones())
            proposed=[]
            for r in rules_now:
                if r['status']=='active' and zones:
                    dest=next((z for z in zones if z!=r['required_zone']),zones[0])
                    proposed.append((Goal('fetch',(r['subject'],),dest),['source:'+r['source_id'],'rule:'+r['id']]))
            for ep in episodes:
                g=Goal.from_dict(json.loads(ep['goal_json']))
                proposed.append((g,['episode:'+ep['id']]))
            seen=set()
            for goal,refs in proposed:
                if goal.key() in seen: continue
                seen.add(goal.key())
                case_id=digest({'goal':goal.to_dict(),'basis':self.store.basis(goal),'verifier':'symbolic-verifier-v1'})
                if self.store.one('SELECT 1 FROM cx_practice WHERE id=?',(case_id,)): continue
                # Unpractised goals left for a later cycle are a backlog: the watermark must not hide them.
                if not still_idle() or len(practiced)>=self.store.policy.dream_batch: leftover=True; break
                window=self.store.clock()-self.PRACTICE_WINDOW_S
                if self.store.one('SELECT COUNT(*) n FROM cx_practice WHERE created_at>?',(window,))['n']>=self.LIVE_CAP:
                    # Goals are left but the rolling cap is full: wait, persisted, for its earliest case to leave the
                    # window, instead of re-running (and logging) a cycle every tick until then.
                    practice_wait=self.store.one('SELECT MIN(created_at) t FROM cx_practice WHERE created_at>?',(window,))['t']+self.PRACTICE_WINDOW_S; break
                fixture=compiler.fixture(symbolic=True)
                try:
                    plan=compiler.compile(goal,fixture); plan['refs']=sorted(set(plan['refs']+refs))
                    closure=self.store.related_rules(goal)
                    if closure:
                        relationship=self.store.derive('relationship',{'relation':'requires_preparation_path','task':goal.to_dict(),
                            'path':[{'subject':r['subject'],'prerequisite':r['object'],'station':r['required_zone']} for r in closure]},
                            plan['refs'],origin='dependency_graph_consolidation',verified=True,verifier='dependency-closure-v1',basis=plan['basis'])
                        made.append(relationship['id'])
                    expected={'goal':goal.to_dict(),'mode':'SIMULATED'}
                    result=verify(goal,plan['steps'],fixture,self.store.related_rules(goal),self.store.policy)
                    # Probe the oracle with a plausible wrong answer as well as a valid one.
                    wrong=[{'op':'observe','args':{}}]
                    invalid=verify(goal,wrong,fixture,self.store.related_rules(goal),self.store.policy)
                    meaningful=(not invalid.ok) or all(fixture['positions'].get(o)==goal.zone for o in goal.targets)
                    ok=result.ok and meaningful
                    with self.store.tx():
                        self.store.conn.execute('INSERT OR IGNORE INTO cx_practice VALUES(?,?,?,?,?,?,?,?)',
                            (case_id,canonical({'goal':goal.to_dict(),'fixture':fixture}),canonical(expected),canonical(result.to_dict()),
                             'PASS' if ok else 'FAIL','symbolic-verifier-v1','SIMULATED',self.store.clock()))
                    practiced.append(case_id)
                    if ok and still_idle():
                        derived=self.store.derive('practice_goal',{'goal':goal.to_dict(),'case_id':case_id,'mode':'SIMULATED'},refs,
                                  origin='self_proposed_practice',verified=True,verifier='symbolic-verifier-v1',basis=plan['basis'])
                        made.append(derived['id'])
                        skill=self.skills.create(plan,mode='SIMULATED')
                        test_result=skill['validation'] if skill['status']=='APPROVED' else self.skills.test(skill['id'],fixture)
                        if test_result['ok'] and skill['status']!='APPROVED' and self.store.policy.auto_publish_verified and still_idle():
                            self.skills.approve(skill['id'],skill['digest'],principal='policy:'+self.store.policy.fingerprint(),automatic=True)
                except CortexError as e: errors.append(type(e).__name__+': '+str(e))
            # Local-model associations are hypotheses only, never automatically factual. The input is built and compared
            # first: one already answered (or explicitly abandoned), or none worth asking, is no pending work and lets the
            # watermark advance. A new input stays pending until the model answered it: past the time budget it keeps the
            # watermark open; past the call budget it waits, persisted, until the oldest call leaves the window (idle ticks
            # do not re-run the cycle meanwhile); a failed attempt keeps it open and backs off. A cap of zero never asks.
            if self.qwen and self._model_cap()>0:
                evidence=[]; refs=[]
                for r in self.store.rules()[:4]:
                    if r['status']=='active':
                        evidence.append({'rule_id':r['id'],'subject':r['subject'],'prerequisite':r['object'],'station':r['required_zone']})
                        refs+=['rule:'+r['id'],'source:'+r['source_id']]
                # General knowledge/notes and episodes also participate: dreaming is
                # not limited to the presence of a robotics prerequisite rule.
                declared=self.store.generic_facts()[:4]
                for f in declared:refs+=['source:'+f['source_id']]
                recent_sources=self.store.rows("SELECT c.id,c.source_id,c.text FROM source_chunk c JOIN cx_source s ON s.source_id=c.source_id JOIN source v ON v.id=c.source_id WHERE s.status='accepted' AND v.kind!='future' ORDER BY v.created_at DESC LIMIT 3")
                for c in recent_sources:refs+=['source:'+c['source_id']]
                refs+=['episode:'+e['id'] for e in episodes[:2]]
                live=self.store.one("SELECT COUNT(*) n FROM cx_derived WHERE kind='hypothesis' AND status IN ('CANDIDATE','VERIFIED')")['n']
                # The bundle is intentionally small; no whole-vault prompt.
                # Only citable memory enters the prompt: every item carries a ref that is in allowed_refs. Observations,
                # pending commitments and approved-skill rows have no resolvable ref, so a hypothesis could not cite them.
                bundle={'rules':evidence,'allowed_refs':sorted(set(refs)),'episodes':[{'id':e['id'],'outcome':e['outcome'],'mode':e['mode']} for e in episodes[:2]],
                        'declared_knowledge':declared,'source_passages':recent_sources}
                asked=digest(bundle)
                if not (refs and live<self.LIVE_CAP and asked!=self._model_state()['answered']): pass     # nothing new to ask
                elif not still_idle(): model_pending=True
                elif (resume:=self._model_budget()) is not None: model_pending=True; model_wait=resume
                else:
                    st=self._model_state(); q=None; st['calls'].append(self.store.clock()); self._save_model_state(st)
                    try: q=self.qwen.complete('dream',bundle)
                    except CortexError as e: errors.append(type(e).__name__)
                    st=self._model_state()
                    if q is None:
                        n=st['attempts']+1 if st['input']==asked else 1
                        if n>=self.MODEL_ATTEMPTS:
                            st.update(answered=asked,input=None,attempts=0)    # explicitly abandoned, and recorded
                            self.store.decision('dream_model','local',None,False,f'input abandoned after {n} failed model attempts')
                        else: st.update(input=asked,attempts=n); model_pending=model_failed=True
                    elif interrupted(): model_pending=True                     # a late answer is not kept: asked again
                    else: st.update(answered=asked,input=None,attempts=0)
                    self._save_model_state(st)
                    if q and not interrupted():
                        for h in q.value.get('candidates',[])[:3]:
                            # One bad candidate never sinks its siblings or the cycle.
                            try:
                                if not isinstance(h,dict) or set(h)!={'text','refs'} or not isinstance(h['refs'],list) or not all(isinstance(x,str) for x in h['refs']):
                                    raise Missing('malformed hypothesis candidate')
                                if not h['refs'] or not set(h['refs'])<=set(refs): raise Missing('hypothesis must cite allowed refs')
                                m=self.store.derive('hypothesis',{'text':text(h['text'],'hypothesis',600)},h['refs'],origin='qwen_idle_hypothesis')
                                made.append(m['id'])
                            except CortexError as e: errors.append('hypothesis rejected: '+type(e).__name__)
            status='yielded' if interrupted() else 'complete'; leftover=leftover or (model_pending and model_wait is None)
        except Exception as e:
            status='failed'; errors.append(type(e).__name__)
        result={'id':run,'status':status,'derived_ids':list(dict.fromkeys(made)),'practice_ids':practiced,'errors':errors,'model_pending':model_pending,
                'mode':'SIMULATED_PRACTICE','physical_actions':0,'weights_updated':False}
        with self.store.tx():
            if status=='complete' and not leftover: self.store.conn.execute("INSERT INTO meta VALUES('cx_dream_watermark',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(watermark,))
            for key,wait in (('cx_dream_practice_wait',practice_wait),('cx_dream_model_wait',model_wait)):
                if status=='complete' and wait: self.store.conn.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(wait)))
                elif status=='complete': self.store.conn.execute('DELETE FROM meta WHERE key=?',(key,))
            self.store.conn.execute('UPDATE cx_dream SET finished_at=?,status=?,summary_json=? WHERE id=?',(self.store.clock(),status,canonical(result),run))
        if status!='yielded': self._backoff(status=='failed' or model_failed)
        return result
