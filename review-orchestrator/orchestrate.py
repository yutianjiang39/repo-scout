"""Review Orchestrator CLI. The program runs the workflow; reviewers only judge.

  start    freeze candidate once, build shared envs once, run first-round gates in order
  env      build/reuse shared dependency envs for the frozen base
  run      continue first-round gates from the ledger state
  recheck  send counter-evidence to the objecting reviewer (patch unchanged), then continue
  final    seal deliverable, run every participant's final round (parallel by default)
  status   phase, hashes and verdicts only (never report contents)

Exit codes: 0 done/PASS, 1 BLOCKED, 2 interrupted gate, 3 REVISE.
"""

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from ro.orchestrator import Task
from ro.util import ROOT, Refused


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, required=True, help="task ledger (one per task; its folder holds evidence)")
    p.add_argument("--work-root", type=Path, help="where reviewer workspaces go (outside the repo)")
    p.add_argument("--cache", type=Path, default=ROOT / ".cache" / "envs")
    p.add_argument("--timeout", type=int, help="seconds per reviewer CLI (default none; Gemini can be slow)")
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start")
    for k in ("repo", "base", "tip", "requirement", "commands", "checks", "implementer"):
        st.add_argument("--" + k, required=True)
    st.add_argument("--tests", nargs="+", required=True, help="changed test file paths")
    st.add_argument("--gates", default="gpt,claude")
    st.add_argument("--input-change", default="")
    sub.add_parser("env")
    sub.add_parser("run")
    rc = sub.add_parser("recheck")
    rc.add_argument("role")
    rc.add_argument("counter_evidence")
    f = sub.add_parser("final")
    f.add_argument("deliverable")
    f.add_argument("--serial", action="store_true")
    sub.add_parser("status")
    a = p.parse_args()
    from ro import policy
    policy.enforce(lambda m: print(m, flush=True))
    t = Task(a.db, a.work_root, a.cache, a.timeout, log=lambda m: print(m, flush=True))
    try:
        if a.cmd == "start":
            code = t.start(a.repo, a.base, a.tip, a.requirement, a.commands, a.checks, a.implementer,
                           a.tests, a.gates, a.input_change)
        elif a.cmd == "env":
            t.build_envs()
            code = 0
        elif a.cmd == "run":
            code = t.run()
        elif a.cmd == "recheck":
            code = t.recheck(a.role, a.counter_evidence) or t.run()
        elif a.cmd == "final":
            code = t.final(a.deliverable, a.serial)
        else:
            print(json.dumps(t.status(), ensure_ascii=False, indent=2))
            code = 0
    except (Refused, ValueError, KeyError, RuntimeError, OSError, sqlite3.Error, subprocess.CalledProcessError) as e:
        p.exit(1, f"Orchestrator stopped: {e}\n")
    sys.exit(code)


if __name__ == "__main__":
    main()
