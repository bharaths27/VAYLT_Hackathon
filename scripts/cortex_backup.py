"""Consistent SQLite backup of an existing vault; never overwrites a destination, nor writes through one."""
import argparse,os,sqlite3,tempfile
from pathlib import Path

def backup(source:Path,destination:Path):
    source=source.resolve(strict=True)
    if source==destination.resolve():raise ValueError('source and destination must differ')
    # The consistent copy is taken and checked in memory: SQLite never opens a destination path.
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);copy=sqlite3.connect(':memory:')
    try:
        src.backup(copy)
        if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('backup integrity check failed')
        data=copy.serialize()
    finally:
        src.close();copy.close()
    # Published in one step that never replaces: staged in a private directory beside the destination (mode 0700), written
    # through the descriptor that created the file exclusively (never reopened by name), then hard-linked into place.
    # link() fails if anything already exists there, a symlink swapped in included, and nothing half-written is visible.
    stage=tempfile.mkdtemp(prefix='.cortex-backup-',dir=destination.parent)
    staged=os.path.join(stage,'backup.db')
    try:
        fd=os.open(staged,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0),0o600)
        try:
            view=memoryview(data)
            while view:view=view[os.write(fd,view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(staged,destination)
    finally:
        if os.path.lexists(staged):os.unlink(staged)
        os.rmdir(stage)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('destination',type=Path);a=p.parse_args();backup(a.source,a.destination);print('Backup integrity: ok. Original unchanged.')
