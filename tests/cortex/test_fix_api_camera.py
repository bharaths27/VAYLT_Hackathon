"""The camera poller runs only while a viewer is attached (review finding 7)."""
import asyncio,threading,time
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.media import FrameHub
TOKEN='synthetic-test-operator-token-32-characters'
JPEG=b'\xff\xd8'+b'\x00'*64+b'\xff\xd9'

class Counting:
    def __init__(self):self.n=0
    def __call__(self):self.n+=1;return JPEG,None

def wait_until(pred,timeout=3):
    end=time.monotonic()+timeout
    while time.monotonic()<end and not pred():time.sleep(.02)
    return pred()

def test_no_viewer_no_fetch_then_single_frame_viewer_then_idle(brain):
    fetch=Counting();hub=FrameHub(fetch,fps=20);hub.idle_s=.3
    with TestClient(create_app(brain,tokens={TOKEN:'carther'},frame_hub=hub,start_idle=False)) as c:
        c.headers['Authorization']='Bearer '+TOKEN
        time.sleep(.6)
        assert fetch.n==0 and not c.get('/camera/status').json()['polling']   # was ~12 fetches at startup with nobody watching
        r=c.get('/frame.jpg')
        assert r.status_code==200 and r.content==JPEG and fetch.n>=1          # the first viewer gets a frame without a prior poll
        assert wait_until(lambda:not hub.polling())
        time.sleep(.15);settled=fetch.n;time.sleep(.5)
        assert fetch.n==settled                                              # stopped after the grace period

def test_stream_viewer_keeps_polling_and_detaches_on_disconnect(brain):
    fetch=Counting();hub=FrameHub(fetch,fps=20);hub.idle_s=.2;hub.start()
    app=create_app(brain,tokens={TOKEN:'carther'},frame_hub=hub,start_idle=False)
    async def stream():
        got=asyncio.Event();sent=[];calls=[]
        async def receive():
            calls.append(1)
            if len(calls)==1:return {'type':'http.request','body':b'','more_body':False}   # a GET has an empty body
            await got.wait();await asyncio.sleep(.3);return {'type':'http.disconnect'}
        async def send(m):
            sent.append(m)
            if m['type']=='http.response.body' and m.get('body'):got.set()
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET','scheme':'http','path':'/video.mjpg','raw_path':b'/video.mjpg',
               'query_string':b'','root_path':'','headers':[(b'host',b'x'),(b'authorization',('Bearer '+TOKEN).encode())],'client':('127.0.0.1',5),'server':('127.0.0.1',8010)}
        await asyncio.wait_for(app(scope,receive,send),10)
        return sent
    try:
        sent=asyncio.run(stream())
        assert any(m.get('body','').startswith(b'--frame') for m in sent if m['type']=='http.response.body')
        assert hub.metadata()['viewers']==0
        assert wait_until(lambda:not hub.polling())
        time.sleep(.15);settled=fetch.n;time.sleep(.4);assert fetch.n==settled
    finally:hub.stop()
