"""Print the SMALL console memory bridge diff against exact anchors, for review only.

BLOCKED: --apply refuses. The bridge attaches the server-side operator credential to
every Cortex call, and the console's own mutating routes (8000/8443) take no credential,
so applying it would make every LAN client the Cortex operator. See
docs/CORTEX_VALIDATION.md CXV6 and docs/CORTEX_HANDOFF.md. Never modifies
voice.py/voice.js/speech_epoch or a deployed service.

The generated brain.py imports engram.cortex lazily, only when CORTEX_BRIDGE=1, so the
console still starts from console/ (scripts/run.sh) without the repository root on PYTHONPATH.
"""
from pathlib import Path
import argparse,difflib,sys

BLOCKED=('cortex_console_patch --apply is blocked until the console authenticates its mutating routes; '
         'see docs/CORTEX_VALIDATION.md CXV6. Nothing was written. The diff above is for review only.')
LAZY_CLIENT=('if URL and os.environ.get("CORTEX_BRIDGE") == "1":  # lazy: only the opted-in bridge needs engram on sys.path\n'
             '    from engram.cortex.console_adapter import authorized_client\n'
             '    _c = authorized_client(URL, timeout=60.0)\n'
             'else:\n'
             '    _c = httpx.AsyncClient(timeout=20.0) if URL else None\n')
STATUS=('                 "refused": "FAILED"}.get(outcome, "NEEDS_VERIFICATION")\n')
LABEL=('    if os.environ.get("CORTEX_BRIDGE") == "1":  # Cortex says SIMULATED or PHYSICAL; a simulated run is never VERIFIED\n'
       '        from engram.cortex.console_adapter import console_status\n'
       '        op.mode, op.status = res.get("mode"), console_status(res)\n')

def transforms(root):
    paths={
      'console/server/brain.py':[
        ('import time\n','import os\nimport time\n'),
        ('_c = httpx.AsyncClient(timeout=20.0) if URL else None\n',LAZY_CLIENT),
        ('        self.task = None\n','        self.task = None\n        self.mode = p.get("mode")\n'),
        ('"bundle_id": self.bundle_id, "brain": URL}','"bundle_id": self.bundle_id, "brain": URL, "mode": self.mode}'),
        (STATUS,STATUS+LABEL)],
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
    p=argparse.ArgumentParser();p.add_argument('--repo',type=Path,default=Path('.'));p.add_argument('--apply',action='store_true',help='refused: see module docstring');a=p.parse_args()
    for path,old,new in transforms(a.repo):
        print(''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/'+path,tofile='b/'+path)),end='')
    if a.apply:print(BLOCKED,file=sys.stderr);return 2
    return 0
if __name__=='__main__':sys.exit(main())
