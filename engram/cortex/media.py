"""Bounded read-only frame fanout. Never opens the camera or motor controller.
Polls the configured frame route only while a viewer is attached.

Transport freshness is NOT capture freshness. Unknown capture timestamps remain
unknown, including when a camera service repeats an old JPEG with a new response.
"""
from __future__ import annotations
import hashlib,threading,time,urllib.request
from dataclasses import dataclass
from typing import Callable
from .models import NoRedirect,validate_loopback
from .types import CortexError,finite

@dataclass(frozen=True)
class Frame:
    seq:int
    jpeg:bytes
    received_at:float
    captured_at:float|None
    digest:str
    received_mono:float  # receipt on the monotonic clock: what freshness is judged on (a wall clock can step back)

class FrameHub:
    """Polls the camera only while someone is watching: a streaming viewer (watch) or a recent
    single-frame request (touch). Idle_s after the last viewer leaves, polling stops."""
    def __init__(self,fetch:Callable,*,fps:float=10,max_bytes:int=1_000_000,clock=time.time,idle_s:float=5):
        self.fetch=fetch;self.fps=finite(fps,'display polling rate',1,30);self.max_bytes=max_bytes;self.clock=clock;self.monotonic=time.monotonic
        self.idle_s=finite(idle_s,'viewer grace',0,60)
        self.condition=threading.Condition();self.current=None;self.error=None;self.sequence=0
        self._stop=threading.Event();self._wake=threading.Event();self._thread=None;self._viewers=0;self._lease=0.0
    def publish(self,jpeg:bytes,captured_at:float|None=None):
        if not isinstance(jpeg,bytes) or len(jpeg)>self.max_bytes or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
            raise CortexError('missing, invalid or oversized JPEG')
        now=self.clock()
        if captured_at is not None: captured_at=finite(captured_at,'capture time',0,now+1)
        with self.condition:
            self.sequence+=1
            self.current=Frame(self.sequence,jpeg,now,captured_at,hashlib.sha256(jpeg).hexdigest(),self.monotonic())
            self.error=None;self.condition.notify_all()
    def receipt_age(self,f:Frame)->float:
        return self.monotonic()-f.received_mono
    def touch(self):
        """A viewer wants frames now; keep polling for idle_s more seconds."""
        with self.condition:self._lease=max(self._lease,time.monotonic()+self.idle_s)
        self._wake.set()
    def watch(self):
        with self.condition:self._viewers+=1
        self._wake.set()
    def unwatch(self):
        with self.condition:self._viewers=max(0,self._viewers-1);self._lease=max(self._lease,time.monotonic()+self.idle_s)
    def polling(self)->bool:
        with self.condition:return self._viewers>0 or time.monotonic()<self._lease
    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear()
        def producer():
            while not self._stop.is_set():
                if not self.polling():
                    self._wake.wait(1);self._wake.clear();continue
                start=time.monotonic()
                try:
                    data,captured=self.fetch()
                    if not self._stop.is_set():self.publish(data,captured)
                except Exception as e:
                    with self.condition:self.error=type(e).__name__;self.condition.notify_all()
                self._stop.wait(max(0,1/self.fps-(time.monotonic()-start)))
        self._thread=threading.Thread(target=producer,name='engram-frame-fanout',daemon=True);self._thread.start()
    def stop(self):
        self._stop.set();self._wake.set()
        with self.condition:self.condition.notify_all()
        if self._thread:self._thread.join(timeout=3)
    def latest(self,*,after:int=0,wait_s:float=0)->Frame|None:
        with self.condition:
            if wait_s and (self.current is None or self.current.seq<=after):
                self.condition.wait_for(lambda:self._stop.is_set() or (self.current and self.current.seq>after),timeout=min(wait_s,2))
            f=self.current
            return f if f and f.seq>after else None
    def metadata(self):
        with self.condition:
            f=self.current; now=self.clock()
            return {'available':f is not None,'seq':f.seq if f else None,'error':self.error,
                'receipt_age_s':self.receipt_age(f) if f else None,
                'capture_age_s':max(0,now-f.captured_at) if f and f.captured_at is not None else None,
                'freshness_basis':'capture' if f and f.captured_at is not None else 'receipt_only_capture_unknown',
                'buffered_frames':1 if f else 0,'configured_poll_fps':self.fps,'polling':self._viewers>0 or time.monotonic()<self._lease,'viewers':self._viewers}

class LocalJPEG:
    def __init__(self,url:str,*,timeout_s:float=2):
        self.url=validate_loopback(url)
        parsed=urllib.parse.urlparse(self.url)
        if parsed.path not in {'/robot/frame','/frame.jpg'}:raise CortexError('only a configured read-only frame endpoint is supported')
        self.timeout=finite(timeout_s,'camera timeout',.1,5)
    def __call__(self):
        request=urllib.request.Request(self.url,headers={'Accept':'image/jpeg','Cache-Control':'no-cache'},method='GET')
        # ProxyHandler({}): never through HTTP(S)_PROXY; a proxy could serve a fabricated frame as the local camera.
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect).open(request,timeout=self.timeout) as response:
            data=response.read(1_000_001)
            captured=response.headers.get('X-Frame-Captured-At')
        return data,float(captured) if captured else None
