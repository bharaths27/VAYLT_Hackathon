"""Generate/apply the SMALL console auth/memory bridge diff against exact anchors.

Default: print diff only. --apply writes only two files, saves .pre-cortex backups.
Never modifies voice.py/voice.js/speech_epoch or changes a deployed service.
"""
from pathlib import Path
import argparse,difflib

def transforms(root):
    paths={
      'console/server/brain.py':[
        ('import time\n','import time\nimport os\nfrom engram.cortex.console_adapter import authorized_client\n'),
        ('_c = httpx.AsyncClient(timeout=20.0) if URL else None',
         '_c = (authorized_client(URL, timeout=60.0) if os.environ.get("CORTEX_BRIDGE") == "1" else httpx.AsyncClient(timeout=20.0)) if URL else None')],
      'console/server/adapters/memory.py':[
        ('def make_memory():\n', 'def make_memory():\n    import os\n    if os.environ.get("CORTEX_BRIDGE") == "1":\n        from engram.cortex.console_adapter import AsyncCortexMemory\n        return AsyncCortexMemory(config.BRAIN_URL)\n')]
    }
    out=[]
    for path,replacements in paths.items():
        old=(root/path).read_text();new=old
        for before,after in replacements:
            if after in new:continue
            if new.count(before)!=1:raise ValueError('Review changed upstream anchor: '+path)
            new=new.replace(before,after,1)
        out.append((path,old,new))
    return out

def main():
    p=argparse.ArgumentParser();p.add_argument('--repo',type=Path,default=Path('.'));p.add_argument('--apply',action='store_true');a=p.parse_args()
    changed=transforms(a.repo)
    for path,old,new in changed:
        print(''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/'+path,tofile='b/'+path)),end='')
    if a.apply:
        for path,old,new in changed:
            if old==new:continue
            target=a.repo/path; backup=target.with_suffix(target.suffix+'.pre-cortex')
            if backup.exists():raise FileExistsError('Refusing to overwrite backup '+str(backup))
        for path,old,new in changed:
            if old!=new:
                target=a.repo/path;target.with_suffix(target.suffix+'.pre-cortex').write_text(old);target.write_text(new)
if __name__=='__main__':main()
