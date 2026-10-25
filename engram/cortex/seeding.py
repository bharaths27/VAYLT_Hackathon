"""Explicit reviewed bootstrap. Repeated loading never resurrects corrected facts."""
from .types import Conflict,Denied,CortexError,canonical,digest,text

def load_seed(store,seed:dict,*,principal:str,runtime_mode:str='SIMULATED')->dict:
    text(principal,'reviewer',100)
    if set(seed)!={'format','id','version','evidence_class','sources'} or seed['format']!='cortex-reviewed-seed-v1':raise CortexError('unsupported seed contract')
    sid=text(seed['id'],'seed id',80);text(seed['version'],'seed version',40)
    if seed['evidence_class']!='SIMULATED' or runtime_mode!='SIMULATED':raise Denied('this bootstrap loader is for explicitly simulated review data only')
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
            sources[local]=store.teach(source['title'],source['text'],owner=principal,
                **{k:v for k,v in source.items() if k not in {'id','title','text'}},cloud_ok=False)['source_id']
        saved={'seed_id':sid,'version':seed['version'],'sha256':fingerprint,'source_ids':sources,'evidence_class':'SIMULATED'}
        store.conn.execute('INSERT INTO meta VALUES(?,?)',(key,canonical(saved)))
    return {**saved,'reused':False}
