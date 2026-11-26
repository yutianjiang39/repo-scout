"""Artifact manager: freeze a candidate once into an immutable review capsule."""

import json
from pathlib import Path

from . import config
from .util import ROOT, dump, git, git_text, need, sha

CAPSULE = ROOT / "capsule"  # files shipped into every reviewer workspace
# Files never shown in the first round (final-round material / templates).
NOT_IN_MANIFEST = {"final.md", "stage.md"}


def validate_checks(data):
    c = json.loads(data)

    def argv_list(key):
        v = c.get(key, [])
        return isinstance(v, list) and all(
            isinstance(x, list) and x and all(isinstance(y, str) for y in x) for x in v
        )

    for k in ("prepare", "focused", "suite"):
        need(argv_list(k), f"checks.json: {k} must be a list of argv lists")
    need(c.get("focused"), "checks.json: focused commands are required")
    need(isinstance(c.get("envs", {}), dict), "checks.json: envs must be an object")
    m = c.get("mutation", {})
    need(isinstance(m, dict), "checks.json: mutation must be an object")
    return c


def _common(L, prior, req, commands, flags, kind):
    s = L.s
    s.update(kind=kind, gates=config.parse_gates(flags.get("gates")), first={}, final={},
             sessions={}, attempts={}, workspaces={}, evidence={}, reports=[], version=L.v)
    changed = []
    for name, path in (("requirement.md", req), ("commands.md", commands)):
        text = Path(path).read_bytes()
        need(text.strip(), "Empty task input: " + name)
        old = L.get(name, L.v - 1) if L.v > 1 else None
        same_task = (prior.get("kind"), prior.get("repo")) == (kind, s.get("repo"))
        if same_task and old is not None and old != text:
            changed.append(name)
        L.put(name, text)
    # Blind inputs must not silently absorb reviewer feedback between versions.
    need(not changed or flags.get("input-change", "").strip(),
         f"{changed} changed since version {L.v - 1}; pass --input-change=<reason>"
         " (never paste reviewer findings into blind inputs)")
    if changed:
        s["input_change"] = dict(files=changed, reason=flags["input-change"])
    protocol = "artifact.md" if kind == "artifact" else "protocol.md"
    for name, src in (("PROTOCOL.md", protocol), ("contract.md", "report.md"),
                      ("final.md", "final.md"), ("stage.md", "stage.md")):
        L.put(name, (CAPSULE / src).read_bytes())
    if kind == "code":
        L.put("harness.py", (CAPSULE / "harness.py").read_bytes())
    manifest = dict(version=L.v, kind=kind, patch=s["patch"], base=s.get("base"), tree=s.get("tree"),
                    gates=s["gates"],
                    files={n: sha(L.get(n)) for n in sorted(L.names()) if n not in NOT_IN_MANIFEST})
    L.put("MANIFEST.json", dump(manifest))
    s["manifest"] = sha(L.get("MANIFEST.json"))


def freeze_code(L, prior, a, flags):
    repo, base, tip, req, commands, implementer, *testpaths = a
    need(testpaths and implementer.strip(), "Need implementer session ID and exact test paths")
    need(flags.get("checks"), "Code candidates need --checks=checks.json (program-run verification)")
    checks = Path(flags["checks"]).read_bytes()
    validate_checks(checks)
    repo = Path(repo).resolve()
    base, tip = [git_text(repo, "rev-parse", "--verify", "--end-of-options", x + "^{commit}") for x in (base, tip)]
    git(repo, "merge-base", "--is-ancestor", base, tip)
    opts = ["--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv", base, tip]
    paths = git(repo, "diff", "--name-only", "--no-renames", "-z", base, tip).decode().rstrip("\0").split("\0")
    need(set(testpaths) <= set(paths) and set(paths) - set(testpaths),
         "Specify changed test files and at least one implementation file")
    patch = git(repo, "diff", *opts)
    L.v += 1
    L.s = dict(repo=str(repo), base=base, tip=tip, tree=git_text(repo, "rev-parse", base + "^{tree}"),
               patch=sha(patch), implementer=implementer)
    L.put("fix.diff", patch)
    L.put("tests.diff", git(repo, "--literal-pathspecs", "diff", *opts, "--", *testpaths))
    L.put("code.diff", git(repo, "--literal-pathspecs", "diff", *opts, "--",
                           *[p for p in paths if p not in testpaths]))
    L.put("checks.json", checks)
    _common(L, prior, req, commands, flags, "code")


def freeze_artifact(L, prior, a, flags):
    candidate, req, commands, implementer = a
    data = Path(candidate).read_bytes()
    need(data and implementer.strip(), "Need candidate and implementer session")
    L.v += 1
    L.s = dict(filename="candidate" + Path(candidate).suffix, patch=sha(data), implementer=implementer)
    L.put(L.s["filename"], data)
    _common(L, prior, req, commands, flags, "artifact")
