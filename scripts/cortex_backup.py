"""Consistent SQLite backup of an existing vault; never overwrites a destination."""
import argparse,os,sqlite3
from pathlib import Path

def backup(source:Path,destination:Path):
    source=source.resolve(strict=True)
    if source==destination.resolve():raise ValueError('source and destination must differ')
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    src=dst=None
    try:
        src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);dst=sqlite3.connect(destination)
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('backup integrity check failed')
    except Exception:
        if dst:dst.close();dst=None
        destination.unlink(missing_ok=True);raise
    finally:
        if src:src.close()
        if dst:dst.close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('destination',type=Path);a=p.parse_args();backup(a.source,a.destination);print('Backup integrity: ok. Original unchanged.')
