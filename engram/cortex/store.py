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

from .schema import migrate
from .types import (Conflict, CortexError, Denied, Goal, Missing, Policy, Stale,
                    canonical, digest, finite, normalize, text, uid)

RULE_RE = re.compile(
    r'(?P<pre>[A-Za-z0-9 _-]+?)\s+(?:must|should|needs? to)\s+be\s+'
    r'(?:at|on|in)\s+(?:the\s+)?(?P<zone>[A-Za-z0-9 _-]+?)\s+before\s+'
    r'(?:handling|using|moving|touching)\s+(?P<subject>[A-Za-z0-9 _-]+)', re.I)


class Store:
    def __init__(self, path: str | Path = ':memory:', *, workspace: str = 'local',
                 policy: Policy | None = None, clock: Callable[[], float] = time.time):
        self.path = str(path)
        self.workspace = text(workspace, 'workspace', 100)
        self.policy = policy or Policy()
        self.clock = clock
        self.lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if self.path != ':memory:':
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=FULL')
        migrate(self.conn)
        with self.tx():
            existing = self.one("SELECT value FROM meta WHERE key='cortex_workspace'")
            if existing and existing['value'] != self.workspace:
                raise Denied('one local workspace per vault; use a separate database for another workspace')
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES('cortex_workspace',?)", (self.workspace,))

    def close(self):
        with self.lock:
            self.conn.close()

    @contextmanager
    def tx(self):
        with self.lock:
            root = self._depth == 0
            savepoint = f'cx_sp_{self._depth}'
            self.conn.execute('BEGIN IMMEDIATE' if root else f'SAVEPOINT {savepoint}')
            self._depth += 1
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
                self.conn.execute('COMMIT' if root else f'RELEASE {savepoint}')

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
        with self.tx():
            for z in zones:
                text(z, 'zone', 80)
                existing = self.one('SELECT * FROM cx_station WHERE id=?', (z,))
                tag = next((int(k) for k, v in arena.get('zone_tags', {}).items() if v == z), None)
                self.conn.execute('INSERT INTO cx_station(id,name,aliases_json,tag_id,version) VALUES(?,?,?,?,1) '
                                  'ON CONFLICT(id) DO UPDATE SET tag_id=excluded.tag_id,version=version+CASE WHEN tag_id IS NOT excluded.tag_id THEN 1 ELSE 0 END',
                                  (z, z.replace('_', ' '), canonical([]), tag))
            for obj in arena.get('objects', []):
                oid, name = text(obj.get('id'), 'object id', 80), text(obj.get('name'), 'object name', 100)
                tag = obj.get('tag')
                if tag is not None and (isinstance(tag, bool) or not isinstance(tag, int) or not 0 <= tag <= 99999):
                    raise CortexError('invalid object tag')
                aliases = obj.get('aliases', [])
                if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases) or len(aliases) > 20:
                    raise CortexError('invalid object aliases')
                prior = self.one('SELECT * FROM object_binding WHERE object_id=?', (oid,))
                prior_alias = self.one('SELECT aliases_json FROM cx_entity WHERE object_id=?', (oid,))
                changed = prior is not None and (prior['name'] != name or prior['tag_id'] != tag or bool(prior['graspable']) != obj.get('graspable', True)
                           or (prior_alias and prior_alias['aliases_json'] != canonical(aliases)))
                self.conn.execute('INSERT INTO object_binding(object_id,name,tag_id,graspable,width_mm) VALUES(?,?,?,?,?) '
                                  'ON CONFLICT(object_id) DO UPDATE SET name=excluded.name,tag_id=excluded.tag_id,graspable=excluded.graspable,width_mm=excluded.width_mm',
                                  (oid, name, tag, int(bool(obj.get('graspable', True))), obj.get('width_mm')))
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
            age = None if r['last_seen_at'] is None else max(0.0, t - r['last_seen_at'])
            obs=self.one('SELECT payload FROM observation WHERE id=?',(r['last_observation_id'],)) if r['last_observation_id'] else None
            try:r['evidence_class']=json.loads(obs['payload']).get('mode','LEGACY_UNKNOWN') if obs else 'UNKNOWN'
            except (TypeError,ValueError):r['evidence_class']='UNKNOWN'
            if getattr(self,'runtime_mode',None)=='PHYSICAL' and r['evidence_class']!='PHYSICAL': age=None
            r['age_s'] = age
            r['fresh'] = age is not None and age <= self.policy.freshness_s
            r['id'] = r['object_id']
            r['tag'] = r['tag_id']
            r['freshness'] = max(0.0, 1.0 - age/self.policy.freshness_s) if age is not None else 0.0
            r['statement'] = (f"{r['name']}: location unknown" if not r['last_zone'] else
                              f"{r['name']} {'is' if r['fresh'] else 'was last observed'} at {r['last_zone']}")
        return out

    def resolve_objects(self, phrase: str) -> list[str]:
        p = ' ' + normalize(phrase) + ' '
        found = []
        for o in self.objects():
            names = [o['id'], o['name']] + o['aliases']
            if any(' '+normalize(n)+' ' in p for n in names):
                found.append(o['id'])
        return found

    def resolve_zone(self, phrase: str) -> str | None:
        p = ' ' + normalize(phrase) + ' '
        hits = []
        for z in self.rows('SELECT * FROM cx_station'):
            names = [z['id'], z['name']] + json.loads(z['aliases_json'])
            if any(' '+normalize(n)+' ' in p for n in names):
                hits.append(z['id'])
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

    def _extract(self, body: str) -> tuple[list[dict], list[str]]:
        found, unknown = [], []
        for m in RULE_RE.finditer(body):
            pre, subject = self.resolve_objects(m['pre']), self.resolve_objects(m['subject'])
            try:
                zone = self.resolve_zone(m['zone'])
            except Missing:
                zone = None
            if len(pre) == len(subject) == 1 and zone:
                found.append({'subject': subject[0], 'prerequisite': pre[0], 'required_zone': zone})
            else:
                unknown.append(m.group(0))
        return found, unknown

    def teach(self, title: str, body: str, *, owner: str, kind: str = 'document',
              rules: list[dict] | None = None, supersedes: str | None = None,
              cloud_ok: bool = False, goal_definitions: dict | None = None, prohibitions: list[str] | None = None, facts: list[dict] | None = None) -> dict:
        title, body, owner = text(title, 'title', 200), text(body, 'source', 200000), text(owner, 'source owner', 100)
        extracted, unresolved = self._extract(body) if rules is None else (rules, [])
        if not isinstance(extracted, list) or len(extracted) > 64:
            raise CortexError('too many rules')
        known = {o['id'] for o in self.objects()}
        if rules is None and re.search(r'\b(unless|except|only if|provided that)\b',body,re.I):
            unresolved.append('Conditional exception requires explicit structured review')
            extracted=[]
        checked = []
        for r in extracted:
            if not isinstance(r, dict) or set(r) - {'subject','prerequisite','required_zone','rule_key'}:
                raise CortexError('invalid prerequisite rule shape')
            if r.get('subject') not in known or r.get('prerequisite') not in known or r.get('required_zone') not in self.zones():
                raise Missing('rule refers to an unregistered object or station')
            if r['subject'] == r['prerequisite']:
                raise Conflict('self-dependent rule')
            checked.append({**r, 'rule_key': r.get('rule_key') or f"{r['subject']}::requires::{r['prerequisite']}"})
        forbidden = list(prohibitions or [])
        for sent in re.split(r'[.\n]', body):
            if re.search(r"\b(never|must not|do not|don't)\s+(move|touch|handle|pick|carry)\b", sent, re.I):
                forbidden.extend(self.resolve_objects(sent))
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
            Goal.from_dict(g)
        sid, cid, t = uid('src'), uid('chk'), self.clock()
        with self.tx():
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
            if unresolved:
                self.conn.execute('INSERT INTO cx_unresolved VALUES(?,?,?)',(sid,canonical(self.resolve_objects(body)),'; '.join(unresolved)[:1000]))
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
                'unresolved_rules': unresolved, 'kind': 'taught'}

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
        parts = self.rows('SELECT text FROM source_chunk WHERE source_id=? ORDER BY start_char', (sid,))
        return self.teach(s['title'], '\n\n'.join(p['text'] for p in parts), owner=owner, kind='adopted_legacy')

    def revoke_source(self, sid: str, *, delete: bool = False) -> None:
        self.source_state(sid)
        with self.tx():
            self.conn.execute('UPDATE cx_source SET status=? WHERE source_id=?', ('deleted' if delete else 'revoked', sid))
            self.conn.execute("UPDATE cx_rule SET status='revoked' WHERE assertion_id IN (SELECT id FROM assertion WHERE source_id=?)", (sid,))
            if delete:
                for r in self.rows('SELECT id FROM source_chunk WHERE source_id=?', (sid,)):
                    self.conn.execute('DELETE FROM source_chunk_fts WHERE chunk_id=?', (r['id'],))
                self.conn.execute("UPDATE source_chunk SET text='[deleted]',end_char=start_char WHERE source_id=?", (sid,))
            self._bump()
            self.invalidate()
            if delete:
                # Remove retained source-text copies from public memory projections.
                for d in self.rows("SELECT id,refs_json FROM cx_derived WHERE status='STALE'"):
                    if not self.refs_valid(json.loads(d['refs_json'])):
                        self.conn.execute("UPDATE cx_derived SET payload_json='{\"redacted\":true}',status='DELETED' WHERE id=?",(d['id'],))
                for e in self.rows('SELECT id,evidence_json FROM episode WHERE evidence_json IS NOT NULL'):
                    meta=json.loads(e['evidence_json'])
                    evid=meta.get('bundle',{}).get('evidence',[])
                    if any(x.get('source_id')==sid for x in evid):
                        meta['bundle']={'redacted':True,'reason':'source removed'}
                        self.conn.execute('UPDATE episode SET evidence_json=? WHERE id=?',(canonical(meta),e['id']))
                for k in self.rows("SELECT id,definition_json FROM skill_version WHERE status='REVOKED'"):
                    definition=json.loads(k['definition_json'])
                    if 'source:'+sid in definition.get('refs',[]):
                        self.conn.execute("UPDATE skill_version SET definition_json='{\"redacted\":true}' WHERE id=?",(k['id'],))
            # A removed instruction cannot wake a pending commitment later.
            self.conn.execute("UPDATE prospective_task SET status='cancelled' WHERE source_id=? AND status IN ('waiting','fired')", (sid,))

    def rules(self) -> list[dict]:
        return self.rows("SELECT a.*,r.rule_key,r.required_zone,r.status,c.revision FROM assertion a JOIN cx_rule r ON r.assertion_id=a.id "
                         "JOIN cx_source c ON c.source_id=a.source_id WHERE c.workspace=? AND c.status='accepted' AND r.status IN ('active','disputed') ORDER BY r.rule_key,a.id", (self.workspace,))

    def related_rules(self, goal: Goal) -> list[dict]:
        frontier, seen, relevant = list(goal.targets), set(), []
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

    def unresolved_for(self,object_ids: set[str]) -> list[dict]:
        rows=self.rows("SELECT u.* FROM cx_unresolved u JOIN cx_source s ON s.source_id=u.source_id WHERE s.status='accepted' AND s.workspace=?",(self.workspace,))
        return [r for r in rows if object_ids & set(json.loads(r['mentions_json']))]

    def generic_facts(self, query: str='') -> list[dict]:
        rows=self.rows("SELECT a.*,f.status FROM assertion a JOIN cx_fact f ON f.assertion_id=a.id JOIN cx_source s ON s.source_id=a.source_id WHERE s.workspace=? AND s.status='accepted' AND f.status IN ('active','disputed') ORDER BY a.created_at DESC",(self.workspace,))
        words=set(re.findall(r'[a-z0-9]+',normalize(query)))
        return [{**r,'evidence_class':'source_declared_not_sensor_truth'} for r in rows if not words or words & set(re.findall(r'[a-z0-9]+',normalize(r['subject']+' '+r['predicate']+' '+r['object'])))][:32]

    def goal_alias(self, request: str) -> Goal | None:
        name = normalize(request)
        row = self.one("SELECT g.* FROM cx_goal g JOIN cx_source s ON g.source_id=s.source_id WHERE g.name=? AND s.workspace=? AND s.status='accepted'", (name, self.workspace))
        return Goal.from_dict(json.loads(row['goal_json'])) if row else None

    def basis(self, goal: Goal) -> str:
        rules = self.related_rules(goal)
        ids = set(goal.targets) | {r['subject'] for r in rules} | {r['object'] for r in rules}
        entities = [{k:o[k] for k in ('id','name','tag','graspable','version')} for o in self.objects() if o['id'] in ids]
        zones = self.rows('SELECT id,version FROM cx_station ORDER BY id')
        payload = {'goal':goal.to_dict(), 'rules':[{k:r[k] for k in ('id','rule_key','required_zone','object','subject','status')} for r in rules],
                   'entities':entities, 'zones':zones}
        payload['unresolved']=self.unresolved_for(ids)
        payload['prohibitions'] = self.prohibitions(ids)
        return digest(payload)

    def evidence(self, chunk_id: str, *, history: bool = False) -> dict:
        r = self.one('SELECT c.*,s.title,x.status,x.revision,x.cloud_ok FROM source_chunk c JOIN source s ON s.id=c.source_id '
                     'JOIN cx_source x ON x.source_id=s.id WHERE c.id=? AND x.workspace=?', (chunk_id, self.workspace))
        if not r or r['status'] in {'deleted','revoked'} or (not history and r['status'] != 'accepted'):
            raise Missing('evidence is unavailable, revoked or superseded')
        return r

    def search(self, query: str, limit: int = 5) -> list[dict]:
        tokens = re.findall(r'[A-Za-z0-9]+', text(query, 'query', 2000))[:40]
        if not tokens:
            return []
        q = ' OR '.join('"'+t+'"' for t in tokens)
        return self.rows("SELECT c.id chunk_id,c.source_id,c.text,c.start_char,c.end_char,s.title,x.revision,x.cloud_ok,bm25(source_chunk_fts) score "
                         "FROM source_chunk_fts f JOIN source_chunk c ON c.id=f.chunk_id JOIN source s ON s.id=c.source_id "
                         "JOIN cx_source x ON x.source_id=s.id WHERE source_chunk_fts MATCH ? AND x.workspace=? AND x.status='accepted' ORDER BY score,c.id LIMIT ?",
                         (q, self.workspace, min(max(int(limit),1),20)))

    def bundle(self, request: str, goal: Goal | None = None) -> dict:
        rules = self.related_rules(goal) if goal else []
        ids = set(goal.targets if goal else self.resolve_objects(request)) | {r['object'] for r in rules}
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
             'basis': self.basis(goal) if goal else None, 'facts':facts, 'objects':objects, 'constraints':facts,
             'evidence':evidence, 'missing':missing, 'omitted':[], 'applicable_skills':[], 'derived':[]}
        b['unresolved_constraints']=self.unresolved_for(ids)
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
            episodes=self.rows('SELECT e.id,e.request,e.outcome,e.failure,c.mode FROM cx_episode_fts f JOIN episode e ON e.id=f.episode_id JOIN cx_episode c ON c.id=e.id WHERE cx_episode_fts MATCH ? ORDER BY bm25(cx_episode_fts) LIMIT 3',(q,))
        prospective=self.rows("SELECT id,request,trigger,status FROM prospective_task WHERE status IN ('waiting','fired','unknown') ORDER BY created_at DESC LIMIT 3")
        approved=self.rows("SELECT v.id,v.name,v.digest FROM skill_version v JOIN cx_skill c ON c.id=v.id WHERE v.status='APPROVED' AND c.goal_key=? LIMIT 3",(goal.key() if goal else '',))
        related_derived=[]
        for d in self.derived()[-32:]:
            if d['status']=='VERIFIED' and self.refs_valid(d['refs']) and (not goal or d['basis_hash']==self.basis(goal)):
                related_derived.append({'id':d['id'],'kind':d['kind'],'payload':d['payload'],'refs':d['refs']})
        optional={'related_episodes':episodes,'prospective':prospective,'applicable_skills':approved,'derived':related_derived[:3],
                  'parametric':{'knowledge_is_evidence':False,'weight_updates':False}}
        for name,value in optional.items():
            trial={**b,name:value}
            if len(canonical(trial).encode())<=self.policy.max_bundle_bytes: b=trial
            else: b['omitted'].append(name)
        return b

    def prohibitions(self, object_ids=None):
        rs=self.rows("SELECT a.id,a.subject,a.source_id,a.chunk_id,c.effect FROM assertion a JOIN cx_constraint c ON c.assertion_id=a.id JOIN cx_source s ON s.source_id=a.source_id WHERE c.status='active' AND s.status='accepted' AND s.workspace=? ORDER BY a.id",(self.workspace,))
        return [r for r in rs if object_ids is None or r['subject'] in object_ids]

    def resolve_rule(self, assertion_id: str, *, principal: str, reason: str):
        text(principal,'reviewer',100); text(reason,'adjudication reason',1000)
        with self.tx():
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

    def derived(self, did: str | None = None, *, include_stale: bool = False) -> dict | list[dict]:
        sql, args = 'SELECT * FROM cx_derived', ()
        if did:
            sql += ' WHERE id=?'; args=(did,)
        elif not include_stale:
            sql += " WHERE status IN ('CANDIDATE','VERIFIED')"
        rows = self.rows(sql+' ORDER BY created_at,id', args)
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
            for r in self.rows("SELECT e.id,e.plan_json FROM episode e JOIN cx_episode c ON c.id=e.id WHERE e.outcome='proposed'"):
                try:
                    p=json.loads(r['plan_json']); valid=self.refs_valid(p['refs']) and p['basis']==self.basis(Goal.from_dict(p['goal']))
                except (CortexError,KeyError,TypeError,ValueError): valid=False
                if not valid:self.conn.execute("UPDATE episode SET outcome='invalidated',failure='dependency changed',finished_at=? WHERE id=?",(self.clock(),r['id']))

    def decision(self, stage: str, provider: str, model: str | None, accepted: bool, detail: str) -> None:
        with self.tx():
            self.conn.execute('INSERT INTO cx_decision VALUES(?,?,?,?,?,?,?)',
                              (uid('dec'), stage, provider, model, int(accepted), detail[:500], self.clock()))

    def manifest(self, name: str, kind: str, payload: dict):
        with self.tx():
            self.conn.execute('INSERT INTO cx_manifest VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,created_at=excluded.created_at',
                              (name,kind,canonical(payload),self.clock()))

    def recall(self, question: str, kinds: list[str] | None = None) -> dict:
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
            result['memories']['episodic']=self.rows('SELECT e.id,e.request,e.outcome,e.failure,e.started_at,c.mode FROM episode e LEFT JOIN cx_episode c ON c.id=e.id ORDER BY e.started_at DESC LIMIT 8')
        if 'procedural' in chosen:
            self.invalidate()
            result['memories']['procedural']=self.rows("SELECT v.id,v.name,v.version,v.status FROM skill_version v JOIN cx_skill c ON c.id=v.id WHERE v.status='APPROVED' ORDER BY v.approved_at DESC LIMIT 10")
        if 'prospective' in chosen:
            result['memories']['prospective']=self.rows("SELECT id,trigger,request,status,proposal_id FROM prospective_task WHERE status NOT IN ('done','cancelled') ORDER BY created_at DESC LIMIT 10")
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
        if ids:
            episodes=[e for e in episodes if ids & set(self.resolve_objects(e['request']))]
        if re.search(r'\b(what happened|what did you|last time|previous run)\b',low):
            if episodes:
                e=episodes[0]
                result['reply']=f"Recorded task: {e['request']}. Outcome: {e['outcome']} ({e.get('mode') or 'legacy, unknown evidence class'})."
                result['evidence']=['episode:'+e['id']]
            else:result['reply']='No matching execution record; a procedure is not proof that it happened.'
        elif re.search(r'\b(pending|what is next|what needs to happen|waiting)\b',low):
            tasks=result['memories'].get('prospective',[])
            result['reply']='; '.join(f"{t['request']}: {t['status']} (trigger: {t['trigger']})" for t in tasks[:3]) or 'No open commitments in the returned scope.'
        elif 'where' in low and ids:
            result['reply']='; '.join(o['statement'] for o in self.objects() if o['id'] in ids)
        elif hits:
            result['reply']='Source says: '+hits[0]['text'][:600]
        elif ids:
            result['reply']='; '.join(o['statement'] for o in self.objects() if o['id'] in ids)
        else:result['reply']='I do not have enough source evidence to answer that.'
        return result
