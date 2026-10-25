"""Compatibility with the existing Brian adapter. No replacement motor driver.

Location references preserve their ORIGINAL timestamp. A cached observation is not
re-stamped as a fresh camera observation. E_UNVERIFIED remains unknown.
"""
from __future__ import annotations
import os,threading
from types import SimpleNamespace
from .types import Denied,Missing
from .models import validate_loopback
from .simulation import Result

class MemoryView:
    def __init__(self,store):self.store=store
    def object_state(self,oid):
        o=next((x for x in self.store.objects() if x['id']==oid),None)
        if not o:return None
        return SimpleNamespace(tag_id=o['tag'],last_zone=o['last_zone'],stale=not o['fresh'],name=o['name'])

class GuardedUpstream:
    """Adapter-side half of the STOP interlock. One lock serializes the stop latch, reset and motion submission, and the
    latch is checked under it at submission: once stop() returns, no motion command can still go out, and a reset
    that races a newer STOP cannot clear it. stop() first interrupts a command already submitted (without waiting
    for the lock that submission holds), then latches, then stops again for anything submitted in between."""
    def __init__(self,upstream,store):
        self.upstream,self.store=upstream,store;self._lock=threading.Lock();self._stopped=threading.Event();self.stop_generation=0
    def state(self):return self.upstream.state()
    def stop(self):
        try:self.upstream.stop()
        finally:
            with self._lock:
                self.stop_generation+=1;self._stopped.set()
                self.upstream.stop()
    def reset_stop(self):
        with self._lock:self.upstream.reset_stop();self._stopped.clear()
    def execute(self,op,args):
        from .planning import PHYSICAL
        if op in PHYSICAL:
            with self._lock:
                if self._stopped.is_set():return Result('rejected','stop latched at the adapter; motion refused until the stop is cleared')
                return self._checked(self.upstream.execute(op,args))
        if op=='locate_tag':
            o=next((x for x in self.store.objects() if x['id']==args['object_id']),None)
            if not o or not o['fresh'] or not o['last_observation_id']:return Result('failed','no current source-grounded location')
            row=self.store.one('SELECT payload FROM observation WHERE id=?',(o['last_observation_id'],))
            import json
            payload=json.loads(row['payload']) if row else {}
            if payload.get('mode')!='PHYSICAL':return Result('failed','location is not a physical sensor observation')
            r=Result('done','reference to a current sensor observation, not a new camera capture')
            r.location_evidence={o['id']:{'observation_id':o['last_observation_id'],'zone':o['last_zone'],'captured_at':o['last_seen_at']}}
            return r
        return self._checked(self.upstream.execute(op,args))
    @staticmethod
    def _checked(r):
        if 'not sensor checked' in getattr(r,'detail','').lower() or 'E_UNVERIFIED' in getattr(r,'detail',''):
            return Result('unknown','Adapter reported an unverified physical effect; operator reconciliation required.')
        return r

def upstream_robot(store,arena):
    if os.environ.get('CORTEX_LIVE_ACK')!='WITNESSED_STOP_AND_SINGLE_EXECUTOR':
        raise Denied('live startup requires the witnessed-stop and single-executor acknowledgment')
    url=validate_loopback(os.environ.get('ENGRAM_ROBOT_URL','http://127.0.0.1:8200'))
    keyfile=os.environ.get('ENGRAM_ROBOT_KEY_FILE')
    if not keyfile:raise Missing('existing robot secret file is not configured')
    # Import the reviewed existing adapter, never embed or duplicate its secret.
    from ..robot.brian8200 import BrianRobot,read_secret
    upstream=BrianRobot(url,MemoryView(store),read_secret(keyfile),{zone:int(tag) for tag,zone in arena.get('zone_tags',{}).items()})
    return GuardedUpstream(upstream,store)
