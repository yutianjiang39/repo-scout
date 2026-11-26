"""Policy lock: instruction files must not drift.

`policy/snapshot/` holds the user-approved copy of every protected instruction file and
`policy/lock.json` their SHA-256. Every orchestrate.py / gate.py run calls `enforce()`:
any protected file that differs from the snapshot is restored and the event is logged.
Changing a rule = edit the file, then the USER runs `lock` with their own words; models
must never run `lock`. The snapshot is also pushed to GitHub, so tampering with the
snapshot itself is visible in git history.

  python -m ro.policy check             # list drift, exit 1 if any
  python -m ro.policy restore           # put approved text back
  python -m ro.policy lock "<用户原话>"   # user only: approve current text
"""

import json
import os
import stat
import sys
import time
from pathlib import Path

from .util import ROOT, sha

POLICY = ROOT / "policy"
SNAP = POLICY / "snapshot"
LOCK = POLICY / "lock.json"
LOG = POLICY / "events.log"


def workspace():
    return Path(os.environ.get("REVIEW_WORKSPACE", ROOT.parent.parent)).resolve()


def targets():
    """Protected files: snapshot name -> live path."""
    ws = workspace()
    out = {
        "workspace/AGENTS.md": ws / "AGENTS.md",
        "workspace/opencode.jsonc": ws / "opencode.jsonc",
        "repo-scout/AGENTS.md": ws / "projects" / "repo-scout" / "AGENTS.md",
        "pipeline/AGENTS.md": ROOT / "AGENTS.md",
        "pipeline/config/reviewers.json": ROOT / "config" / "reviewers.json",
    }
    for sub in ("capsule", "prompts"):
        for p in sorted((ROOT / sub).glob("*")):
            if p.is_file():
                out[f"pipeline/{sub}/{p.name}"] = p
    return out


def _log(msg):
    POLICY.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{msg}\n")


def _writable(p, on):
    if p.exists():
        os.chmod(p, (stat.S_IWRITE | stat.S_IREAD) if on else stat.S_IREAD)


def drift():
    if not LOCK.is_file():
        return []
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    out = []
    for name, live in targets().items():
        want = lock["files"].get(name)
        snap = SNAP / name
        if want is None:
            out.append((name, "unlocked"))
        elif not snap.is_file() or sha(snap.read_bytes()) != want:
            out.append((name, "snapshot-tampered"))
        elif not live.is_file() or sha(live.read_bytes()) != want:
            out.append((name, "changed"))
    return out


def restore(log=print):
    fixed = []
    for name, why in drift():
        if why != "changed":
            continue
        live = targets()[name]
        _writable(live, True)
        live.write_bytes((SNAP / name).read_bytes())
        _writable(live, False)
        fixed.append(name)
        _log(f"restored\t{name}")
        log(f"policy: restored {name} to the approved text")
    return fixed


def enforce(log=print):
    """Called at every CLI entry. Restores drift; refuses if the snapshot itself was altered."""
    if not LOCK.is_file():
        return
    restore(log)
    bad = [n for n, why in drift() if why == "snapshot-tampered"]
    if bad:
        raise SystemExit(f"policy snapshot altered: {bad}. Re-run `python -m ro.policy lock` as the user.")


def lock(quote):
    if not quote.strip():
        raise SystemExit("lock needs the user's own words")
    files = {}
    for name, live in targets().items():
        if not live.is_file():
            continue
        data = live.read_bytes()
        dst = SNAP / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        _writable(dst, True)
        dst.write_bytes(data)
        files[name] = sha(data)
        _writable(live, False)
    LOCK.write_text(json.dumps(dict(files=files, approved_by=quote, at=time.strftime("%Y-%m-%d %H:%M:%S")),
                               ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"locked\t{len(files)} files\t{quote}")
    return files


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "check"
    if cmd == "lock":
        print(f"locked {len(lock(' '.join(argv[1:])))} files")
    elif cmd == "restore":
        print(f"restored {len(restore())} files")
    else:
        d = drift()
        for name, why in d:
            print(why, name)
        print(f"{len(d)} drift(s)")
        return 1 if d else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
