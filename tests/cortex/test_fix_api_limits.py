"""Streamed/chunked bodies: no unauthenticated buffering, 413 past the limit (review finding 5)."""
import asyncio,json
import pytest
from engram.cortex.api import create_app
TOKEN='synthetic-test-operator-token-32-characters'
SENSOR='synthetic-test-sensor-token-32-characters!'
CHUNK=64*1024

def call(app,method,path,*,auth=None,total=0,body=None,declared=None):
    """Drive the ASGI app with a body that arrives in 64 KB chunks and no Content-Length (chunked)."""
    pulled=0;sent=[]
    headers=[(b'host',b'x'),(b'content-type',b'application/json')]+([(b'authorization',b'Bearer '+auth.encode())] if auth else [])
    headers+=[(b'content-length',str(declared).encode())] if declared is not None else [(b'transfer-encoding',b'chunked')]
    data=body if body is not None else b'{"title":"t","text":"'+b'a'*total+b'"}'
    async def receive():
        nonlocal pulled
        if pulled>=len(data):return {'type':'http.disconnect'}
        part=data[pulled:pulled+CHUNK];pulled+=len(part)
        return {'type':'http.request','body':part,'more_body':pulled<len(data)}
    async def send(m):sent.append(m)
    scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,'scheme':'http','path':path,'raw_path':path.encode(),
           'query_string':b'','root_path':'','headers':headers,'client':('127.0.0.1',5),'server':('127.0.0.1',8010)}
    asyncio.run(app(scope,receive,send))
    start=next(m for m in sent if m['type']=='http.response.start')
    return start['status'],pulled,b''.join(m.get('body',b'') for m in sent if m['type']=='http.response.body')

@pytest.fixture
def app(brain):return create_app(brain,tokens={TOKEN:'carther'},sensor_tokens={SENSOR:'sensor:camera'},start_idle=False)

def test_unauthenticated_chunked_mutation_is_refused_before_any_body_byte(app):
    status,pulled,_=call(app,'POST','/memory/sources',total=20*2**20)
    assert status==401 and pulled==0                                  # was: 20 MB buffered, then 401
    status,pulled,_=call(app,'POST','/memory/sources',auth='x'*40,total=20*2**20)
    assert status==401 and pulled==0

def test_authenticated_chunked_body_stops_at_the_limit(app,brain):
    status,pulled,body=call(app,'POST','/memory/sources',auth=TOKEN,total=20*2**20)
    assert status==413 and pulled<=300000+CHUNK and json.loads(body)['detail']=='input too large'
    assert call(app,'POST','/memory/sources',auth=TOKEN,declared=400000,body=b'')[:2]==(413,0)
    assert not brain.store.rows("SELECT * FROM source")

def test_small_chunked_bodies_still_reach_the_routes(app):
    status,_,body=call(app,'POST','/memory/sources',auth=TOKEN,body=json.dumps({'title':'SOP','text':'Safety goggles must be at the ppe station before handling Sample B.'}).encode())
    assert status==200 and json.loads(body)['source_id']
    status,_,_=call(app,'POST','/observations/sensor',auth=SENSOR,body=b'{}')
    assert status==422                                                # a sensor credential passes the gate; the route validates
    status,_,_=call(app,'POST','/commands',auth=TOKEN,body=b'{"text":"hello"}')
    assert status==200
