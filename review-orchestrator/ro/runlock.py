"""One running orchestrate.py per repository.

Tasks live in repos/<repo>/tasks/<task>/review.sqlite; the lock is repos/<repo>/tasks/.running.lock
(or <task>/.running.lock for a ledger elsewhere). It is an OS file lock, so it disappears with the
process: no stale locks after a crash or kill. A second run is refused with who holds it.
"""

import json
import os
from pathlib import Path
import time

from .util import Refused

OFFSET = 1 << 20  # lock a byte past the info text so the holder's info stays readable


def path_for(db):
    task = Path(db).resolve().parent
    return (task.parent if task.parent.name == "tasks" else task) / ".running.lock"


def _lock(fh):
    fh.seek(OFFSET)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.lockf(fh, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, OFFSET)


def acquire(db, cmd):
    """Returns the open handle (keep it alive for the whole run)."""
    p = path_for(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+b")
    try:
        _lock(fh)
    except OSError:
        fh.seek(0)
        try:
            who = json.loads(fh.read(4096).decode("utf-8", "replace") or "{}")
        except ValueError:
            who = {}
        fh.close()
        raise Refused(f"another review is running for this repository: task {who.get('task', '?')} "
                      f"({who.get('cmd', '?')}, pid {who.get('pid', '?')}, since {who.get('since', '?')}). "
                      "Wait for it or stop that process; only one review per repository runs at a time.")
    info = dict(task=Path(db).resolve().parent.name, cmd=cmd, pid=os.getpid(),
                since=time.strftime("%Y-%m-%d %H:%M:%S"))
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(info, ensure_ascii=False).encode("utf-8"))
    fh.flush()
    return fh
