"""Reviewer helper (copied into each workspace as REVIEW/harness.py).

The orchestrator already ran baseline/fixed/suite/mutation for this gate; results are in
REVIEW/evidence/. These helpers only make the reviewer's own experiments convenient.
Anything recorded here is re-verified by the program in a fresh copy afterwards.

  test <copy-dir>                    run the focused tests in a copy (shared env, correct PYTHONPATH)
  probe-start <name> [--on baseline] disposable copy (default: full patch applied) to break on purpose
  probe-run <name>                   record the mutation to REVIEW/out/probes/<name>.diff and run tests
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REVIEW = Path(__file__).resolve().parent
WORK = REVIEW.parent
RUNS = WORK / ".review-runs"
PROBES = REVIEW / "out" / "probes"


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    if p.returncode:
        sys.exit(f"git {' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode(errors="replace")


def env_python():
    envs = REVIEW / "envs.json"
    if envs.is_file():
        data = json.loads(envs.read_text(encoding="utf-8"))
        return next(iter(data.values()))["python"]
    return sys.executable


def run_focused(dst):
    dst = Path(dst).resolve()
    py = env_python()
    checks = json.loads((REVIEW / "checks.json").read_text(encoding="utf-8"))
    env = os.environ.copy()
    paths = [str(dst)] + ([str(dst / "src")] if (dst / "src").is_dir() else [])
    env.update(PYTHONPATH=os.pathsep.join(paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])),
               PYTHONDONTWRITEBYTECODE="1")
    worst = 0
    for template in checks.get("prepare", []):
        argv = [x.replace("{py}", py).replace("{work}", str(dst)) for x in template]
        subprocess.run(argv, cwd=dst, env=env, check=True)
    for template in checks["focused"]:
        argv = [x.replace("{py}", py).replace("{work}", str(dst)) for x in template]
        print("$", subprocess.list2cmdline(argv), flush=True)
        code = subprocess.run(argv, cwd=dst, env=env).returncode
        print(f"exit={code}")
        worst = worst or code
    return worst


def probe_start(name, on):
    dst = RUNS / f"probe-{name}"
    if dst.exists():
        subprocess.run(["git", "-C", str(WORK), "worktree", "remove", "--force", str(dst)], capture_output=True)
        shutil.rmtree(dst, ignore_errors=True)
    RUNS.mkdir(exist_ok=True)
    git(WORK, "-c", "core.autocrlf=false", "worktree", "add", "-q", "--detach", str(dst), "HEAD")
    git(dst, "config", "core.autocrlf", "false")
    git(dst, "apply", "--whitespace=nowarn", str(REVIEW / ("fix.diff" if on == "fixed" else "tests.diff")))
    git(dst, "add", "-A")
    PROBES.mkdir(parents=True, exist_ok=True)
    (PROBES / f"{name}.json").write_text(json.dumps(dict(on=on, start_tree=git(dst, "write-tree").strip())),
                                         encoding="utf-8")
    print(f"Edit implementation files (not tests) in: {dst}\nthen: probe-run {name}")


def probe_run(name):
    meta = json.loads((PROBES / f"{name}.json").read_text(encoding="utf-8"))
    dst = RUNS / f"probe-{name}"
    git(dst, "add", "-A")
    # Raw bytes: text mode on Windows would turn LF into CRLF and break `git apply`.
    diff = subprocess.run(["git", "-C", str(dst), "diff", "--cached", "--binary", meta["start_tree"]],
                          capture_output=True, check=True).stdout
    (PROBES / f"{name}.diff").write_bytes(diff)
    if not diff.strip():
        sys.exit("empty mutation: edit the copy first")
    code = run_focused(dst)
    print(f"probe {name}: {'detected (tests fail)' if code else 'NOT detected (tests pass)'}; "
          "the orchestrator will re-verify it in a fresh copy.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test")
    t.add_argument("copy")
    s = sub.add_parser("probe-start")
    s.add_argument("name")
    s.add_argument("--on", choices=("fixed", "baseline"), default="fixed")
    r = sub.add_parser("probe-run")
    r.add_argument("name")
    a = p.parse_args()
    if getattr(a, "name", "x").replace("-", "").replace("_", "").isalnum() is False:
        sys.exit("probe name: letters, digits, - and _ only")
    if a.cmd == "test":
        sys.exit(run_focused(a.copy))
    if a.cmd == "probe-start":
        probe_start(a.name, a.on)
    else:
        probe_run(a.name)


if __name__ == "__main__":
    main()
