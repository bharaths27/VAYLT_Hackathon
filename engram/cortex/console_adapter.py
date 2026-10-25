"""Server-side compatibility for Verna's console, not a browser credential.

BLOCKED for deployment: the console's own mutating routes take no credential, so this
server-side operator credential would serve every LAN client (scripts/cortex_console_patch.py
refuses --apply; docs/CORTEX_HANDOFF.md lists the blockers). When unblocked, opt in with
CORTEX_BRIDGE=1. Planning and memory reads go to ONE local Cortex API. Do not create a
second memory authority or raw motor route. Refusals reach the console as 4xx, not 500.
"""
from __future__ import annotations
import os
import httpx
from fastapi import HTTPException
from .models import validate_loopback
from .types import Denied,Missing


def authorized_client(url:str,*,timeout:float=30,transport=None):
    validate_loopback(url)
    key=os.environ.get('CORTEX_API_TOKEN','')
    if len(key)<32:raise Denied('configure the console server operator credential')
    return httpx.AsyncClient(timeout=timeout,headers={'Authorization':'Bearer '+key},transport=transport,follow_redirects=False)

def console_status(res:dict)->str:
    """Cortex outcome -> console card status. Only an attested PHYSICAL success is VERIFIED;
    a SIMULATED run is labeled SIMULATED, and an unknown mode is never promoted."""
    o=str(res.get('outcome') or res.get('status') or 'unknown').lower()
    if o=='success':return {'PHYSICAL':'VERIFIED','SIMULATED':'SIMULATED'}.get(res.get('mode'),'NEEDS_VERIFICATION')
    return {'failed':'FAILED','rejected':'FAILED','aborted':'STOPPED','cancelled':'CANCELLED','expired':'EXPIRED'}.get(o,'NEEDS_VERIFICATION')

class AsyncCortexMemory:
    def __init__(self,base:str,*,transport=None):
        self.base=validate_loopback(base)
        self.c=authorized_client(self.base,transport=transport)
    async def close(self):await self.c.aclose()
    async def _get(self,path):
        r=await self.c.get(self.base+path);r.raise_for_status();return r.json()
    async def _post(self,path,body):
        r=await self.c.post(self.base+path,json=body);r.raise_for_status();return r.json()
    async def query(self,goal,scene=None):
        return await self._post('/memory/query',{'goal':goal})
    async def evidence(self,eid):
        # None lets the console fall back to its own ledger/frames for ids Cortex never held.
        from urllib.parse import quote
        r=await self.c.get(self.base+'/evidence/'+quote(eid,safe=''))
        if r.status_code==404 or (r.status_code==409 and r.json().get('code')==Missing.code):return None
        r.raise_for_status();return r.json()
    async def list_skills(self):return await self._get('/skills')
    async def list_prospective(self):return await self._get('/prospective')
    async def add_rule(self,text,fields=None):
        if fields:raise HTTPException(422,'zone/tag/trigger fields are not bridged; send the plain sentence or teach a reviewed source in the Cortex inspector')
        return await self._post('/memory/sources',{'title':'Console instruction','text':text,'kind':'spoken'})
    async def add_prospective(self,task):
        request=task.get('request') or task.get('text')
        if task.get('due_at') is not None:
            return await self._post('/prospective/timed',{'request':request,'due_at':float(task['due_at'])})
        return await self._post('/prospective',{'request':request,'trigger':task['trigger']})
    async def record_episode(self,episode):
        raise Denied('only the canonical executor records outcomes; no client-supplied success')
    async def set_skill_status(self,sid,status):
        if status in {'REVOKED','REJECTED'}:return await self._post('/skills/'+sid+'/revoke',{})
        raise HTTPException(409,'skill publication must carry the exact inspected digest; approve it in the Cortex inspector')
    async def approve_skill(self,sid,digest):return await self._post('/skills/'+sid+'/approve',{'digest':digest})
