"""Bounded read-only frame fanout. Never opens the camera or motor controller.

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

class FrameHub:
    def __init__(self,fetch:Callable,*,fps:float=10,max_bytes:int=1_000_000,clock=time.time):
        self.fetch=fetch;self.fps=finite(fps,'display polling rate',1,30);self.max_bytes=max_bytes;self.clock=clock
        self.condition=threading.Condition();self.current=None;self.error=None;self.sequence=0
        self._stop=threading.Event();self._thread=None
    def publish(self,jpeg:bytes,captured_at:float|None=None):
        if not isinstance(jpeg,bytes) or len(jpeg)>self.max_bytes or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
            raise CortexError('missing, invalid or oversized JPEG')
        now=self.clock()
        if captured_at is not None: captured_at=finite(captured_at,'capture time',0,now+1)
        with self.condition:
            self.sequence+=1
            self.current=Frame(self.sequence,jpeg,now,captured_at,hashlib.sha256(jpeg).hexdigest())
            self.error=None;self.condition.notify_all()
    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear()
        def producer():
            while not self._stop.is_set():
                start=time.monotonic()
                try:
                    data,captured=self.fetch()
                    if not self._stop.is_set():self.publish(data,captured)
                except Exception as e:
                    with self.condition:self.error=type(e).__name__;self.condition.notify_all()
                self._stop.wait(max(0,1/self.fps-(time.monotonic()-start)))
        self._thread=threading.Thread(target=producer,name='engram-frame-fanout',daemon=True);self._thread.start()
    def stop(self):
        self._stop.set()
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
                'receipt_age_s':max(0,now-f.received_at) if f else None,
                'capture_age_s':max(0,now-f.captured_at) if f and f.captured_at is not None else None,
                'freshness_basis':'capture' if f and f.captured_at is not None else 'receipt_only_capture_unknown',
                'buffered_frames':1 if f else 0,'configured_poll_fps':self.fps}

class LocalJPEG:
    def __init__(self,url:str,*,timeout_s:float=2):
        self.url=validate_loopback(url)
        parsed=urllib.parse.urlparse(self.url)
        if parsed.path not in {'/robot/frame','/frame.jpg'}:raise CortexError('only a configured read-only frame endpoint is supported')
        self.timeout=finite(timeout_s,'camera timeout',.1,5)
    def __call__(self):
        request=urllib.request.Request(self.url,headers={'Accept':'image/jpeg','Cache-Control':'no-cache'},method='GET')
        with urllib.request.build_opener(NoRedirect).open(request,timeout=self.timeout) as response:
            data=response.read(1_000_001)
            captured=response.headers.get('X-Frame-Captured-At')
        return data,float(captured) if captured else None
