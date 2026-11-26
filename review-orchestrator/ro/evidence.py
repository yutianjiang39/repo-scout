"""Evidence manager: program-owned evidence store, sealing, isolation audit, receipts."""

import json
import os
from pathlib import Path
import shutil
import stat

from . import envs as envmod
from .util import dump, rmtree


def store_dir(task_dir, version, role, attempt, kind="evidence"):
    return Path(task_dir) / kind / f"v{version}-{role}-{attempt[:8]}"


def write(ev, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evidence.json").write_bytes(dump(ev))
    return out_dir / "evidence.json"


def publish_copy(out_dir, work):
    """Read-only copy for the reviewer. The ledger keeps the hash of the original."""
    dst = Path(work) / "REVIEW" / "evidence"
    rmtree(dst)
    shutil.copytree(out_dir, dst)
    for p in dst.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IREAD)
    return dst


def seal(work, sealed_dir):
    """Move a finished gate's working material out of reach of later reviewers."""
    work, sealed_dir = Path(work), Path(sealed_dir)
    sealed_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("out", "evidence"):
        src = work / "REVIEW" / sub
        if src.exists():
            for p in src.rglob("*"):
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            dest = sealed_dir / sub
            rmtree(dest)
            shutil.move(str(src), str(dest))
    envmod.drop_worktrees(work)


def collect_probes(work):
    """Reviewer-designed wrong implementations: REVIEW/out/probes/<name>.diff (+ .json {on})."""
    d = Path(work) / "REVIEW" / "out" / "probes"
    out = {}
    if d.is_dir():
        for diff in sorted(d.glob("*.diff")):
            meta = diff.with_suffix(".json")
            on = "fixed"
            if meta.is_file():
                try:
                    on = json.loads(meta.read_text(encoding="utf-8")).get("on", "fixed")
                except ValueError:
                    pass
            out[diff.stem] = dict(diff=diff, on=on if on in ("fixed", "baseline") else "fixed")
    return out


def _variants(path):
    p = str(Path(path).resolve())
    fwd = p.replace("\\", "/")
    forms = {p, fwd, p.replace("\\", "\\\\")}
    if len(fwd) > 2 and fwd[1] == ":":
        forms.add("/" + fwd[0].lower() + fwd[2:])  # git-bash /c/...
    return {f.lower() for f in forms if len(f) > 3}


def audit(text, forbidden):
    """Paths outside this gate's workspace that the transcript references."""
    low = text.lower()
    hits = []
    for path in forbidden:
        if any(v in low for v in _variants(path)):
            hits.append(str(path))
    return hits


def observed_models(text):
    """Model identifiers reported by the CLI itself (Claude modelUsage, OpenCode modelID)."""
    found = set()
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        stack = [obj]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if isinstance(o.get("modelUsage"), dict):
                    found.update(o["modelUsage"])
                for k in ("modelID", "model_id"):
                    if isinstance(o.get(k), str):
                        prov = o.get("providerID")
                        found.add(f"{prov}/{o[k]}" if isinstance(prov, str) else o[k])
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    return sorted(found)


def model_matches(expected, observed):
    base = expected.split("#", 1)[0]
    short = base.split("/", 1)[-1]
    return any(o == base or o.split("/", 1)[-1] == short or o.startswith(short) for o in observed)
