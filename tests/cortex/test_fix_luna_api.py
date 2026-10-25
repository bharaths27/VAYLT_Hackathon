"""Regression tests for the luna API review of PR 7 (each fails on 3cd807c)."""
import asyncio,json,socket,threading,time,urllib.request
from http.server import BaseHTTPRequestHandler,HTTPServer
import pytest
from fastapi.testclient import TestClient
from engram.cortex.api import create_app
from engram.cortex.console_adapter import AsyncCortexMemory
from engram.cortex.media import FrameHub,LocalJPEG
TOKEN='synthetic-console-token-32-characters'
JPEG=bytes([0xff,0xd8])+b'REAL-CAMERA'+bytes([0xff,0xd9])
CRLF=bytes([13,10])

def recording_proxy(reply: bytes):
    """A proxy that records every request it is sent and answers with `reply`."""
    srv=socket.socket();srv.bind(('127.0.0.1',0));srv.listen(4);seen=[]
    def run():
        while True:
            try:c,_=srv.accept()
            except OSError:return
            seen.append(c.recv(65536))
            c.sendall(CRLF.join([b'HTTP/1.1 200 OK',b'Content-Length: '+str(len(reply)).encode(),b'Connection: close',b''])+CRLF+reply);c.close()
    threading.Thread(target=run,daemon=True).start()
    return srv,seen,f'http://127.0.0.1:{srv.getsockname()[1]}'

def serve(body: bytes, ctype: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    srv=HTTPServer(('127.0.0.1',0),H);threading.Thread(target=srv.serve_forever,daemon=True).start()
    return srv

@pytest.fixture
def proxied(monkeypatch):
    def use(url):
        for k in ('NO_PROXY','no_proxy'):monkeypatch.delenv(k,raising=False)
        for k in ('HTTP_PROXY','http_proxy','HTTPS_PROXY','https_proxy','ALL_PROXY','all_proxy'):monkeypatch.setenv(k,url)
        monkeypatch.setattr(urllib.request,'proxy_bypass',lambda host:False)   # Windows registry bypass would hide the leak
    return use


# P1: the console bridge's bearer token never leaves through an environment proxy.
def test_console_bridge_token_never_goes_through_an_env_proxy(monkeypatch,proxied):
    proxy,seen,url=recording_proxy(b'{"via":"proxy"}'); srv=serve(b'{"via":"server"}','application/json')
    proxied(url); monkeypatch.setenv('CORTEX_API_TOKEN',TOKEN)
    async def go():
        m=AsyncCortexMemory(f'http://127.0.0.1:{srv.server_port}')
        try:return await m.list_skills()
        finally:await m.close()
    try:
        assert asyncio.run(go())=={'via':'server'} and not seen
    finally:srv.shutdown();srv.server_close();proxy.close()


# P2: the loopback camera is read directly; a proxy cannot hand the inspector a fabricated frame.
def test_camera_frame_never_goes_through_an_env_proxy(proxied):
    proxy,seen,url=recording_proxy(bytes([0xff,0xd8])+b'FABRICATED'+bytes([0xff,0xd9])); srv=serve(JPEG,'image/jpeg')
    proxied(url)
    try:
        data,captured=LocalJPEG(f'http://127.0.0.1:{srv.server_port}/frame.jpg')()
        assert data==JPEG and captured is None and not seen
    finally:srv.shutdown();srv.server_close();proxy.close()


# P3: frame freshness is judged on a monotonic receipt clock; a wall-clock step back cannot make an old frame live.
def test_frame_freshness_uses_a_monotonic_receipt_clock(brain):
    wall=[time.time()+3600.0]; mono=[100.0]       # received an hour "ahead": the wall clock has since stepped back
    def dead_camera(): raise OSError('camera gone')
    hub=FrameHub(dead_camera,clock=lambda:wall[0]); hub.monotonic=lambda:mono[0]
    hub.publish(JPEG); mono[0]+=10                 # ten real seconds later
    assert hub.metadata()['receipt_age_s']==10
    with TestClient(create_app(brain,tokens={TOKEN:'carther'},frame_hub=hub,start_idle=False)) as c:
        assert c.get('/frame.jpg',headers={'Authorization':'Bearer '+TOKEN}).status_code==503
