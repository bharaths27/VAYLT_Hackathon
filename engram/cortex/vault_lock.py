"""One process owns a vault: an exclusive, non-blocking OS lock on `<db>.cortex.lock`.

The lock is named from the CANONICAL database path (symlinks resolved), so a vault opened
through an alias contends for the same lock as one opened through its target.

fcntl on Linux/macOS (the Pi), msvcrt on Windows, so the same tests run on a developer laptop.
The OS drops the lock when the handle closes or the process dies.
"""
from __future__ import annotations

import os

from .types import Conflict


def acquire(db_path: str):
    handle=open(os.path.realpath(db_path)+'.cortex.lock','a+')
    try:
        if os.name=='nt':
            import msvcrt
            handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close(); raise Conflict('another Cortex process owns this vault') from None
    return handle
