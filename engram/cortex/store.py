"""One authoritative SQLite vault; explicit evidence, revisions and lifecycles.

Uses the team's existing source/assertion/observation/episode/skill/task tables.
No embeddings are fabricated. Retrieval combines FTS, exact entity resolution,
prerequisite graph expansion and lifecycle filtering. Optional ranking is advisory.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from . import vault_lock
from .schema import migrate
from .types import (CONTROL_OPS, Conflict, CortexError, Denied, Goal, Missing, Policy, Stale,
                    canonical, digest, finite, normalize, text, uid)

# Commitment status implied by its proposal's outcome; a settled commitment is never rewritten.
TASK_STATE = {'success':'done','failed':'failed','rejected':'failed','unknown':'unknown','aborted':'unknown',
              'cancelled':'cancelled','expired':'expired','invalidated':'failed'}
STARTED = ('running','unknown','needs_verification')
# The status words the runtime itself journals for an operation's steps (any other status is an adapter's own word).
JOURNAL_STATES = ('pending','not_dispatched','reported_done','unknown','failed','rejected','success','noted')

RULE_RE = re.compile(
    r'(?P<pre>[A-Za-z0-9 _-]+?)\s+(?:must|should|needs? to)\s+be\s+'
    r'(?:at|on|in)\s+(?:the\s+)?(?P<zone>[A-Za-z0-9 _-]+?)\s+before\s+'
    r'(?:handling|using|moving|touching)\s+(?P<subject>[A-Za-z0-9 _-]+)', re.I)
PROHIBIT_RE = re.compile(r"\b(never|must not|do not|don't)\s+(move|touch|handle|pick|carry)\b", re.I)
# Deontic or hazard wording. Left over after the two templates above in a sentence that names a registered
# object or station, it is never silently accepted: it is an unresolved constraint until a person structures it.
SAFETY_RE = re.compile(r"\b(?:must|mustn't|never|should|shouldn't|shall|needs? to|ha(?:s|ve) to|required?|requires|forbidden|prohibited|"
                       r"not (?:allowed|permitted)|not\s+to\s+be|(?:is|are)\s+to\s+be\s+(?:kept|left|used|moved|handled|stored)|off[- ]limits|"
                       r"out\s+of\s+(?:bounds|service|order)|(?:no\s*one|nobody)\s+(?:may|should|can|is\s+to)|"
                       r"do not|don't|cannot|can't|may not|only|always|avoid|keep|hazard\w*|danger\w*|caution|warning)\b", re.I)
# A negated handling statement in any word order ("must never be handled", "should not be moved", "nobody moves it").
NEG_HANDLE_RE = re.compile(r"(?:\b(?:never|not|no|cannot|nobody|nothing)\b|n't\b).*\b(?:move[ds]?|moving|touch\w*|handl\w*|pick\w*|carr\w*|lift\w*|grab\w*|transport\w*)\b", re.I)
# Handling wording: a verb of manipulating an object (not its noun: "the handle", "a lift"), the claw that does it, or
# leaving an object be ("leave Sample A alone", "hands off"). A statement using it is about handling; one without it, or
# without the constraint wording below, is a fact that blocks nothing ("Fridge 2 holds Sample A", "It is hot today").
HANDLE_RE = re.compile(r"\b(?<!\ba )(?<!\ban )(?<!\bthe )(?<!\bits )(?<!\btheir )"
                       r"(?:carr(?:y|ies|ied|ying)|lift\w*|pick(?:s|ed|ing)?|grab\w*|grip\w*|grasp\w*|handl\w*|touch\w*|mov(?:e|es|ed|ing)|"
                       r"tilt\w*|drop\w*|stack\w*|squeez\w*|transport\w*)\b|\b(?:claws?|grippers?|untouched|disturb\w*|hands?\s+off|stay\w*\s+put|"
                       r"leave\s+(?:\w+\s+){0,3}(?:alone|untouched|be|where|in\s+place)|left\s+(?:alone|untouched|in\s+place))\b", re.I)
# An instruction in imperative mood: optional adverbs or negation, then the verb ("Carry it upright", "Never lift anything").
IMPERATIVE_RE = re.compile(r"^\s*(?:(?:please|always|never|only|just|also|then|first|now|carefully|gently|slowly|do\s+not|don't|dont)\s+)*(?P<verb>[a-z]+)\b", re.I)
HAND_VERBS = {'carry','lift','pick','grab','grip','grasp','hold','handle','touch','move','tilt','drop','stack','squeeze','transport',
              'put','place','set','lay','load','store','push','pull'}
GO_VERBS = {'go','drive','navigate','travel'}
# Words that may refer to any object: every object at once ("never lift anything"), or one a sentence does not name ("it").
EVERYTHING_RE = re.compile(r"\b(?:anything|everything|nothing|all|any|every|each|none)\b", re.I)
PRONOUN_RE = re.compile(r"\b(?:it|its|they|them|this|these|those|that|there)\b", re.I)
MODAL_RE = re.compile(r"\b(?:may|might|can|could|must|shall|should|cannot|can't|mustn't|shouldn't)\b", re.I)
# Conditionals: every if/unless/only-if/provided/except form and approval-gated forms ("when approved", "with permission").
CONDITIONAL_RE = re.compile(r"\b(?:unless|except|exceptions?|only\s+if|if|provided|providing|as\s+long\s+as|so\s+long\s+as|on\s+condition|in\s+case|"
                            r"subject\s+to|pending|until\s+(?:\w+\s+){0,3}(?:approv\w*|authori[sz]\w*|clear\w*|permit\w*)|"
                            r"(?:when|whenever|once|after)\s+(?:\w+\s+){0,3}(?:approv\w*|authori[sz]\w*|permit\w*|allow\w*|clear\w*|signs?\s+off|signed\s+off|supervis\w*)|"
                            r"with(?:out)?\s+(?:\w+\s+){0,2}(?:approval|permission|authori[sz]ation|supervision|clearance|sign-?off|consent))\b", re.I)
# A condition of time or circumstance ("while the floor is wet", "when the tray arrives", "before noon"). On a statement
# about motion or handling it is a condition too, never resolved: it is checked on what a stored rule template leaves.
TEMPORAL_RE = re.compile(r"\b(?:while|whilst|when|whenever|once|after|before|until|till|during|as\s+soon\s+as)\b", re.I)
# A hazard or a permission ("unsafe", "dangerous", "a biohazard", "can/cannot/may be used, moved, handled", "not allowed",
# "requires approval"). Unparsed, it is never accepted silently: it becomes an unresolved constraint (scope: Store._reach).
HAZARD_RE = re.compile(r"\b(?:unsafe|safe(?:ly)?|danger\w*|\w*hazard\w*|toxic|flammable|corrosive|fragile|explosive|volatile|caution|warning|risk\w*|"
                       r"forbidden|prohibited|banned|restricted|allowed|permitted|authori[sz]ed|approved|permission|approval|"
                       r"(?:can|could|may|might|must|shall|should|cannot)(?:\s+not|n't)?(?:\s+only)?(?:\s+ever)?\s+be\s+(?:\w+\s+)?"
                       r"(?:used|moved|handled|touched|picked|carried|lifted|stored|placed|left|opened|taken|brought|transported))\b", re.I)
# Motion of the robot itself ("never drive", "driving is forbidden"); "move" too, which may mean either the robot or an
# object. Naming nothing, an instruction about it still has a subject: the robot (Store._reach).
LOCOMOTION_RE = re.compile(r"\b(?:go|goes|going|went|driv(?:e\w*|ing)|drove|enter\w*|navigat\w*|travel\w*|mov(?:e|es|ed|ing))\b", re.I)
# Motion of the robot or of what it takes somewhere, or use of a place ("goes on shelf B", "can be used"). A constraint
# speaking of it holds for every plan involving what it covers, not only handling plans.
MOTION_RE = re.compile(r"\b(?:go|goes|going|went|driv(?:e\w*|ing)|drove|enter\w*|navigat\w*|travel\w*|use[ds]?|using|bring\w*|brought|take|takes|taking|took|"
                       r"mov(?:e|es|ed|ing))\b", re.I)


class Store:
    def __init__(self, path: str | Path = ':memory:', *, workspace: str = 'local',
                 policy: Policy | None = None, clock: Callable[[], float] = time.time, lock: bool = True):
        self.path = str(path)
        self.workspace = text(workspace, 'workspace', 100)
        self.policy = policy or Policy()
        self.clock = clock
        self.lock = threading.RLock()
        # The vault's authority. A transaction that may withdraw authority from a running plan (a revocation, a new
        # constraint, a changed binding: authority_tx) holds it, and so does each plan step from its final check through
        # its dispatch (Brain._execute): such a change never lands between the two. It waits for the dispatched step, and
        # the next step's check sees it. Always taken before self.lock (held_authority enforces the order).
        self.authority = threading.RLock()
        self._held = threading.local()
        self._depth = 0
        # Own the vault before the first write (WAL switch, migration, arena registration).
        # lock=False only for a caller that already holds vault_lock for this path.
        self.file_lock = vault_lock.acquire(self.path) if lock and self.path != ':memory:' else None
        self.conn = None
        try:
            self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            if self.path != ':memory:':
                self.conn.execute('PRAGMA journal_mode=WAL')
                self.conn.execute('PRAGMA synchronous=FULL')
                self.conn.execute('PRAGMA secure_delete=ON')   # deleted source text is overwritten, not left in free pages
            migrate(self.conn)
            with self.tx():
                existing = self.one("SELECT value FROM meta WHERE key='cortex_workspace'")
                if existing and existing['value'] != self.workspace:
                    raise Denied('one local workspace per vault; use a separate database for another workspace')
                self.conn.execute("INSERT OR IGNORE INTO meta VALUES('cortex_workspace',?)", (self.workspace,))
        except BaseException:
            self.close(); raise

    def close(self):
        with self.lock:
            if self.conn: self.conn.close()
            if self.file_lock: self.file_lock.close(); self.file_lock = None

    @contextmanager
    def tx(self):
        with self.lock:
            root = self._depth == 0
            savepoint = f'cx_sp_{self._depth}'
            self.conn.execute('BEGIN IMMEDIATE' if root else f'SAVEPOINT {savepoint}')
            self._depth += 1
            self._held.tx = getattr(self._held, 'tx', 0) + 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if root:
                    self.conn.rollback()
                else:
                    self.conn.execute(f'ROLLBACK TO {savepoint}')
                    self.conn.execute(f'RELEASE {savepoint}')
                raise
            else:
                self._depth -= 1
                if not root:
                    self.conn.execute(f'RELEASE {savepoint}')
                    return
                try:
                    self.conn.execute('COMMIT')
                except BaseException:
                    # A refused COMMIT (e.g. a deferred constraint) leaves the transaction open; never wedge the shared connection.
                    if self.conn.in_transaction: self.conn.rollback()
                    raise
            finally:
                self._held.tx -= 1

    @contextmanager
    def held_authority(self):
        """Hold the vault's authority (see __init__). Taken before the store lock, never from inside a transaction that
        does not already hold it: that one order keeps a revocation and a dispatching step from deadlocking."""
        if getattr(self._held, 'tx', 0) and not getattr(self._held, 'authority', 0):
            raise RuntimeError('lock order: take the vault authority before opening a transaction')
        with self.authority:
            self._held.authority = getattr(self._held, 'authority', 0) + 1
            try:
                yield
            finally:
                self._held.authority -= 1

    @contextmanager
    def authority_tx(self):
        """A transaction that may withdraw authority from a running plan: it never commits between a step's final check
        and its dispatch."""
        with self.held_authority(), self.tx():
            yield self.conn

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def epoch(self) -> int:
        return int(self.one("SELECT value FROM meta WHERE key='cortex_epoch'")['value'])

    def _bump(self):
        self.conn.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='cortex_epoch'")

    def register_arena(self, arena: dict) -> None:
        if not isinstance(arena, dict) or not isinstance(arena.get('zones'), (list, dict)):
            raise CortexError('arena needs zones and objects')
        zones = arena['zones']
        with self.authority_tx():                         # a changed binding withdraws what a running plan rests on
            for z in zones:
                text(z, 'zone', 80)
                existing = self.one('SELECT * FROM cx_station WHERE id=?', (z,))
                tags = [k for k, v in arena.get('zone_tags', {}).items() if v == z]
                if any(not (isinstance(k, str) and k.isdigit() or isinstance(k, int) and not isinstance(k, bool)) for k in tags): raise CortexError('invalid station tag')
                tag = int(tags[0]) if tags else None
                self.conn.execute('INSERT INTO cx_station(id,name,aliases_json,tag_id,version) VALUES(?,?,?,?,1) '
                                  'ON CONFLICT(id) DO UPDATE SET tag_id=excluded.tag_id,version=version+CASE WHEN tag_id IS NOT excluded.tag_id THEN 1 ELSE 0 END',
                                  (z, z.replace('_', ' '), canonical([]), tag))
            for obj in arena.get('objects', []):
                oid, name = text(obj.get('id'), 'object id', 80), text(obj.get('name'), 'object name', 100)
                tag = obj.get('tag')
                if tag is not None and (isinstance(tag, bool) or not isinstance(tag, int) or not 0 <= tag <= 99999):
                    raise CortexError('invalid object tag')
                graspable, width = obj.get('graspable', True), obj.get('width_mm')
                if not isinstance(graspable, bool): raise CortexError('graspable must be true or false')
                if width is not None: finite(width, 'object width (mm)', 0, 10000)
                aliases = obj.get('aliases', [])
                if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases) or len(aliases) > 20:
                    raise CortexError('invalid object aliases')
                prior = self.one('SELECT * FROM object_binding WHERE object_id=?', (oid,))
                prior_alias = self.one('SELECT aliases_json FROM cx_entity WHERE object_id=?', (oid,))
                changed = prior is not None and (prior['name'] != name or prior['tag_id'] != tag or bool(prior['graspable']) != graspable
                           or (prior_alias and prior_alias['aliases_json'] != canonical(aliases)))
                self.conn.execute('INSERT INTO object_binding(object_id,name,tag_id,graspable,width_mm) VALUES(?,?,?,?,?) '
                                  'ON CONFLICT(object_id) DO UPDATE SET name=excluded.name,tag_id=excluded.tag_id,graspable=excluded.graspable,width_mm=excluded.width_mm',
                                  (oid, name, tag, int(graspable), width))
                self.conn.execute('INSERT INTO cx_entity(object_id,aliases_json,version) VALUES(?,?,1) '
                                  'ON CONFLICT(object_id) DO UPDATE SET aliases_json=excluded.aliases_json,version=version+?',
                                  (oid, canonical(aliases), int(changed)))
                if changed:
                    self.conn.execute('UPDATE object_binding SET last_zone=NULL,last_seen_at=NULL,last_observation_id=NULL WHERE object_id=?', (oid,))
                    self._bump()
            # Arena initial locations are NOT physical observations.

    def zones(self) -> set[str]:
        return {r['id'] for r in self.rows('SELECT id FROM cx_station')}

    def objects(self) -> list[dict]:
        out = self.rows('SELECT b.*,COALESCE(e.aliases_json,\'[]\') aliases_json,COALESCE(e.version,0) version '
                        'FROM object_binding b LEFT JOIN cx_entity e ON e.object_id=b.object_id ORDER BY b.object_id')
        t = self.clock()
        for r in out:
            r['aliases'] = json.loads(r.pop('aliases_json'))
            age = None if r['last_seen_at'] is None else t - r['last_seen_at']
            # A clock now behind the capture time (rolled back) cannot date the observation: age unknown, never fresh, flagged.
            r['clock_behind'] = age is not None and age < 0
            obs=self.one('SELECT payload FROM observation WHERE id=?',(r['last_observation_id'],)) if r['last_observation_id'] else None
            try:r['evidence_class']=json.loads(obs['payload']).get('mode','LEGACY_UNKNOWN') if obs else 'UNKNOWN'
            except (TypeError,ValueError):r['evidence_class']='UNKNOWN'
            if r['clock_behind'] or (getattr(self,'runtime_mode',None)=='PHYSICAL' and r['evidence_class']!='PHYSICAL'): age=None
            r['age_s'] = age
            r['fresh'] = age is not None and age <= self.policy.freshness_s
            r['id'] = r['object_id']
            r['tag'] = r['tag_id']
            r['freshness'] = max(0.0, 1.0 - age/self.policy.freshness_s) if age is not None else 0.0
            r['statement'] = (f"{r['name']}: location unknown" if not r['last_zone'] else
                              f"{r['name']} {'is' if r['fresh'] else 'was last observed'} at {r['last_zone']}"
                              +(' (observation dated after the current clock; age unknown)' if r['clock_behind'] else ''))
        return out

    def resolve_objects(self, phrase: str) -> list[str]:
        p = ' ' + normalize(phrase) + ' '
        found = []
        for o in self.objects():
            names = [o['id'], o['name']] + o['aliases']
            if any(' '+normalize(n)+' ' in p for n in names):
                found.append(o['id'])
        return found

    def zones_named(self, phrase: str) -> list[str]:
        p = ' ' + normalize(phrase) + ' '
        return [z['id'] for z in self.rows('SELECT * FROM cx_station')
                if any(' '+normalize(n)+' ' in p for n in [z['id'], z['name']] + json.loads(z['aliases_json']))]

    def resolve_targets(self, phrase: str) -> list[str]:
        """Objects a request names as targets, each unambiguously: longer names consume their words first, and a name
        several objects share (an alias collision) is refused rather than read as all of them."""
        owners = {}
        for o in self.objects():
            for n in [o['id'], o['name']] + o['aliases']:
                if normalize(n): owners.setdefault(normalize(n), set()).add(o['id'])
        rest, found = ' ' + normalize(phrase) + ' ', set()
        for n in sorted(owners, key=len, reverse=True):
            if ' ' + n + ' ' in rest:
                if len(owners[n]) > 1: raise Missing(f'"{n}" names more than one registered object; name the one you mean')
                rest = rest.replace(' ' + n + ' ', ' | '); found |= owners[n]
        return [o['id'] for o in self.objects() if o['id'] in found]

    def named(self, phrase: str) -> list[str]:
        """Registered objects and stations a phrase names: the scope an unresolved statement blocks."""
        return sorted(set(self.resolve_objects(phrase)) | set(self.zones_named(phrase)))

    def resolve_zone(self, phrase: str) -> str | None:
        hits = self.zones_named(phrase)
        if len(hits) > 1:
            raise Missing('more than one destination; name one')
        return hits[0] if hits else None

    def observe(self, object_id: str, zone: str, *, origin: str, mode: str,
                captured_at: float | None = None, evidence: str | None = None) -> str:
        text(origin, 'observation origin', 200)
        if mode not in {'PHYSICAL', 'SIMULATED', 'OPERATOR'}:
            raise CortexError('observation needs an explicit evidence class')
        if not any(o['id'] == object_id for o in self.objects()) or zone not in self.zones():
            raise Missing('unknown observed object or station')
        at = self.clock() if captured_at is None else finite(captured_at, 'capture time')
        if at > self.clock() + 1:
            raise CortexError('observation timestamp is in the future')
        oid = uid('obs')
        with self.tx():
            self.conn.execute('INSERT INTO observation(id,kind,payload,observed_at) VALUES(?,?,?,?)',
                              (oid, 'tag_seen', canonical({'object_id': object_id, 'zone': zone, 'origin': origin,
                               'mode': mode, 'evidence': evidence}), at))
            # Out-of-order delivery cannot replace the newer observation.
            self.conn.execute('UPDATE object_binding SET last_zone=?,last_seen_at=?,last_observation_id=? '
                              'WHERE object_id=? AND (last_seen_at IS NULL OR last_seen_at<=?)',
                              (zone, at, oid, object_id, at))
        return oid

    def _mask(self, phrase: str) -> str:
        """The phrase with every registered object and station name replaced by a neutral word, so that wording checks
        never read a name ("Safety goggles", "Danger zone") as a hazard or condition."""
        names = sorted({normalize(n) for o in self.objects() for n in [o['id'], o['name']] + o['aliases']} |
                       {normalize(n) for z in self.rows('SELECT * FROM cx_station') for n in [z['id'], z['name']] + json.loads(z['aliases_json'])}, key=len, reverse=True)
        out = ' ' + ' '.join(re.findall(r"[A-Za-z0-9']+", phrase)) + ' '
        for n in filter(None, names):
            out = re.sub(r'(?<![A-Za-z0-9])' + r'\s+'.join(map(re.escape, n.split())) + r'(?![A-Za-z0-9])', ' entity ', out, flags=re.I)
        return out

    @staticmethod
    def _handling(text: str) -> bool:
        """Whether a (masked) sentence speaks of handling an object: handling wording, a negated handling verb, or an
        imperative whose verb handles or places something ("Hold Sample A by the cap", "Put it on shelf B")."""
        m = IMPERATIVE_RE.match(text)
        return bool(HANDLE_RE.search(text) or NEG_HANDLE_RE.search(text) or (m and m['verb'].lower() in HAND_VERBS))

    def _reach(self, masked: str, named: list[str]) -> list[str]:
        """What an unresolved statement blocks: what it names, or every object when it speaks of every object at once
        ("nothing may be moved", "never lift anything"). Naming nothing, an instruction about the robot's own motion
        ("never drive", "the robot must not travel") blocks every plan until it is reviewed: what moves is the robot, so
        there is nothing for it to name. Otherwise, naming nothing, it blocks everything only as an instruction about
        handling or motion that may refer to any object ("carry it upright", "it must never be handled"); any other
        statement blocks nothing, so a remark ("lunch is served daily except Sunday") never freezes the robot. A
        statement about handling alone blocks only plans that handle what it covers ('@handle'): pick, carry, place."""
        handling, m = self._handling(masked), IMPERATIVE_RE.match(masked)
        motion = bool(MOTION_RE.search(masked) or (m and m['verb'].lower() in GO_VERBS))
        instruction = bool(SAFETY_RE.search(masked) or MODAL_RE.search(masked) or NEG_HANDLE_RE.search(masked) or (m and m['verb'].lower() in HAND_VERBS | GO_VERBS))
        if named: reach = list(named)
        elif instruction and LOCOMOTION_RE.search(masked): reach = ['*']
        elif (handling or motion) and (EVERYTHING_RE.search(masked) or (instruction and PRONOUN_RE.search(masked))): reach = ['*']
        else: return []
        return reach + (['@handle'] if handling and not motion else [])

    def _template_rule(self, m) -> tuple | None:
        """(subject, prerequisite, station) of one prerequisite-template match, when each resolves to exactly one."""
        pre, subject = self.resolve_objects(m['pre']), self.resolve_objects(m['subject'])
        try:
            zone = self.resolve_zone(m['zone'])
        except Missing:
            zone = None
        return (subject[0], pre[0], zone) if len(pre) == len(subject) == 1 and zone else None

    def _extract(self, body: str) -> tuple[list[dict], list[str]]:
        found, unknown = [], []
        for m in RULE_RE.finditer(body):
            key = self._template_rule(m)
            if key:
                found.append({'subject': key[0], 'prerequisite': key[1], 'required_zone': key[2]})
            else:
                unknown.append(m.group(0))
        return found, unknown

    def teach(self, title: str, body: str, *, owner: str, kind: str = 'document',
              rules: list[dict] | None = None, supersedes: str | None = None,
              cloud_ok: bool = False, goal_definitions: dict | None = None, prohibitions: list[str] | None = None, facts: list[dict] | None = None) -> dict:
        title, body, owner = text(title, 'title', 200), text(body, 'source', 200000), text(owner, 'source owner', 100)
        # A commitment is its owner's request, never shared knowledge: no rule, prohibition, fact, task alias or
        # unresolved constraint comes from its text (its wake is parsed, planned and refused like any other request).
        commitment = kind == 'future'
        if commitment and (rules or prohibitions or facts or goal_definitions or supersedes):
            raise CortexError('a commitment holds only its own request text')
        sentences = [] if commitment else [s.replace(chr(0x2019), "'") for s in re.split(r'[.\n]', body)]
        extracted, unresolved = (rules, []) if rules is not None else ([], [])
        if rules is None:
            for s in sentences:
                if CONDITIONAL_RE.search(s): continue      # no template rule from a conditional sentence: unresolved below
                found, unknown = self._extract(s); extracted += found; unresolved += unknown
        if not isinstance(extracted, list) or len(extracted) > 64:
            raise CortexError('too many rules')
        known = {o['id'] for o in self.objects()}
        checked = []
        for r in extracted:
            if not isinstance(r, dict) or set(r) - {'subject','prerequisite','required_zone','rule_key'}:
                raise CortexError('invalid prerequisite rule shape')
            if r.get('subject') not in known or r.get('prerequisite') not in known or r.get('required_zone') not in self.zones():
                raise Missing('rule refers to an unregistered object or station')
            if r['subject'] == r['prerequisite']:
                raise Conflict('self-dependent rule')
            checked.append({**r, 'rule_key': r.get('rule_key') or f"{r['subject']}::requires::{r['prerequisite']}"})
        forbidden, pending = list(prohibitions or []), []
        # Each sentence is accounted for by its meaning, not by the entities it names. What is stored accounts for its
        # own wording only: a prerequisite-template clause whose rule is stored as written (a reviewer's rule or an
        # extracted one, its clause saying nothing else), a prohibition whose objects are prohibited, or a handling or
        # hazard sentence about reviewer-prohibited objects alone. A conditional is never resolved: no structured form
        # expresses its exception. Whatever constraint wording remains (conditional, hazard or permission, deontic, or
        # handling) is an unresolved constraint on what its OWN sentence names (Store._reach); anything else is a fact.
        stored = {(r.get('subject'), r.get('prerequisite'), r.get('required_zone')) for r in extracted if isinstance(r, dict)}
        banned = set(forbidden)
        quote = lambda s: s.strip()[:300]
        for sent in sentences:
            named = self.named(sent)                      # its own names only
            masked = self._mask(sent)                     # registered names never count as wording ("Safety goggles")
            residual = sent
            for m in RULE_RE.finditer(sent):
                extra = set(self._mask(' '.join((m['pre'], m['zone'], m['subject']))).split()) - {'entity','the','a','an'}
                if self._template_rule(m) in stored and not extra:   # the clause says no more than its stored rule
                    residual = residual.replace(m.group(0), ' ')
            if PROHIBIT_RE.search(sent):
                objs = self.resolve_objects(sent); forbidden.extend(objs)
                if objs: residual = PROHIBIT_RE.sub(' ', residual)
            elif (self._handling(masked) or HAZARD_RE.search(masked)) and named and set(named) <= banned and not CONDITIONAL_RE.search(masked):
                residual = ''                             # a reviewer's total ban on exactly these objects covers it
            rest = self._mask(residual)
            if CONDITIONAL_RE.search(masked): why = 'Conditional exception'                  # never resolved
            elif TEMPORAL_RE.search(rest) and (self._handling(masked) or MOTION_RE.search(masked)): why = 'Condition on motion or handling'
            elif HAZARD_RE.search(rest): why = 'Unparsed hazard or permission'
            elif SAFETY_RE.search(rest) or PROHIBIT_RE.search(residual): why = 'Unparsed safety statement'
            elif self._handling(rest): why = 'Unparsed handling instruction or limitation'
            else: continue                                # a fact: stored and searchable, blocking nothing
            reach = self._reach(masked, named)
            pending.append((why+'; structured review required'+('' if reach else ' (it names no object, so it blocks nothing; restate it naming one)')
                            +': '+quote(sent), reach))
        if len(forbidden)>32 or not set(forbidden)<=known:
            raise Missing('prohibition needs registered objects')
        facts=facts or []
        if not isinstance(facts,list) or len(facts)>32: raise CortexError('invalid generic fact list')
        for f in facts:
            if not isinstance(f,dict) or set(f)!={'subject','predicate','object'}: raise CortexError('generic facts need subject, predicate, object')
            for k,v in f.items(): text(v,k,500)
        defs = goal_definitions or {}
        if not isinstance(defs, dict) or len(defs) > 20:
            raise CortexError('invalid task definitions')
        for n, g in defs.items():
            text(n, 'task name', 100)
            alias = Goal.from_dict(g)
            if alias.kind == 'primitive' and alias.primitive in CONTROL_OPS:
                raise Denied(f'a task definition cannot be the {alias.primitive} control: STOP has its own path, never a task')
        sid, cid, t = uid('src'), uid('chk'), self.clock()
        # A source may add a constraint or retire the one it revises; a commitment adds none (and is scheduled from inside
        # the caller's transaction).
        with (self.tx() if commitment else self.authority_tx()):
            rev = 1
            if supersedes:
                prior = self.source_state(supersedes)
                if prior['status'] != 'accepted':
                    raise Conflict('only an accepted source can be explicitly revised')
                rev = prior['revision'] + 1
                self.conn.execute("UPDATE cx_source SET status='superseded' WHERE source_id=?", (supersedes,))
                self.conn.execute("UPDATE cx_rule SET status='superseded' WHERE assertion_id IN (SELECT id FROM assertion WHERE source_id=?)", (supersedes,))
            self.conn.execute('INSERT INTO source VALUES(?,?,?,?,?)', (sid, title, kind, owner, t))
            self.conn.execute('INSERT INTO source_chunk VALUES(?,?,?,?,?,?)', (cid, sid, None, body, 0, len(body)))
            self.conn.execute('INSERT INTO source_chunk_fts(text,section,chunk_id) VALUES(?,?,?)', (body, '', cid))
            self.conn.execute('INSERT INTO cx_source VALUES(?,?,?,?,?,?,?)',
                              (sid, self.workspace, 'accepted', rev, supersedes, hashlib.sha256(body.encode()).hexdigest(), int(cloud_ok)))
            for reason, mentions in pending:
                self.conn.execute('INSERT INTO cx_unresolved VALUES(?,?,?)',(sid,canonical(sorted(mentions)),reason[:1000]))
            if supersedes:
                self.conn.execute("UPDATE cx_constraint SET status='superseded' WHERE assertion_id IN (SELECT id FROM assertion WHERE source_id=?)", (supersedes,))
            for obj in sorted(set(forbidden)):
                aid = uid('asr')
                self.conn.execute('INSERT INTO assertion(id,subject,predicate,object,source_id,chunk_id,created_at) VALUES(?,?,?,?,?,?,?)',
                                  (aid,obj,'forbidden_to_move','true',sid,cid,t))
                self.conn.execute('INSERT INTO cx_constraint VALUES(?,?,?)',(aid,'move','active'))
            if supersedes:
                self.conn.execute("UPDATE cx_fact SET status='superseded' WHERE assertion_id IN (SELECT id FROM assertion WHERE source_id=?)",(supersedes,))
            for f in facts:
                aid=uid('asr'); fk=digest({'subject':f['subject'],'predicate':f['predicate']})
                old=self.rows("SELECT a.object FROM assertion a JOIN cx_fact c ON c.assertion_id=a.id WHERE c.fact_key=? AND c.status IN ('active','disputed')",(fk,))
                conflict=any(x['object']!=f['object'] for x in old)
                self.conn.execute('INSERT INTO assertion(id,subject,predicate,object,source_id,chunk_id,created_at) VALUES(?,?,?,?,?,?,?)',(aid,f['subject'],f['predicate'],f['object'],sid,cid,t))
                self.conn.execute('INSERT INTO cx_fact VALUES(?,?,?)',(aid,fk,'disputed' if conflict else 'active'))
                if conflict:self.conn.execute("UPDATE cx_fact SET status='disputed' WHERE fact_key=? AND status='active'",(fk,))
            added = []
            for r in checked:
                prior_rules = self.rows("SELECT a.id,a.object,r.required_zone FROM assertion a JOIN cx_rule r ON r.assertion_id=a.id "
                                       "WHERE r.rule_key=? AND r.status IN ('active','disputed')", (r['rule_key'],))
                conflict = any(x['object'] != r['prerequisite'] or x['required_zone'] != r['required_zone'] for x in prior_rules)
                aid = uid('asr')
                self.conn.execute('INSERT INTO assertion(id,subject,predicate,object,source_id,chunk_id,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)',
                                  (aid, r['subject'], 'requires_before', r['prerequisite'], sid, cid, 1.0, t))
                self.conn.execute('INSERT INTO cx_rule VALUES(?,?,?,?)',
                                  (aid, r['rule_key'], r['required_zone'], 'disputed' if conflict else 'active'))
                if conflict:
                    self.conn.execute("UPDATE cx_rule SET status='disputed' WHERE rule_key=? AND status='active'", (r['rule_key'],))
                added.append(aid)
            for name, goal in defs.items():
                old = self.one('SELECT * FROM cx_goal WHERE name=?', (normalize(name),))
                if old and old['source_id'] != supersedes:
                    raise Conflict('task alias already exists; explicitly revise its source')
                self.conn.execute('INSERT INTO cx_goal VALUES(?,?,?,1) ON CONFLICT(name) DO UPDATE SET goal_json=excluded.goal_json,source_id=excluded.source_id,version=version+1',
                                  (normalize(name), canonical(goal), sid))
            self._bump()
            self.invalidate()
        return {'source_id': sid, 'chunk_id': cid, 'revision': rev, 'rules': added,
                'unresolved_rules': [reason for reason, _ in pending], 'kind': 'taught'}

    def source_state(self, sid: str) -> dict:
        r = self.one('SELECT s.*,c.* FROM source s JOIN cx_source c ON c.source_id=s.id WHERE s.id=? AND c.workspace=?', (sid, self.workspace))
        if not r:
            raise Missing('source is absent or outside this workspace; legacy sources require explicit adoption')
        return r

    def adopt_legacy_source(self, sid: str, *, owner: str) -> dict:
        """Explicitly re-ingest a reviewed legacy source; never auto-trust old content."""
        text(owner, 'reviewer', 100)
        s = self.one('SELECT * FROM source WHERE id=?', (sid,))
        if not s:
            raise Missing('no such legacy source')
        if self.one('SELECT 1 FROM cx_source WHERE source_id=?', (sid,)):      # a commitment's text never becomes shared
            raise Conflict('not a legacy source: Cortex already holds it (revise it with supersedes)')
        parts = self.rows('SELECT text FROM source_chunk WHERE source_id=? ORDER BY start_char', (sid,))
        return self.teach(s['title'], '\n\n'.join(p['text'] for p in parts), owner=owner, kind='adopted_legacy')

    def revoke_source(self, sid: str, *, delete: bool = False) -> None:
        self.source_state(sid)
        with self.authority_tx():
            self.conn.execute('UPDATE cx_source SET status=? WHERE source_id=?', ('deleted' if delete else 'revoked', sid))
            self.conn.execute("UPDATE cx_rule SET status='revoked' WHERE assertion_id IN (SELECT id FROM assertion WHERE source_id=?)", (sid,))
            if delete:
                for r in self.rows('SELECT id FROM source_chunk WHERE source_id=?', (sid,)):
                    self.conn.execute('DELETE FROM source_chunk_fts WHERE chunk_id=?', (r['id'],))
                self.conn.execute("UPDATE source_chunk SET text='[deleted]',end_char=start_char WHERE source_id=?", (sid,))
                # Its title and a reviewer's own rule keys are its text too (a revoked rule key is matched by no rule).
                self.conn.execute("UPDATE source SET title='[deleted]' WHERE id=?", (sid,))
                self.conn.execute("UPDATE cx_rule SET rule_key='[deleted]' WHERE assertion_id IN (SELECT id FROM assertion WHERE source_id=?)", (sid,))
            self._bump()
            # A removed instruction cannot wake a pending commitment later; one whose effect started keeps its real
            # outcome. Withdrawn first, so the invalidation below (which settles commitments too) finds it cancelled.
            for t in self.rows("SELECT id FROM prospective_task WHERE source_id=? AND status IN ('waiting','fired')", (sid,)):
                self.withdraw_commitment(t['id'], 'source withdrawn')
            self.invalidate()
            if delete:
                # Remove retained source-text copies from public memory projections.
                for d in self.rows("SELECT id,refs_json FROM cx_derived WHERE status='STALE'"):
                    if not self.refs_valid(json.loads(d['refs_json'])):
                        self.conn.execute("UPDATE cx_derived SET payload_json='{\"redacted\":true}',status='DELETED' WHERE id=?",(d['id'],))
                for e in self.rows('SELECT id,evidence_json FROM episode WHERE evidence_json IS NOT NULL'):
                    meta=json.loads(e['evidence_json'])
                    if sid in canonical(meta.get('bundle',{})):        # anywhere in the bundle, not only its evidence list
                        meta['bundle']={'redacted':True,'reason':'source removed'}
                        self.conn.execute('UPDATE episode SET evidence_json=? WHERE id=?',(canonical(meta),e['id']))
                for k in self.rows("SELECT id,definition_json FROM skill_version WHERE status='REVOKED'"):
                    definition=json.loads(k['definition_json'])
                    if 'source:'+sid in definition.get('refs',[]):
                        self.conn.execute("UPDATE skill_version SET definition_json='{\"redacted\":true}' WHERE id=?",(k['id'],))
            if delete:
                # Every other copy of the text goes too: unresolved reasons, declared facts, task aliases, the commitment's
                # trigger and request, the events that woke it and a failed wake's error text, and the episodes built from it.
                aliases = [Goal.from_dict(json.loads(r['goal_json'])).to_dict() for r in self.rows('SELECT goal_json FROM cx_goal WHERE source_id=?', (sid,))]
                tasks = {r['id'] for r in self.rows('SELECT id FROM prospective_task WHERE source_id=?', (sid,))}
                self.conn.execute('DELETE FROM cx_unresolved WHERE source_id=?', (sid,))
                self.conn.execute("UPDATE assertion SET subject='[deleted]',predicate='[deleted]',object='[deleted]' WHERE source_id=? AND id IN (SELECT assertion_id FROM cx_fact)", (sid,))
                self.conn.execute('DELETE FROM cx_goal WHERE source_id=?', (sid,))
                self.conn.execute("UPDATE cx_event SET trigger='[deleted]' WHERE id IN (SELECT w.event_id FROM cx_wake w JOIN prospective_task p ON p.id=w.task_id WHERE p.source_id=?)", (sid,))
                self.conn.execute("UPDATE cx_wake SET error=NULL WHERE task_id IN (SELECT id FROM prospective_task WHERE source_id=?)", (sid,))
                self.conn.execute("UPDATE prospective_task SET trigger='[deleted]',goal='[deleted]',request='[deleted]' WHERE source_id=?", (sid,))
                self._purge_episodes(sid, aliases, tasks)
        if delete:
            # ...and no residue stays on disk: full-text segments rebuilt without it, freed pages zeroed (secure_delete),
            # the write-ahead log checkpointed and truncated.
            with self.tx():
                self.conn.execute("INSERT INTO source_chunk_fts(source_chunk_fts) VALUES('optimize')")
                self.conn.execute("INSERT INTO cx_episode_fts(cx_episode_fts) VALUES('optimize')")
            if self.path != ':memory:' and self._depth == 0: self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def _purge_episodes(self, sid: str, aliases: list[dict], tasks: set[str]) -> None:
        """Caller holds the transaction. The episodes built from a deleted source: those it was recorded as the origin of
        (a task alias or commitment request, planned or not), plans citing it, a goal with its free-text arguments, the
        wakes of its commitments. Each keeps its outcome as an audit record
        but loses every copy of the source: request (a commitment's text, a task alias's name) and its search row, plan,
        evidence bundle, the step arguments journalled at execution and every free-text field recorded about it (failure
        reason, adapter replies, operator notes), a goal's free-text arguments, and the idle memories made from it
        (summaries, practice, procedures)."""
        redacted, gone, goals = canonical({'redacted': True}), set(), []
        linked = {r['episode_id'] for r in self.rows('SELECT episode_id FROM cx_episode_source WHERE source_id=?', (sid,))}
        for e in self.rows('SELECT e.id,e.plan_json,c.goal_json,c.request_key FROM episode e JOIN cx_episode c ON c.id=e.id'):
            key, goal = e['request_key'] or '', json.loads(e['goal_json'])
            woken = ':wake:' in key and key.rsplit(':wake:', 1)[1].rsplit(':', 1)[0] in tasks
            refs = json.loads(e['plan_json']).get('refs', []) if e['plan_json'] else []
            if not (woken or e['id'] in linked or 'source:'+sid in refs or (goal.get('args') and goal in aliases)): continue
            gone.add('episode:'+e['id'])
            if goal.get('args'): goals.append(goal); goal = {**goal, 'args': {'redacted': True}}
            # Free text recorded about it may quote the source too: a failure reason, an adapter's reply (echoing the text
            # it was told to speak), an operator's note. Its outcome stays; the words go.
            self.conn.execute("UPDATE episode SET goal=?,request='[deleted]',plan_json=NULL,evidence_json=NULL,"
                              "failure=CASE WHEN failure IS NULL THEN NULL ELSE '[deleted]' END WHERE id=?", (canonical(goal), e['id']))
            self.settle(e['id'], 'invalidated', ('preparing','proposed'), 'source deleted')    # its plan is gone: never approvable
            self.conn.execute('DELETE FROM cx_episode_fts WHERE episode_id=?', (e['id'],))
            self.conn.execute('UPDATE cx_episode SET goal_json=? WHERE id=?', (canonical(goal), e['id']))
            # The journal keeps each step's operation and the runtime's own status words; an adapter's own words go.
            self.conn.execute("UPDATE operation_event SET args_json=?,detail=CASE WHEN detail='' THEN '' ELSE '[deleted]' END,"
                              f"status=CASE WHEN status IN ({','.join('?'*len(JOURNAL_STATES))}) THEN status ELSE '[deleted]' END WHERE episode_id=?",
                              (redacted, *JOURNAL_STATES, e['id']))
        for d in self.rows('SELECT id,refs_json FROM cx_derived'):
            if gone & set(json.loads(d['refs_json'])):     # a fresh fingerprint: a purged memory never stands in for a new one
                self.conn.execute("UPDATE cx_derived SET payload_json=?,status='DELETED',fingerprint=? WHERE id=?", (redacted, 'deleted:'+d['id'], d['id']))
        for k in self.rows('SELECT id,definition_json FROM skill_version'):
            if gone & set(json.loads(k['definition_json']).get('refs', [])):
                self.conn.execute("UPDATE skill_version SET status='REVOKED',definition_json=? WHERE id=?", (redacted, k['id']))
        for c in self.rows('SELECT id,case_json FROM cx_practice'):
            if goals and json.loads(c['case_json']).get('goal') in goals:
                self.conn.execute('UPDATE cx_practice SET case_json=?,expected_json=?,actual_json=? WHERE id=?', (redacted, redacted, redacted, c['id']))

    def settle(self, ep: str, outcome: str, allowed: tuple, failure: str | None = None) -> bool:
        """The one guarded episode transition; the linked commitment follows in the same transaction."""
        with self.tx():
            n = self.conn.execute(f"UPDATE episode SET outcome=?,failure=?,finished_at=? WHERE id=? AND outcome IN ({','.join('?'*len(allowed))}) "
                                  "AND id IN (SELECT id FROM cx_episode)", (outcome, failure, self.clock(), ep, *allowed)).rowcount
            if n: self.finish_task(ep, outcome)
        return n == 1

    def finish_task(self, ep: str, outcome: str) -> None:
        if outcome in TASK_STATE:
            with self.tx():
                self.conn.execute("UPDATE prospective_task SET status=? WHERE proposal_id=? AND status NOT IN ('cancelled','done')", (TASK_STATE[outcome], ep))

    def withdraw_commitment(self, tid: str, reason: str) -> bool:
        """Cancel a commitment, its unstarted proposal and pending wakes; False if its effect may have started."""
        with self.tx():
            r = self.one('SELECT p.proposal_id,e.outcome FROM prospective_task p LEFT JOIN episode e ON e.id=p.proposal_id WHERE p.id=?', (tid,))
            if not r: raise Missing('task not found')
            if r['outcome'] in STARTED: return False
            if r['proposal_id']: self.settle(r['proposal_id'], 'cancelled', ('preparing','proposed'), reason)
            self.conn.execute("UPDATE prospective_task SET status='cancelled' WHERE id=?", (tid,))
            self.conn.execute('UPDATE cx_commitment SET generation=generation+1 WHERE id=?', (tid,))
            self.conn.execute("UPDATE cx_wake SET status='cancelled' WHERE task_id=? AND status IN ('pending','preparing')", (tid,))
        return True

    def rules(self) -> list[dict]:
        return self.rows("SELECT a.*,r.rule_key,r.required_zone,r.status,c.revision FROM assertion a JOIN cx_rule r ON r.assertion_id=a.id "
                         "JOIN cx_source c ON c.source_id=a.source_id WHERE c.workspace=? AND c.status='accepted' AND r.status IN ('active','disputed') ORDER BY r.rule_key,a.id", (self.workspace,))

    def related_rules(self, goal: Goal, held: frozenset = frozenset()) -> list[dict]:
        """The prerequisite closure of what a goal handles: its targets, and the object in the claw (`held`) for a claw
        primitive that grips it (Compiler.held)."""
        frontier, seen, relevant = list(goal.targets) + sorted(held), set(), []
        rules = self.rules()
        while frontier:
            obj = frontier.pop()
            if obj in seen:
                continue
            seen.add(obj)
            if len(seen) > self.policy.max_nodes:
                raise Missing('prerequisite graph exceeds bounded retrieval')
            for r in rules:
                if r['subject'] == obj:
                    relevant.append(r)
                    frontier.append(r['object'])
        return relevant

    def unresolved_for(self,ids: set[str],*,handles: bool=True) -> list[dict]:
        """Unresolved statements that block a plan involving these ids ('*': every plan). One about handling alone
        ('@handle') blocks only a plan that handles them: handles=False (no pick or place, e.g. goto) passes it."""
        rows=self.rows("SELECT u.* FROM cx_unresolved u JOIN cx_source s ON s.source_id=u.source_id WHERE s.status='accepted' AND s.workspace=?",(self.workspace,))
        return [r for r in rows if (m := set(json.loads(r['mentions_json']))) and ('*' in m or ids & m) and (handles or '@handle' not in m)]

    @staticmethod
    def scope(goal: Goal, rules: list[dict]) -> set[str]:
        """Objects and stations a goal involves: what an unresolved statement naming any of them blocks."""
        return set(goal.targets) | {r[k] for r in rules for k in ('subject','object','required_zone')} | ({goal.zone} if goal.zone else set())

    def generic_facts(self, query: str='') -> list[dict]:
        rows=self.rows("SELECT a.*,f.status FROM assertion a JOIN cx_fact f ON f.assertion_id=a.id JOIN cx_source s ON s.source_id=a.source_id WHERE s.workspace=? AND s.status='accepted' AND f.status IN ('active','disputed') ORDER BY a.created_at DESC",(self.workspace,))
        words=set(re.findall(r'[a-z0-9]+',normalize(query)))
        return [{**r,'evidence_class':'source_declared_not_sensor_truth'} for r in rows if not words or words & set(re.findall(r'[a-z0-9]+',normalize(r['subject']+' '+r['predicate']+' '+r['object'])))][:32]

    def goal_alias(self, request: str) -> Goal | None:
        name = normalize(request)
        row = self.one("SELECT g.* FROM cx_goal g JOIN cx_source s ON g.source_id=s.source_id WHERE g.name=? AND s.workspace=? AND s.status='accepted'", (name, self.workspace))
        return Goal.from_dict(json.loads(row['goal_json'])) if row else None

    def basis(self, goal: Goal, held: frozenset = frozenset()) -> str:
        """Everything a plan for this goal rests on; with `held`, the held object's entity, rules and constraints too."""
        rules = self.related_rules(goal, held)
        ids = set(goal.targets) | set(held) | {r['subject'] for r in rules} | {r['object'] for r in rules}
        entities = [{k:o[k] for k in ('id','name','tag','graspable','version')} for o in self.objects() if o['id'] in ids]
        zones = self.rows('SELECT id,version FROM cx_station ORDER BY id')
        payload = {'goal':goal.to_dict(), 'rules':[{k:r[k] for k in ('id','rule_key','required_zone','object','subject','status')} for r in rules],
                   'entities':entities, 'zones':zones}
        payload['unresolved']=self.unresolved_for(self.scope(goal,rules)|set(held),handles=goal.kind=='fetch' or bool(held))
        payload['prohibitions'] = self.prohibitions(ids)
        if held: payload['held'] = sorted(held)
        return digest(payload)

    def evidence(self, chunk_id: str, *, history: bool = False, principal: str | None = None) -> dict:
        r = self.one('SELECT c.*,s.title,s.kind,s.origin,x.status,x.revision,x.cloud_ok FROM source_chunk c JOIN source s ON s.id=c.source_id '
                     'JOIN cx_source x ON x.source_id=s.id WHERE c.id=? AND x.workspace=?', (chunk_id, self.workspace))
        if not r or r['status'] in {'deleted','revoked'} or (not history and r['status'] != 'accepted'):
            raise Missing('evidence is unavailable, revoked or superseded')
        if r['kind'] == 'future' and (principal is None or r['origin'] != principal):
            raise Missing('evidence is unavailable, revoked or superseded')    # a commitment is its owner's alone
        return r

    def search(self, query: str, limit: int = 5) -> list[dict]:
        tokens = re.findall(r'[A-Za-z0-9]+', text(query, 'query', 2000))[:40]
        if not tokens:
            return []
        q = ' OR '.join('"'+t+'"' for t in tokens)
        return self.rows("SELECT c.id chunk_id,c.source_id,c.text,c.start_char,c.end_char,s.title,x.revision,x.cloud_ok,bm25(source_chunk_fts) score "
                         "FROM source_chunk_fts f JOIN source_chunk c ON c.id=f.chunk_id JOIN source s ON s.id=c.source_id "
                         "JOIN cx_source x ON x.source_id=s.id WHERE source_chunk_fts MATCH ? AND x.workspace=? AND x.status='accepted' AND s.kind!='future' ORDER BY score,c.id LIMIT ?",
                         (q, self.workspace, min(max(int(limit),1),20)))

    def bundle(self, request: str, goal: Goal | None = None, *, principal: str | None = None, held: frozenset = frozenset()) -> dict:
        rules = self.related_rules(goal, held) if goal else []
        ids = set(goal.targets if goal else self.resolve_objects(request)) | set(held) | {r['object'] for r in rules}
        objects = [o for o in self.objects() if o['id'] in ids]
        mandatory_ids = list(dict.fromkeys([r['chunk_id'] for r in rules]+[r['chunk_id'] for r in self.prohibitions(ids)]))
        evidence = [self.evidence(cid) for cid in mandatory_ids]
        missing = [f"current location of {o['id']}" for o in objects if not o['fresh']]
        disputed = [r['rule_key'] for r in rules if r['status']=='disputed']
        if disputed:
            missing += ['unresolved conflicting rule: '+k for k in sorted(set(disputed))]
        facts = [{'id':r['id'], 'subject':r['subject'], 'predicate':'requires_at_before', 'object':r['object'],
                  'required_zone':r['required_zone'], 'chunk_id':r['chunk_id'], 'source_id':r['source_id'], 'status':r['status']} for r in rules]
        b = {'goal':request, 'task':goal.to_dict() if goal else None, 'epoch':self.epoch(),
             'basis': self.basis(goal, held) if goal else None, 'facts':facts, 'objects':objects, 'constraints':facts,
             'evidence':evidence, 'missing':missing, 'omitted':[], 'applicable_skills':[], 'derived':[]}
        b['unresolved_constraints']=self.unresolved_for(self.scope(goal,rules)|set(held) if goal else ids|set(self.zones_named(request)),
                                                        handles=not goal or goal.kind=='fetch' or bool(held))
        b['prohibitions'] = self.prohibitions(ids)
        # Optional declared facts never override sensor locations or authority.
        generic=self.generic_facts(request)[:8]
        b['declared_facts']=generic
        if len(canonical(b).encode()) > self.policy.max_bundle_bytes:
            raise Missing('required evidence exceeds context budget; inspect sources rather than drop a rule')
        for hit in self.search(request, 5):
            if hit['chunk_id'] in mandatory_ids:
                continue
            candidate = {**b, 'evidence':b['evidence']+[hit]}
            if len(canonical(candidate).encode()) <= self.policy.max_bundle_bytes:
                b = candidate
            else:
                b['omitted'].append(hit['chunk_id'])
        # Cross-memory retrieval: one bounded packet, not seven indiscriminate dumps.
        words=re.findall(r'[A-Za-z0-9]+',request)[:20]
        q=' OR '.join('"'+w+'"' for w in words)
        episodes=[]
        if q:
            episodes=self.rows('SELECT e.id,e.request,e.outcome,e.failure,c.mode FROM cx_episode_fts f JOIN episode e ON e.id=f.episode_id JOIN cx_episode c ON c.id=e.id WHERE cx_episode_fts MATCH ? AND c.owner=? ORDER BY bm25(cx_episode_fts) LIMIT 3',(q,principal))
        prospective=self.rows("SELECT p.id,p.request,p.trigger,p.status FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.status IN ('waiting','fired','unknown') AND c.owner=? ORDER BY p.created_at DESC LIMIT 3",(principal,))
        approved=self.approved_skills(principal,goal_key=goal.key() if goal else '',limit=3,fields=('id','name','digest'))
        related_derived=[]
        for d in self.derived(limit=32):
            if d['status']=='VERIFIED' and self.refs_valid(d['refs']) and (not goal or d['basis_hash']==self.basis(goal, held)) and self.visible(d['refs'],principal):
                related_derived.append({'id':d['id'],'kind':d['kind'],'payload':d['payload'],'refs':d['refs']})
        optional={'related_episodes':episodes,'prospective':prospective,'applicable_skills':approved,'derived':related_derived[:3],
                  'parametric':{'knowledge_is_evidence':False,'weight_updates':False}}
        for name,value in optional.items():
            trial={**b,name:value}
            if len(canonical(trial).encode())<=self.policy.max_bundle_bytes: b=trial
            else: b['omitted'].append(name)
        return b

    def approved_skills(self, principal: str | None, *, goal_key: str | None = None, limit: int = 10, fields: tuple = ('id','name')) -> list[dict]:
        """Approved procedures this principal may see (Store.visible on their definition refs), newest first: one built
        on another operator's commitment or episode is that operator's alone."""
        out = []
        for r in self.rows("SELECT v.*,c.goal_key FROM skill_version v JOIN cx_skill c ON c.id=v.id WHERE v.status='APPROVED' AND (? IS NULL OR c.goal_key=?) "
                           "ORDER BY v.approved_at DESC", (goal_key, goal_key)):
            if len(out) >= limit: break
            if self.visible(json.loads(r['definition_json']).get('refs', []), principal): out.append({k: r[k] for k in fields})
        return out

    def visible(self, refs: list[str], principal: str | None, _seen: frozenset = frozenset()) -> bool:
        """Whether a memory may be shown to this principal. One citing an episode or a commitment (a 'future' source) is
        shown to their owner only, directly or through a derived memory it cites: its payload may quote that operator's
        request. With no principal it is shown to nobody: no owner means no private rows, never all of them."""
        for ref in refs:
            kind, _, rid = ref.partition(':')
            if kind == 'episode':
                o = self.one('SELECT owner FROM cx_episode WHERE id=?', (rid,))
                if principal is None or not o or o['owner'] != principal: return False
            elif kind == 'source':
                s = self.one('SELECT kind,origin FROM source WHERE id=?', (rid,))
                if s and s['kind'] == 'future' and (principal is None or s['origin'] != principal): return False
            elif kind == 'derived' and ref not in _seen and len(_seen) <= self.policy.max_nodes:
                d = self.one('SELECT refs_json FROM cx_derived WHERE id=?', (rid,))
                if d and not self.visible(json.loads(d['refs_json']), principal, _seen | {ref}): return False
        return True

    def prohibitions(self, object_ids=None):
        rs=self.rows("SELECT a.id,a.subject,a.source_id,a.chunk_id,c.effect FROM assertion a JOIN cx_constraint c ON c.assertion_id=a.id JOIN cx_source s ON s.source_id=a.source_id WHERE c.status='active' AND s.status='accepted' AND s.workspace=? ORDER BY a.id",(self.workspace,))
        return [r for r in rs if object_ids is None or r['subject'] in object_ids]

    def resolve_rule(self, assertion_id: str, *, principal: str, reason: str):
        text(principal,'reviewer',100); text(reason,'adjudication reason',1000)
        with self.authority_tx():
            r=next((x for x in self.rules() if x['id']==assertion_id),None)
            if not r: raise Missing('no current rule to resolve')
            self.conn.execute("UPDATE cx_rule SET status='overridden' WHERE rule_key=? AND status IN ('active','disputed')",(r['rule_key'],))
            self.conn.execute("UPDATE cx_rule SET status='active' WHERE assertion_id=?",(assertion_id,))
            self.conn.execute('INSERT INTO observation(id,kind,payload,observed_at) VALUES(?,?,?,?)',
                              (uid('obs'),'operator_adjudication',canonical({'rule_id':assertion_id,'principal':principal,'reason':reason}),self.clock()))
            self._bump(); self.invalidate()

    def cloud_eligible(self, refs: list[str]) -> bool:
        if not refs: return False
        for ref in refs:
            if ref.startswith('source:'):
                r=self.one("SELECT cloud_ok,status FROM cx_source WHERE source_id=? AND workspace=?",(ref[7:],self.workspace))
                if not r or r['status']!='accepted' or not r['cloud_ok']: return False
            elif ref.startswith('rule:'):
                r=self.one('SELECT source_id FROM assertion WHERE id=?',(ref[5:],))
                if not r or not self.cloud_eligible(['source:'+r['source_id']]): return False
            else:
                # Episode text and private voice history have no implicit cloud consent.
                return False
        return True

    def refs_valid(self, refs: list[str], visited: set[str] | None = None) -> bool:
        visited = set() if visited is None else set(visited)
        if len(visited)>self.policy.max_nodes:
            return False
        for ref in refs:
            if not isinstance(ref, str) or ':' not in ref or ref in visited:
                return False
            kind, rid = ref.split(':',1)
            if kind=='source':
                r = self.one("SELECT 1 FROM cx_source WHERE source_id=? AND workspace=? AND status='accepted'", (rid,self.workspace))
            elif kind=='rule':
                r = self.one("SELECT 1 FROM cx_rule r JOIN assertion a ON a.id=r.assertion_id JOIN cx_source s ON s.source_id=a.source_id WHERE r.assertion_id=? AND r.status='active' AND s.workspace=? AND s.status='accepted'", (rid,self.workspace))
            elif kind=='episode':
                r = self.one('SELECT 1 FROM episode WHERE id=?', (rid,))
            elif kind=='derived':
                row = self.one("SELECT * FROM cx_derived WHERE id=? AND status='VERIFIED'", (rid,))
                r = row and self.refs_valid(json.loads(row['refs_json']), visited | {ref})
            else:
                return False
            if not r:
                return False
        return True

    def derive(self, kind: str, payload: dict, refs: list[str], *, origin: str,
               verified: bool = False, verifier: str | None = None, basis: str = '') -> dict:
        """Internal API. Models can call propose_candidate, not set verified=True."""
        if kind not in {'task_recipe','hypothesis','lesson','summary','relationship','practice_goal'}:
            raise CortexError('unknown derived-memory class')
        if not refs or len(refs)>self.policy.max_nodes or len(canonical(payload).encode())>32000:
            raise Missing('derived memory needs bounded source evidence')
        if not self.refs_valid(refs):
            raise Stale('derived memory evidence is no longer valid')
        if verified and verifier not in {'symbolic-verifier-v1','dependency-closure-v1','episodic-summary-v1'}:
            raise Denied('only named code verifiers may mark a derivation verified')
        fp = digest({'kind':kind,'payload':payload,'refs':sorted(set(refs)),'basis':basis})
        with self.tx():
            row = self.one('SELECT * FROM cx_derived WHERE fingerprint=?', (fp,))
            if row:
                return self.derived(row['id'])
            did = uid('mem')
            self.conn.execute('INSERT INTO cx_derived VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (did,kind,'VERIFIED' if verified else 'CANDIDATE',canonical(payload),canonical(sorted(set(refs))),basis,fp,origin,verifier,self.clock()))
        return self.derived(did)

    def derived(self, did: str | None = None, *, include_stale: bool = False, limit: int | None = None) -> dict | list[dict]:
        sql, args = 'SELECT * FROM cx_derived', ()
        if did:
            sql += ' WHERE id=?'; args=(did,)
        elif not include_stale:
            sql += " WHERE status IN ('CANDIDATE','VERIFIED')"
        if limit is None: rows = self.rows(sql+' ORDER BY created_at,id', args)
        else: rows = self.rows(sql+' ORDER BY created_at DESC,id DESC LIMIT ?', (*args, int(limit)))[::-1]   # the newest, oldest first
        for r in rows:
            r['payload']=json.loads(r.pop('payload_json')); r['refs']=json.loads(r.pop('refs_json'))
        if did:
            if not rows: raise Missing('derived memory not found')
            return rows[0]
        return rows

    def invalidate(self) -> None:
        # Caller may already hold tx. A fixed point propagates source withdrawal.
        with self.tx():
            for _ in range(self.policy.max_nodes):
                changed=0
                for r in self.rows("SELECT id,refs_json FROM cx_derived WHERE status IN ('VERIFIED','CANDIDATE')"):
                    if not self.refs_valid(json.loads(r['refs_json'])):
                        self.conn.execute("UPDATE cx_derived SET status='STALE' WHERE id=?",(r['id'],)); changed+=1
                if not changed: break
            for r in self.rows("SELECT v.id,v.definition_json FROM skill_version v JOIN cx_skill c ON c.id=v.id WHERE v.status IN ('CANDIDATE','TESTED','APPROVED')"):
                d=json.loads(r['definition_json'])
                try:
                    valid=self.refs_valid(d['refs']) and self.basis(Goal.from_dict(d['goal']))==d['basis']
                except CortexError:
                    valid=False
                if not valid:
                    self.conn.execute("UPDATE skill_version SET status='REVOKED' WHERE id=?",(r['id'],))
            for r in self.rows("SELECT e.id,e.plan_json,e.skill_id,v.status skill_status FROM episode e JOIN cx_episode c ON c.id=e.id LEFT JOIN skill_version v ON v.id=e.skill_id WHERE e.outcome='proposed'"):
                try:
                    p=json.loads(r['plan_json']); valid=(not r['skill_id'] or r['skill_status']=='APPROVED') and self.refs_valid(p['refs']) and p['basis']==self.basis(Goal.from_dict(p['goal']),frozenset(p.get('held',[])))
                except (CortexError,KeyError,TypeError,ValueError): valid=False
                if not valid:self.settle(r['id'],'invalidated',('proposed',),'dependency changed')   # the commitment follows in this transaction

    def decision(self, stage: str, provider: str, model: str | None, accepted: bool, detail: str) -> None:
        with self.tx():
            self.conn.execute('INSERT INTO cx_decision VALUES(?,?,?,?,?,?,?)',
                              (uid('dec'), stage, provider, model, int(accepted), detail[:500], self.clock()))

    def manifest(self, name: str, kind: str, payload: dict):
        with self.tx():
            self.conn.execute('INSERT INTO cx_manifest VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,created_at=excluded.created_at',
                              (name,kind,canonical(payload),self.clock()))

    def episodes_about(self, ids: set[str], limit: int = 8, *, principal: str | None = None) -> list[dict]:
        """The principal's newest episodes naming any of these objects (no principal: none), searched BEFORE limiting."""
        names = [' '+normalize(n)+' ' for o in self.objects() if o['id'] in ids for n in [o['id'], o['name']]+o['aliases']]
        out = []
        with self.lock:
            for r in self.conn.execute('SELECT e.id,e.request,e.outcome,e.failure,e.started_at,c.mode FROM episode e JOIN cx_episode c ON c.id=e.id WHERE c.owner=? ORDER BY e.started_at DESC',(principal,)):
                p = ' '+normalize(r['request'])+' '
                if any(n in p for n in names): out.append(dict(r))
                if len(out) >= limit: break
        return out

    def recall(self, question: str, kinds: list[str] | None = None, *, principal: str | None = None) -> dict:
        # Episodes and commitments are the asking principal's only (no principal: none): another operator's requests stay private.
        allowed={'semantic','episodic','procedural','external','parametric','prospective','working'}
        chosen=set(kinds or allowed)
        if not chosen <= allowed: raise CortexError('unknown memory kind')
        result={'question':question,'kind':'answer','memories':{},'evidence':[], 'reply':''}
        if 'semantic' in chosen:
            result['memories']['semantic']={'rules':self.rules(), 'objects':self.objects()}
        if 'external' in chosen:
            result['memories']['external']=self.search(question)
            result['evidence']=[x['chunk_id'] for x in result['memories']['external']]
        if 'episodic' in chosen:
            result['memories']['episodic']=self.rows('SELECT e.id,e.request,e.outcome,e.failure,e.started_at,c.mode FROM episode e JOIN cx_episode c ON c.id=e.id WHERE c.owner=? ORDER BY e.started_at DESC LIMIT 8',(principal,))
        if 'procedural' in chosen:
            self.invalidate()
            result['memories']['procedural']=self.approved_skills(principal,limit=10,fields=('id','name','version','status'))
        if 'prospective' in chosen:
            result['memories']['prospective']=self.rows("SELECT p.id,p.trigger,p.request,p.status,p.proposal_id FROM prospective_task p JOIN cx_commitment c ON c.id=p.id WHERE p.status NOT IN ('done','cancelled') AND c.owner=? ORDER BY p.created_at DESC LIMIT 10",(principal,))
        if 'parametric' in chosen:
            result['memories']['parametric']=[{**r,'payload':json.loads(r.pop('payload_json'))} for r in self.rows('SELECT * FROM cx_manifest')]
        if 'working' in chosen:
            result['memories']['working']={'persistent':False,'note':'Request-local context is rebuilt, not treated as factual history.'}
        if 'semantic' in chosen:
            result['memories']['semantic']['declared_facts']=self.generic_facts(question)
        # Select the right lifecycle BEFORE wording the answer. Never answer an
        # event-history question by quoting a procedure as if that event happened.
        low=normalize(question); ids=set(self.resolve_objects(question))
        hits=result['memories'].get('external',[])
        episodes=result['memories'].get('episodic',[])
        if ids and 'episodic' in chosen:
            episodes=self.episodes_about(ids,principal=principal)
        if re.search(r'\b(what happened|what did you|last time|previous run)\b',low):
            if episodes:
                e=episodes[0]
                result['reply']=f"Recorded task: {e['request']}. Outcome: {e['outcome']} ({e.get('mode') or 'legacy, unknown evidence class'})."
                result['evidence']=['episode:'+e['id']]
            else:result['reply']='No matching execution record; a procedure is not proof that it happened.'
        elif re.search(r'\b(pending|what is next|what needs to happen|waiting)\b',low):
            tasks=result['memories'].get('prospective',[])
            result['reply']='; '.join(f"{t['request']}: {t['status']} (trigger: {t['trigger']})" for t in tasks[:3]) or 'No open commitments in the returned scope.'
        elif 'where' in low and ids and all(o['fresh'] for o in self.objects() if o['id'] in ids):
            # Object state answers a location only while CURRENT; unobserved or stale state falls through to the evidence.
            result['reply']='; '.join(o['statement'] for o in self.objects() if o['id'] in ids)
        elif hits:
            result['reply']='Source says: '+hits[0]['text'][:600]
        elif ids:
            result['reply']='; '.join(o['statement'] for o in self.objects() if o['id'] in ids)
        else:result['reply']='I do not have enough source evidence to answer that.'
        return result
