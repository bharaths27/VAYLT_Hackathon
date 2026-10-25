"""Typed task composition plus a separately implemented symbolic outcome oracle.

No eval, exec, generated Python, shell access, or motor access exists here.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .store import Store
from .types import Conflict, CortexError, Goal, Missing, Policy, canonical, digest, finite, normalize

NEGATED = re.compile(r"\b(do not|don't|dont|never|no need to|must not|mustn't|not now|stop moving|cancel|cancell?ed|never ?mind|scratch that|forget (?:it|that)|hold on|hold off|wait|undo|abort)\b", re.I)
FUTURE = re.compile(r'\b(when|once|after|until|later|tomorrow|tonight|in\s+\d+\s+(?:seconds?|minutes?|hours?))\b', re.I)
FORBIDDEN = re.compile(r'\b(delete|erase|wipe|dump|dispose|discard|throw|pour|hit|break|override|disable)\b', re.I)
# The stop vocabulary of the brain (engram/engine.py) and the console parser, deliberately broad: any utterance that
# contains one is a STOP ("don't stop the timer" too). A needless stop costs one clear_stop; a missed one can hurt someone.
STOP_WORDS = re.compile(r"\b(stop|estop|halt|freeze|abort|emergency|whoa|wait|pause|hold\s+on|hold\s+it|hang\s+on)\b", re.I)
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
# Ops that move the robot (and so whatever the claw holds).
NAVIGATION = {'go_to_zone','drive_bounded','turn_bounded','approach_tag'}
# The ONLY ops a plan for each goal kind may contain (the model's JSON schema uses the same table).
GOAL_OPS = {'fetch':('observe','locate_tag','pick_tagged_object','place_at_zone'),'goto':('go_to_zone',),'inspect':('observe','locate_tag')}
def goal_ops(goal: dict | Goal | None) -> tuple[str, ...]:
    g=goal.to_dict() if isinstance(goal,Goal) else goal or {}
    return (g.get('primitive'),) if g.get('kind')=='primitive' and g.get('primitive') in CAPS else GOAL_OPS.get(g.get('kind'),())

# Grammar: refuse by default. Anything below means the utterance says more than "move X to Y".
QUESTION = re.compile(r"\?\s*$|^\s*(?:should|shall|do|does|did|is|are|was|were|am|may|might|must|what|why|how|which|who|whose|whether|where|when|(?:can|could|would|will)\s+(?:i|we)\b)\b", re.I)
# Routing, not refusal: a question Brain.command answers from memory. A question mark or a question opener counts,
# except a polite request ("can/could/would/will you ...", "please ..."), a "do not ..." instruction, and a bare
# "when <event> <action>" commitment ("when" counts only before an auxiliary). "What do you see?" is a question too.
OBSERVE = {'look around','observe'}
POLITE = re.compile(r"^\s*(?:(?:can|could|would|will)\s+you|please)\b", re.I)
ASKS = re.compile(r"\?\s*$|^\s*(?:what|where|which|who|whom|whose|whether|why|how|is|are|was|were|am|(?:do|does|did)\s+(?!not\b)|"
                  r"can|could|should|shall|would|will|may|might|must|when\s+(?:is|are|was|were|do|does|did|will|would|can|could|should|shall|has|have|had))\b", re.I)


def asks_memory(request: str) -> bool:
    """A question for memory, not an instruction: parse_goal refuses every question shape, so routing loses nothing."""
    t=request.replace(chr(0x2019),"'")
    return bool(RECALL.search(t)) or (bool(ASKS.search(t)) and not POLITE.search(t))
HEDGES = (
    (re.compile(r"\b(?:not|no|nor|except|excluding|apart from|other than|without|instead|rather|alone|leave|but)\b", re.I), 'an exclusion or correction ("not", "except", "without", "leave ... alone", "instead")'),
    (re.compile(r"\b(?:off|away|out|from)\b", re.I), 'a source location ("off", "away from", "out of", "from"); name only the destination'),
    (re.compile(r"\b(?:or|either)\b", re.I), 'alternatives ("or")'),
    (re.compile(r"\b(?:if|unless|provided|in case)\b", re.I), 'a condition ("if", "unless")'),
    (re.compile(r"\b(?:next to|near|nearby|beside|behind|under|underneath|beneath|above|below|on top of|in front of|between|left of|right of)\b", re.I), 'a relative position ("next to", "near"); name a registered station'),
    (re.compile(r"\b(?:then|also|afterwards?|before)\b", re.I), 'a sequence of actions; give one instruction at a time'),
)
VERBS = {'fetch':r'bring|fetch|move|take|carry|put','goto':r'go|return|drive','inspect':r'find|locate|inspect|look at'}
FILLER = {'the','a','an','to','on','onto','in','into','at','and','please','kindly','now','for','me','us','here','there','back','can','could','would','will','you','just','ok','okay','hey','um','uh','so','robot'}


def safety_reason(request: str) -> str | None:
    t = request.replace('\u2019', "'")
    if NEGATED.search(t): return 'Negative or cancelled instruction: no physical proposal.'
    if FORBIDDEN.search(t): return 'This operation is not supported by the robot.'
    if FUTURE.search(t): return 'Future intent must be stored as a commitment, not executed now.'
    return None


def ambiguity_reason(request: str) -> str | None:
    """Why the plain grammar must NOT guess a goal from this utterance (None if it may)."""
    t=request.replace('\u2019',"'")
    if QUESTION.search(t): return 'That reads as a question, not an instruction; say it as an instruction to get a proposal.'
    for rx,what in HEDGES:
        if rx.search(t): return f'The request contains {what}; nothing was guessed. Say only which object(s) go to which one station.'
    return None


def _uncovered(low: str, store: Store) -> list[str]:
    """Words the grammar did not account for; any leftover means the parse would drop meaning."""
    names={normalize(n) for o in store.objects() for n in [o['id'],o['name'],*o['aliases']]}
    names|={normalize(n) for z in store.rows('SELECT * FROM cx_station') for n in [z['id'],z['name'],*json.loads(z['aliases_json'])]}
    rest=' '+low+' '
    for n in sorted(filter(None,names),key=len,reverse=True)+[v for vs in VERBS.values() for v in vs.split('|')]:
        rest=re.sub(r'(?<!\S)'+re.escape(n)+r'(?!\S)',' ',rest)
    return [w for w in rest.split() if w not in FILLER]


def parse_goal(request: str, store: Store) -> Goal:
    if safety_reason(request): raise Missing(safety_reason(request))
    low=normalize(request)
    if QUESTION.search(request): raise Missing(ambiguity_reason(request))   # before any phrase allowlist
    if low in OBSERVE:
        return Goal('primitive',primitive='observe')
    primitive = {'open the claw':'gripper_open','open claw':'gripper_open','open the gripper':'gripper_open',
                 'close the claw':'gripper_close','close claw':'gripper_close','close the gripper':'gripper_close'}
    if low in primitive: return Goal('primitive',primitive=primitive[low])
    alias = store.goal_alias(request)
    if alias: return alias
    if ambiguity_reason(request): raise Missing(ambiguity_reason(request))
    kinds=[k for k,v in VERBS.items() if re.search(r'\b(?:'+v+r')\b',low)]
    if len(kinds)>1 or sum(len(re.findall(r'\b(?:'+v+r')\b',low)) for v in VERBS.values())>1:
        raise Missing('more than one action in one request; give one instruction at a time')
    ids=store.resolve_targets(request)   # each target named unambiguously: an alias two objects share is refused
    zone=store.resolve_zone(request)
    extra=_uncovered(low,store)
    if kinds and extra: raise Missing('unrecognised words '+repr(' '.join(extra)[:80])+'; name only registered objects and one registered station')
    if kinds==['fetch']:
        if not ids or zone is None: raise Missing('name the object and one destination, or teach a task definition')
        return Goal('fetch',tuple(ids),zone)
    if kinds==['goto'] and zone and not ids:
        return Goal('goto',zone=zone)
    if kinds==['inspect'] and ids and zone is None:
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
    # Effect-set oracle. Allowed ops come from the goal kind alone. A placement is an
    # effect only if it is REQUIRED: a target to the destination, or a prerequisite to
    # its required station before its subject is picked later in this same plan.
    # Unknown (None) positions are never treated as satisfied.
    allowed=set(goal_ops(goal))
    def picked_later(oid,i): return any(isinstance(s,dict) and s.get('op')=='pick_tagged_object' and isinstance(s.get('args'),dict) and s['args'].get('object_id')==oid for s in steps[i+1:])
    def required(oid,zone,i): return (oid in goal_ids and zone==goal.zone) or any(r['object']==oid and r['required_zone']==zone and picked_later(r['subject'],i) for r in rules)
    placed={}; picked_from={}
    # Held-object state comes from the adapter's report (fixture 'holding_known'/'holding'); unknown is never "empty".
    loaded=fixture.get('holding') if fixture.get('holding_known') else 'an unknown object'
    try:
        if goal.kind in {'primitive','goto'} and len(steps)!=1: raise Conflict('extra effect: this goal is exactly one step')
        for i,step in enumerate(steps):
            validate_step(step,set(objects),zones)
            op,a=step['op'],step['args']; res.steps_checked+=1
            if op not in allowed: raise Conflict(f'extra effect: {op} is not allowed for a {goal.kind} goal')
            if op in PHYSICAL and stopped: raise Conflict('motion after stop')
            # The claw's initial content travels with every navigation and blocks every pickup: a reported load, or an
            # unreported (unknown) content, fails closed. Opening the claw is checked below.
            if loaded is not None and (op in NAVIGATION or op=='pick_tagged_object'):
                raise Conflict(f'the claw already holds {loaded}; {op} would carry it or needs an empty claw' if fixture.get('holding_known') else
                               f'the claw contents are unknown (not reported by the adapter); {op} needs a reported empty claw')
            if goal.kind=='primitive' and (op!=goal.primitive or a!=goal.args):
                raise Conflict('primitive differs from the requested effect')
            if goal.kind=='goto' and a.get('zone')!=goal.zone: raise Conflict('navigation to a station other than the goal')
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
                at=positions.pop(oid); holding=oid; open_=False; picked_from[oid]=at
            elif op=='place_at_zone':
                if holding is None: raise Conflict('placement without a held object')
                if goal.kind=='fetch' and not required(holding,a['zone'],i):
                    raise Conflict(f"extra effect: {holding} to {a['zone']} is neither the destination nor a prerequisite station needed later")
                if placed.get(holding)==a['zone']: raise Conflict('extra effect: repeated move to where this plan already put the object')
                # No physical handling without state progress: an object already where it must be stays untouched.
                if picked_from.get(holding)==a['zone']: raise Conflict(f"extra effect: {holding} is put back where it already was; handling it makes no progress")
                positions[holding]=placed[holding]=a['zone']; at=a['zone']; holding=None; open_=True
            elif op=='go_to_zone': at=a['zone']
            elif op=='gripper_open':
                if holding is None and loaded is not None:
                    raise Conflict(f'the claw may be holding {loaded}; opening it would release that object without a placement check')
                if holding: positions[holding]=at; holding=None
                open_=True
            elif op=='gripper_close': open_=False
        if holding: raise Conflict('task ends with unplaced object')
        if goal.kind=='fetch' and any(positions.get(oid)!=goal.zone for oid in goal.targets):
            raise Conflict('requested goal was not reached' if all(original.get(o) is not None for o in goal.targets) else
                           'requested goal was not reached: a target position is unknown (no fresh observation), so it must be moved, not assumed')
        if goal.kind=='goto' and at!=goal.zone: raise Conflict('wrong final station')
        if goal.kind=='inspect' and not goal_ids<=seen: raise Conflict('inspection did not observe the requested objects')
        if goal.kind=='fetch' and any(positions.get(k)!=v for k,v in original.items() if k not in relevant):
            raise Conflict('unrelated object changed')
        res.ok=True
    except CortexError as e:
        res.errors.append(str(e))
    res.final_state={'positions':positions,'at_zone':at,'holding':holding if holding is not None else loaded,'gripper_open':open_,'stopped':stopped}
    return res


class DeniedPlan(CortexError): pass


class CompilerGap(Conflict):
    """The compiler composed a plan for a supported goal that its own independent check rejects: a gap in the
    compiler, not a refusal. Only this may go to the local model (whose plan passes the same refusals and checks)."""


class Compiler:
    """Deterministic synthesis from a supported goal and CURRENT accepted rules."""
    def __init__(self, store: Store): self.store=store

    def fixture(self, *, mode: str='SIMULATED', symbolic: bool=False) -> dict:
        zones=sorted(self.store.zones())
        if not zones: raise Missing('no station registry')
        # Only a FRESH observation is a position. Anything else is unknown (None), never
        # filled in, so neither the planner nor the verifier can rely on an invented world.
        objs=self.store.objects()
        objects={o['id']:{'tag':o['tag'],'graspable':o['graspable']} for o in objs}
        pos={o['id']:o['last_zone'] if o['fresh'] and o['last_zone'] else None for o in objs}
        # What the claw holds is the adapter's to report (Brain._fixture); the store alone never knows, so: unknown.
        # A symbolic start state (practice, skill tests, a plan composed without a live world) declares an empty claw;
        # live proposal and approval re-verify against the adapter's report.
        return {'mode':mode,'positions':pos,'objects':objects,'zones':zones,'at_zone':None,'unknown_positions':sorted(k for k,v in pos.items() if v is None),
                'holding':None,'holding_known':symbolic}

    def compile(self, goal: Goal, fixture: dict | None=None, *, steps: list[dict] | None=None) -> dict:
        """The plan for a supported goal: composed here or, given steps (the local model's), those, held to the same
        refusals and the same independent check. A composed plan that check rejects is a CompilerGap."""
        goal=Goal.from_dict(goal.to_dict())
        f=copy.deepcopy(fixture or self.fixture(symbolic=True))   # no live world given: the symbolic start state
        rules=self.store.related_rules(goal)
        relevant=set(goal.targets)|{r['object'] for r in rules}
        if self.store.unresolved_for(self.store.scope(goal,rules),handles=goal.kind=='fetch'):raise Missing('an applicable source has an unresolved conditional rule or safety statement; structured review required')
        if self.store.prohibitions(relevant) and goal.kind=='fetch': raise DeniedPlan('current source prohibits moving an involved object')
        if any(r['status']!='active' for r in rules): raise Conflict('conflicting applicable source rules need resolution')
        if not set(goal.targets)<=set(f['objects']): raise Missing('unregistered goal target')
        if goal.zone and goal.zone not in f['zones']: raise Missing('unregistered destination')
        if goal.kind=='fetch':
            # Refuse a cyclic or too deep prerequisite graph, even where the current fixture already satisfies a rule.
            def visit(oid,path=()):
                if oid in path: raise Conflict('cyclic prerequisite graph')
                if len(path)>self.store.policy.max_depth: raise Conflict('dependency depth exceeded')
                for r in rules:
                    if r['subject']==oid: visit(r['object'],path+(oid,))
            for oid in goal.targets: visit(oid)
        composed=steps is None
        if composed: steps=self._compose(goal,f,rules)
        v=verify(goal,steps,f,rules,self.store.policy)
        if not v.ok: raise (CompilerGap if composed else Conflict)('; '.join(v.errors))
        refs=sorted(set('source:'+r['source_id'] for r in rules)|set('rule:'+r['id'] for r in rules))
        plan={'goal':goal.to_dict(),'steps':steps,'rules_used':[r['id'] for r in rules],
              'evidence':sorted(set(r['chunk_id'] for r in rules)), 'refs':refs,
              'basis':self.store.basis(goal),'policy_hash':self.store.policy.fingerprint(),
              'verification':v.to_dict(),'origin':'composed_from_current_memory' if composed else 'proposed_and_verified'}
        return plan

    def _compose(self, goal: Goal, f: dict, rules: list[dict]) -> list[dict]:
        positions=dict(f['positions']); steps=[]; ancestry=set()

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
            def needs(oid,acc):
                for r in rules:
                    if r['subject']==oid and r['object'] not in acc: acc.add(r['object']); needs(r['object'],acc)
                return acc
            # A target that another target needs as a prerequisite is delivered LAST.
            for oid in sorted(goal.targets,key=lambda t:sum(t in needs(u,set()) for u in goal.targets if u!=t)): place(oid,goal.zone)
        elif goal.kind=='goto': steps=[{'op':'go_to_zone','args':{'zone':goal.zone}}]
        elif goal.kind=='inspect': steps=[{'op':'locate_tag','args':{'object_id':oid}} for oid in goal.targets]
        elif goal.kind=='primitive': steps=[{'op':goal.primitive,'args':goal.args}]
        return steps or [{'op':'observe','args':{}}]
