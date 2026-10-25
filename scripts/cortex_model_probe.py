#!/usr/bin/env python3
"""Actual model diagnostic: synthetic memory only, disposable DB, no motor API.
Run on the Pi with its existing llama server. Keys are accepted ONLY via env.
Errors are failures, never substituted inference results. No model installation.
"""
import argparse,hashlib,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from engram.cortex.models import QwenClient,IntentAdvisor
from engram.cortex.store import Store
from engram.cortex.planning import Compiler,verify
from engram.cortex.types import Goal

def fingerprint(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        while chunk:=f.read(1024*1024):h.update(chunk)
    return h.hexdigest()
def main():
    a=argparse.ArgumentParser();a.add_argument('--qwen',action='store_true');a.add_argument('--intent',action='store_true');a.add_argument('--url',default='http://127.0.0.1:8080');a.add_argument('--model-file',type=Path);a.add_argument('--out',type=Path,default=Path('cortex-model-probe.json'));args=a.parse_args()
    if not (args.qwen or args.intent):a.error('select --qwen and/or --intent')
    out={'evidence_class':'SYNTHETIC_TEST','physical_actions':0,'qwen':[],'intent':[]}
    s=Store();s.register_arena(json.loads((ROOT/'fixtures/cortex/arena.json').read_text()))
    source=s.teach('Probe rule','Safety goggles must be at the ppe station before handling Sample B.',owner='probe',cloud_ok=False)
    if args.model_file:out['model_sha256_before']=fingerprint(args.model_file)
    if args.qwen:
        q=QwenClient(endpoint=args.url,timeout_s=60,input_budget=3072)
        for zone in ['ppe_station','shelf_b']:
            if zone=='shelf_b':s.teach('Probe corrected rule','Safety goggles must be at shelf B before handling Sample B.',owner='probe',supersedes=source['source_id'])
            g=Goal('fetch',('sample_b',),'bench');beg=time.perf_counter()
            try:
                r=q.complete('plan',s.bundle('Bring Sample B to bench',g),goal=g.to_dict())
                v=verify(g,r.value['steps'],Compiler(s).fixture(),s.related_rules(g),s.policy)
                out['qwen'].append({'status':'response_received','correct':v.ok,'expected_prerequisite_zone':zone,'errors':v.errors,'result':r.value,'model':r.model,'input_tokens':r.input_tokens,'elapsed_ms':r.elapsed_ms,'contract_sha256':r.contract_hash,'prompt_sha256':r.prompt_hash})
            except Exception as e:out['qwen'].append({'status':'failed','exception':type(e).__name__,'request_elapsed_ms':round((time.perf_counter()-beg)*1000,3),'note':'Failure duration is not inference latency'})
    if args.intent:
        j=IntentAdvisor(enabled=True,max_calls=3)
        for utterance in ['Open the claw.','Do not open the claw.']:
            out['intent'].append({'synthetic_input':utterance,'result':j.route(utterance,disclosure_allowed=True)})
        out['intent'].append(j.turn({'utterance':'Open the claw after the timer finishes.','is_final':True},disclosure_allowed=True))
    if args.model_file:
        out['model_sha256_after']=fingerprint(args.model_file)
        out['model_artifact_unchanged']=out['model_sha256_before']==out['model_sha256_after']
        out['fingerprint_limit']='File identity alone does not verify that the running process loaded this exact artifact.'
    s.close();args.out.write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out,indent=2))
if __name__=='__main__':main()
