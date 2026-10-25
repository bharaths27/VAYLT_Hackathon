"""Server-side compatibility for Verna's console, not a browser credential.

Opt in using CORTEX_BRIDGE=1 after review. Both planning and memory reads go to
ONE local Cortex API. Do not create a second memory authority or raw motor route.
"""
from __future__ import annotations
import os
import httpx
from .models import validate_loopback
from .types import Denied,Missing


def authorized_client(url:str,*,timeout:float=30,transport=None):
    validate_loopback(url)
    key=os.environ.get('CORTEX_API_TOKEN','')
    if len(key)<32:raise Denied('configure the console server operator credential')
    return httpx.AsyncClient(timeout=timeout,headers={'Authorization':'Bearer '+key},transport=transport,follow_redirects=False)

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
        from urllib.parse import quote
        return await self._get('/evidence/'+quote(eid,safe=''))
    async def list_skills(self):return await self._get('/skills')
    async def list_prospective(self):return await self._get('/prospective')
    async def add_rule(self,text,fields=None):
        if fields:raise Missing('structured bindings require explicit reviewed source/schema input; do not silently discard fields')
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
        raise Missing('skill publication must carry the exact inspected digest; use Cortex inspector/API')
    async def approve_skill(self,sid,digest):return await self._post('/skills/'+sid+'/approve',{'digest':digest})
