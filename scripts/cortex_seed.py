"""Load reviewed synthetic knowledge into an inactive MOCK review vault, never live."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from engram.cortex.store import Store
from engram.cortex.seeding import load_seed
from engram.cortex import vault_lock
from engram.cortex.types import Denied

def main():
    p=argparse.ArgumentParser();p.add_argument('--db',type=Path,required=True);p.add_argument('--arena',type=Path,default=ROOT/'fixtures/cortex/arena.json');p.add_argument('--seed',type=Path,default=ROOT/'fixtures/cortex/memory_seed.json');p.add_argument('--reviewed-by',required=True);a=p.parse_args()
    with vault_lock.acquire(str(a.db)):
        s=Store(a.db)
        try:
            if s.one("SELECT 1 FROM cx_episode WHERE mode='PHYSICAL'") or s.one("SELECT 1 FROM cx_manifest WHERE id='runtime' AND payload_json LIKE '%PHYSICAL%'"):
                raise Denied('refusing to seed a vault with physical runtime history')
            s.register_arena(json.loads(a.arena.read_text()))
            print(json.dumps(load_seed(s,json.loads(a.seed.read_text()),principal=a.reviewed_by),indent=2))
        finally:s.close()
if __name__=='__main__':main()
