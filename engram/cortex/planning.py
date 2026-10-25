"""Typed task composition plus a separately implemented symbolic outcome oracle.

No eval, exec, generated Python, shell access, or motor access exists here.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from .store import Store
from .types import Conflict, CortexError, Goal, Missing, Policy, canonical, digest, finite, normalize

NEGATED = re.compile(r"\b(do not|don't|dont|never|no need to|must not|mustn't|not now|stop moving)\b", re.I)
FUTURE = re.compile(r'\b(when|once|after|until|later|tomorrow|tonight|in\s+\d+\s+(?:seconds?|minutes?|hours?))\b', re.I)
FORBIDDEN = re.compile(r'\b(delete|erase|wipe|dump|dispose|discard|throw|pour|hit|break|override|disable)\b', re.I)
RECALL = re.compile(r'\b(what happened|where (?:is|was|are|were)|what did|what do you (?:know|remember)|what is pending|what have|why did|show (?:my|the) memor|recall)\b', re.I)

# This is a subset of the team's verified adapter interface, not newly invented motor behavior.
CAPS = {
    'stop': {}, 'observe': {}, 'locate_tag': {'object_id':str},
    'approach_tag': {'object_id':str}, 'pick_tagged_object': {'object_id':str},
    'place_at_zone': {'zone':str}, 'go_to_zone': {'zone':str},
    'gripper_open': {}, 'gripper_close': {},
    'drive_bounded': {'distance_m':float}, 'turn_bounded': {'degrees':float},
    'speak': {'text':str},
}
PHYSICAL = set(CAPS) - {'stop','observe','locate_tag','speak'}


def safety_reason(request: str) -> str | None:
    t = request.replace('\u2019', "'")
    if NEGATED.search(t): return 'Negative or cancelled instruction: no physical proposal.'
    if FORBIDDEN.search(t): return 'This operation is not supported by the robot.'
    if FUTURE.search(t): return 'Future intent must be stored as a commitment, not executed now.'
    return None


def parse_goal(request: str, store: Store) -> Goal:
    if safety_reason(request): raise Missing(safety_reason(request))
    alias = store.goal_alias(request)
    if alias: return alias
    low=normalize(request)
    primitive = {'open the claw':'gripper_open','open claw':'gripper_open','open the gripper':'gripper_open',
                 'close the claw':'gripper_close','close claw':'gripper_close','close the gripper':'gripper_close'}
    if low in primitive: return Goal('primitive',primitive=primitive[low])
    if low in {'look around','observe','what do you see'}:
        return Goal('primitive',primitive='observe')
    ids=store.resolve_objects(request)
    zone=store.resolve_zone(request)
    if re.search(r'\b(bring|fetch|move|take|carry|put)\b',low):
        if not ids or zone is None: raise Missing('name the object and one destination, or teach a task definition')
        return Goal('fetch',tuple(ids),zone)
    if re.search(r'\b(go|return|drive)\b',low) and zone and not ids:
        return Goal('goto',zone=zone)
    if re.search(r'\b(find|locate|inspect|look at)\b',low) and ids:
        return Goal('inspect',tuple(ids))
    raise Missing('not a supported task yet; specify a goal or teach its source-backed task definition')


def validate_step(step: Any, object_ids: set[str], zones: set[str]) -> None:
    if not isinstance(step,dict) or set(step)!={'op','args'} or step.get('op') not in CAPS:
        raise CortexError('step must contain an allowlisted op and args')
    op,args=step['op'],step['args']
    if not isinstance(args,dict) or set(args)!=set(CAPS[op]): raise CortexError('wrong primitive argument names')
    for key,want in CAPS[op].items():
        value=args[key]
        if want is float:
            finite(value,key,-0.5 if key=='distance_m' else -180,0.5 if key=='distance_m' else 180)
        elif not isinstance(value,str) or not value or len(value)>280:
            raise CortexError('invalid primitive string')
    if 'object_id' in args and args['object_id'] not in object_ids: raise Missing('unknown object id')
    if 'zone' in args and args['zone'] not in zones: raise Missing('unknown station id')


@dataclass
class Verification:
    ok: bool
    errors: list[str]=field(default_factory=list)
    final_state: dict=field(default_factory=dict)
    mode: str='SIMULATED'
    verifier: str='symbolic-verifier-v1'
    steps_checked: int=0

    def to_dict(self): return vars(self)


def verify(goal: Goal, steps: list[dict], fixture: dict, rules: list[dict], policy: Policy,
           *, expected_basis: str | None=None, actual_basis: str | None=None) -> Verification:
    """Oracle receives independently specified goal/initial state, never a model's success claim."""
    res=Verification(False)
    if expected_basis is not None and expected_basis != actual_basis:
        res.errors.append('changed evidence basis'); return res
    if not isinstance(steps,list) or not 1<=len(steps)<=policy.max_steps:
        res.errors.append('empty or over-budget plan'); return res
    positions=dict(fixture.get('positions',{})); zones=set(fixture.get('zones',[]))
    objects=dict(fixture.get('objects',{})); seen=set(); holding=None; at=fixture.get('at_zone')
    original=dict(positions); open_=True; stopped=False
    goal_ids=set(goal.targets)
    relevant=set(goal_ids)
    for _ in range(policy.max_nodes):
        old=len(relevant)
        relevant.update(r['object'] for r in rules if r['subject'] in relevant)
        if old==len(relevant): break
    if goal.kind=='fetch' and (not goal_ids<=set(objects) or goal.zone not in zones):
        res.errors.append('unknown goal target/destination'); return res
    if any(r.get('status')!='active' for r in rules if r['subject'] in relevant):
        res.errors.append('unresolved rule conflict'); return res
    try:
        for step in steps:
            validate_step(step,set(objects),zones)
            op,a=step['op'],step['args']; res.steps_checked+=1
            if op=='speak' and goal.kind!='primitive':raise Conflict('narration is emitted only after verified outcome, not by a motion plan')
            if op in PHYSICAL and stopped: raise Conflict('motion after stop')
            if op in ('drive_bounded','turn_bounded','gripper_open','gripper_close') and goal.kind!='primitive':
                raise Conflict('unrequested low-level effect')
            if op in ('go_to_zone',) and goal.kind not in {'goto','primitive'}:
                raise Conflict('unrequested navigation')
            if op in ('pick_tagged_object','place_at_zone','approach_tag') and goal.kind not in {'fetch','primitive'}:
                raise Conflict('unrequested manipulation')
            if op in PHYSICAL and goal.kind=='primitive' and (op!=goal.primitive or a!=goal.args):
                raise Conflict('primitive differs from the requested effect')
            oid=a.get('object_id')
            if oid and goal.kind in {'fetch','inspect'} and oid not in relevant:
                raise Conflict('plan acts on an unrelated object')
            if op=='stop': stopped=True
            elif op=='observe': seen.update(positions)
            elif op=='locate_tag':
                if oid not in positions: raise Missing('object not present in the fixture')
                seen.add(oid)
            elif op in {'pick_tagged_object','approach_tag'}:
                if oid not in seen: raise Missing('must locate the target before manipulation')
                if oid not in positions: raise Missing('object position missing')
                if op=='approach_tag':
                    at=positions[oid]; continue
                if holding: raise Conflict('already holding an object')
                if not objects[oid].get('graspable',True): raise DeniedPlan('object not graspable')
                for r in rules:
                    if r['subject']==oid and positions.get(r['object'])!=r['required_zone']:
                        raise Conflict('prerequisite object is not at its required station')
                at=positions.pop(oid); holding=oid; open_=False
            elif op=='place_at_zone':
                if holding is None: raise Conflict('placement without a held object')
                positions[holding]=a['zone']; at=a['zone']; holding=None; open_=True
            elif op=='go_to_zone': at=a['zone']
            elif op=='gripper_open':
                if holding: positions[holding]=at; holding=None
                open_=True
            elif op=='gripper_close': open_=False
        if holding: raise Conflict('task ends with unplaced object')
        if goal.kind=='fetch' and any(positions.get(oid)!=goal.zone for oid in goal.targets):
            raise Conflict('requested goal was not reached')
        if goal.kind=='goto' and at!=goal.zone: raise Conflict('wrong final station')
        if goal.kind=='inspect' and not goal_ids<=seen: raise Conflict('inspection did not observe the requested objects')
        if goal.kind=='primitive' and not any(s['op']==goal.primitive and s['args']==goal.args for s in steps):
            raise Conflict('requested primitive missing')
        if goal.kind=='primitive' and sum(s['op'] in PHYSICAL for s in steps)>1:
            raise Conflict('primitive repeated or additional effect')
        if goal.kind=='fetch' and any(positions.get(k)!=v for k,v in original.items() if k not in relevant):
            raise Conflict('unrelated object changed')
        res.ok=True
    except CortexError as e:
        res.errors.append(str(e))
    res.final_state={'positions':positions,'at_zone':at,'holding':holding,'gripper_open':open_,'stopped':stopped}
    return res


class DeniedPlan(CortexError): pass


class Compiler:
    """Deterministic synthesis from a supported goal and CURRENT accepted rules."""
    def __init__(self, store: Store): self.store=store

    def fixture(self, *, mode: str='SIMULATED') -> dict:
        zones=sorted(self.store.zones())
        if not zones: raise Missing('no station registry')
        # Synthetic practice may fill unknown positions; explicitly not an observation.
        objects={o['id']:{'tag':o['tag'],'graspable':o['graspable']} for o in self.store.objects()}
        neutral='home' if 'home' in zones else zones[0]
        pos={o['id']:o['last_zone'] if o['fresh'] and o['last_zone'] else neutral for o in self.store.objects()}
        return {'mode':mode,'positions':pos,'objects':objects,'zones':zones,'at_zone':neutral,'assumptions':[o['id'] for o in self.store.objects() if not o['fresh']]}

    def compile(self, goal: Goal, fixture: dict | None=None) -> dict:
        goal=Goal.from_dict(goal.to_dict())
        f=copy.deepcopy(fixture or self.fixture())
        rules=self.store.related_rules(goal)
        relevant=set(goal.targets)|{r['object'] for r in rules}
        if self.store.unresolved_for(relevant):raise Missing('an applicable source has an unresolved conditional rule; structured review required')
        if self.store.prohibitions(relevant) and goal.kind=='fetch': raise DeniedPlan('current source prohibits moving an involved object')
        if any(r['status']!='active' for r in rules): raise Conflict('conflicting applicable source rules need resolution')
        positions=dict(f['positions']); steps=[]; ancestry=set()
        if not set(goal.targets)<=set(f['objects']): raise Missing('unregistered goal target')
        if goal.zone and goal.zone not in f['zones']: raise Missing('unregistered destination')

        def place(oid,zone,depth=0):
            if depth>self.store.policy.max_depth: raise Conflict('dependency depth exceeded')
            if oid in ancestry: raise Conflict('cyclic prerequisites')
            if oid not in f['objects']: raise Missing('unknown prerequisite')
            if positions.get(oid)==zone: return
            ancestry.add(oid)
            for r in rules:
                if r['subject']==oid: place(r['object'],r['required_zone'],depth+1)
            ancestry.remove(oid)
            steps.extend([{'op':'locate_tag','args':{'object_id':oid}},
                          {'op':'pick_tagged_object','args':{'object_id':oid}},
                          {'op':'place_at_zone','args':{'zone':zone}}])
            positions[oid]=zone
            if len(steps)>self.store.policy.max_steps: raise Missing('task exceeds bounded primitive budget')

        if goal.kind=='fetch':
            # Validate graph cycles even when the current fixture already satisfies a rule.
            def visit(oid,path=()):
                if oid in path: raise Conflict('cyclic prerequisite graph')
                if len(path)>self.store.policy.max_depth: raise Conflict('dependency depth exceeded')
                for r in rules:
                    if r['subject']==oid: visit(r['object'],path+(oid,))
            for oid in goal.targets: visit(oid)
            for oid in goal.targets: place(oid,goal.zone)
        elif goal.kind=='goto': steps=[{'op':'go_to_zone','args':{'zone':goal.zone}}]
        elif goal.kind=='inspect': steps=[{'op':'locate_tag','args':{'object_id':oid}} for oid in goal.targets]
        elif goal.kind=='primitive': steps=[{'op':goal.primitive,'args':goal.args}]
        if not steps: steps=[{'op':'observe','args':{}}]
        v=verify(goal,steps,f,rules,self.store.policy)
        if not v.ok: raise Conflict('; '.join(v.errors))
        refs=sorted(set('source:'+r['source_id'] for r in rules)|set('rule:'+r['id'] for r in rules))
        plan={'goal':goal.to_dict(),'steps':steps,'rules_used':[r['id'] for r in rules],
              'evidence':sorted(set(r['chunk_id'] for r in rules)), 'refs':refs,
              'basis':self.store.basis(goal),'policy_hash':self.store.policy.fingerprint(),
              'verification':v.to_dict(),'origin':'composed_from_current_memory'}
        return plan
