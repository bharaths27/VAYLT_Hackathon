"""Explicit reviewed bootstrap. Repeated loading never resurrects corrected facts.

Seeded sources are SYNTHETIC evidence: their source kind is prefixed 'synthetic:' and the
seed record stays in meta, so a PHYSICAL runtime refuses to start on a seeded vault.
"""
from .types import Conflict,Denied,CortexError,canonical,digest,text

SYNTHETIC='synthetic:'

def synthetic_seed_present(store)->list[str]:
    """Seed ids (or synthetic source ids) present in this vault, whatever their status."""
    seeds=[r['key'][len('cortex_seed:'):] for r in store.rows("SELECT key FROM meta WHERE key LIKE 'cortex_seed:%' ORDER BY key")]
    return seeds or [r['id'] for r in store.rows('SELECT id FROM source WHERE kind LIKE ? ORDER BY id',(SYNTHETIC+'%',))]

def load_seed(store,seed:dict,*,principal:str,runtime_mode:str='SIMULATED')->dict:
    text(principal,'reviewer',100)
    if set(seed)!={'format','id','version','evidence_class','sources'} or seed['format']!='cortex-reviewed-seed-v1':raise CortexError('unsupported seed contract')
    sid=text(seed['id'],'seed id',80);text(seed['version'],'seed version',40)
    if seed['evidence_class']!='SIMULATED' or runtime_mode!='SIMULATED':raise Denied('this bootstrap loader is for explicitly simulated review data only')
    # The vault's actual runtime decides, not the caller's word: never seed a store a PHYSICAL brain is running on.
    if getattr(store,'runtime_mode','SIMULATED')!='SIMULATED':raise Denied('this vault belongs to a PHYSICAL runtime; synthetic seed data is refused')
    if not isinstance(seed['sources'],list) or len(seed['sources'])>16:raise CortexError('seed source budget exceeded')
    fingerprint=digest(seed);key='cortex_seed:'+sid
    with store.tx():
        previous=store.one('SELECT value FROM meta WHERE key=?',(key,))
        if previous:
            import json
            saved=json.loads(previous['value'])
            if saved['sha256']!=fingerprint:raise Conflict('seed changed; apply explicit reviewed source revisions instead of reseeding')
            return {**saved,'reused':True}
        sources={}
        for source in seed['sources']:
            if not isinstance(source,dict):raise CortexError('invalid seed source')
            local=text(source.get('id'),'seed source id',80)
            if local in sources:raise Conflict('duplicate seed source id')
            fields=set(source)-{'id','title','text','kind','rules','facts','goal_definitions'}
            if fields:raise CortexError('seed contains unauthorized fields')
            kind=SYNTHETIC+text(source.get('kind','document'),'seed source kind',60)
            sources[local]=store.teach(source['title'],source['text'],owner=principal,kind=kind,
                **{k:v for k,v in source.items() if k not in {'id','title','text','kind'}},cloud_ok=False)['source_id']
        saved={'seed_id':sid,'version':seed['version'],'sha256':fingerprint,'source_ids':sources,'evidence_class':'SIMULATED','synthetic':True}
        store.conn.execute('INSERT INTO meta VALUES(?,?)',(key,canonical(saved)))
    return {**saved,'reused':False}
