"""Test engine: deterministic runs for one reviewer's gate, in disposable worktrees.

Every gate gets its own fresh runs (results are never shared between reviewers).
"""

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import envs as envmod
from . import mutation
from .util import diff_paths, git_text, sha, sha_file

DEP_FILES = ("requirements", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "poetry.lock",
             "uv.lock", "constraints")


def junit_summary(path):
    if not path.is_file():
        return None
    failed, errors, skipped, passed = [], [], 0, 0
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    for case in root.iter("testcase"):
        nid = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None:
            failed.append(nid)
        elif case.find("error") is not None:
            errors.append(nid)
        elif case.find("skipped") is not None:
            skipped += 1
        else:
            passed += 1
    return dict(passed=passed, failed=failed, errors=errors, skipped=skipped)


def unittest_summary(text):
    """Best-effort counts from unittest output (`Ran N tests`, FAILED (failures=, errors=))."""
    m = re.search(r"Ran (\d+) tests?", text)
    if not m:
        return None
    f = re.search(r"failures=(\d+)", text)
    e = re.search(r"errors=(\d+)", text)
    return dict(ran=int(m.group(1)), failures=int(f.group(1)) if f else 0, errors=int(e.group(1)) if e else 0)


class Runner:
    def __init__(self, out_dir, timeout):
        self.out = Path(out_dir)
        self.logs = self.out / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.runs = []

    def execute(self, phase, kind, dst, env, cmds, timeout=None, record=True):
        results = []
        for i, template in enumerate(cmds):
            tag = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{phase}-{env.name}-{kind}-{i}")
            log, junit = self.logs / f"{tag}.log", self.logs / f"{tag}.xml"
            # pytest prints the junit path; keep it inside the reviewer's copy so logs never
            # reveal the program's evidence store location.
            local_junit = Path(dst) / f".review-junit-{tag}.xml"
            argv = env.command(template, dst)
            if any("pytest" in x for x in argv) and not any(x.startswith("--junitxml") for x in argv):
                argv.append(f"--junitxml={local_junit}")
            junit.unlink(missing_ok=True)
            local_junit.unlink(missing_ok=True)
            start, timed_out = time.time(), False
            with log.open("wb") as fh:
                fh.write(("$ " + subprocess.list2cmdline(argv) + "\n").encode())
                fh.flush()
                try:
                    code = subprocess.run(argv, cwd=dst, env=env.child_env(dst), stdout=fh,
                                          stderr=subprocess.STDOUT, timeout=timeout or self.timeout).returncode
                except subprocess.TimeoutExpired:
                    code, timed_out = 124, True
                    fh.write(b"\n[engine] timeout\n")
                except OSError as e:
                    code = 127
                    fh.write(f"\n[engine] cannot start: {e}\n".encode())
            if local_junit.is_file():
                local_junit.replace(junit)
            text = log.read_text(encoding="utf-8", errors="replace")
            rec = dict(phase=phase, kind=kind, env=env.name, index=i, argv=argv, exit=code,
                       timed_out=timed_out, seconds=round(time.time() - start, 1),
                       log=f"logs/{log.name}", log_sha256=sha_file(log),
                       junit=junit_summary(junit), unittest=unittest_summary(text),
                       tail=text[-1500:])
            results.append(rec)
            if record:
                self.runs.append(rec)
        return results


def related_tests(work, code_diff, tests_root="tests"):
    """Test files that import a changed module (informational, not gating)."""
    root = Path(work) / tests_root
    if not root.is_dir():
        return []
    mods = set()
    for p in diff_paths(code_diff):
        if p.endswith(".py"):
            parts = Path(p).with_suffix("").parts
            parts = parts[1:] if parts and parts[0] == "src" else parts
            if parts:
                mods.add(".".join(parts))
                mods.add(parts[-1])
    hits = []
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8", errors="replace")
        if any(re.search(rf"^\s*(from|import)\s+[\w.]*\b{re.escape(m)}\b", text, re.M) for m in mods):
            hits.append(f.relative_to(work).as_posix())
    return sorted(hits)


def preflight(work, capsule, envs, checks, out_dir, log=print):
    """Program-run verification for one gate. Returns the evidence dict."""
    work, capsule = Path(work), Path(capsule)
    binding = json.loads((capsule / "binding.json").read_text(encoding="utf-8"))
    runner = Runner(out_dir, checks.get("timeout", 1800))
    ev = dict(attempt=binding["attempt"], patch=binding["patch"], role=binding["role"],
              base=binding["base"], tree=binding["tree"], runs=runner.runs, problems=[])

    def problem(msg):
        ev["problems"].append(msg)
        log("  problem: " + msg)

    head = git_text(work, "rev-parse", "HEAD")
    tree = git_text(work, "rev-parse", "HEAD^{tree}")
    ev["verify"] = dict(head=head, tree=tree, patch=sha_file(capsule / "fix.diff"),
                        clean=not git_text(work, "status", "--porcelain", "--untracked-files=all"))
    if (head, tree) != (binding["base"], binding["tree"]) or ev["verify"]["patch"] != binding["patch"] \
            or not ev["verify"]["clean"]:
        problem("workspace or package does not match the frozen capsule")
        return ev
    code_diff = (capsule / "code.diff").read_bytes()
    ev["dependency_files_changed"] = [p for p in diff_paths(code_diff) if any(k in Path(p).name for k in DEP_FILES)]
    tests = diff_paths((capsule / "tests.diff").read_bytes())
    ev["tests_identical"], ev["import_origin"] = {}, {}
    for name, env in envs.items():
        if not env.intact():
            problem(f"shared env {name} changed since build")
            continue
        base = envmod.worktree(work, f"baseline-{name}", [capsule / "tests.diff"])
        fixed = envmod.worktree(work, f"fixed-{name}", [capsule / "fix.diff"])
        digest = [{p: sha((d / p).read_bytes()) if (d / p).is_file() else None for p in tests} for d in (base, fixed)]
        ev["tests_identical"][name] = digest[0] == digest[1]
        if checks.get("module"):
            o = [envmod.import_origin(env, checks["module"], d) for d in (base, fixed)]
            ev["import_origin"][name] = dict(baseline=o[0], fixed=o[1], ok=o[0]["ok"] and o[1]["ok"])
            if not ev["import_origin"][name]["ok"]:
                problem(f"{checks['module']} not imported from the review copy in {name}")
                continue
        for phase, dst in (("baseline", base), ("fixed", fixed)):
            for r in runner.execute(phase, "focused", dst, env, checks["focused"]):
                log(f"  {phase:<9}{name:<8} focused exit={r['exit']}")
        for r in runner.execute("fixed", "suite", fixed, env, checks.get("suite", [])):
            log(f"  fixed    {name:<8} suite   exit={r['exit']}")
        if checks.get("baseline_suite"):
            runner.execute("baseline", "suite", base, env, checks.get("suite", []))
    for name in envs:
        fx = [r for r in runner.runs if r["env"] == name and r["phase"] == "fixed"]
        if any(r["exit"] != 0 for r in fx):
            problem(f"candidate focused/suite not green in {name} (independent env)")
    rel = related_tests(work, code_diff)
    ev["related_tests"] = [p for p in rel if p not in tests]
    first = next(iter(envs.values()))
    m = checks.get("mutation", {})
    if m.get("enabled", True) and not ev["problems"]:
        ev["mutation"] = run_mutants(work, capsule, first, checks, runner, m, log)
    return ev


def run_mutants(work, capsule, env, checks, runner, cfg, log=print):
    lines = mutation.added_lines((capsule / "code.diff").read_bytes())
    if not lines:
        return dict(supported=False, reason="no Python lines added by the candidate")
    dst = envmod.worktree(work, "mutation", [capsule / "fix.diff"])
    files = {p: ((dst / p).read_bytes(), ls) for p, ls in lines.items() if (dst / p).is_file()}
    mutants, total = mutation.generate(files, cfg.get("max", 30))
    mdir = runner.out / "mutants"
    mdir.mkdir(exist_ok=True)
    results = []
    for m in mutants:
        target = dst / m["file"]
        original = target.read_bytes()
        target.write_bytes(m["source"])
        try:
            rs = runner.execute(f"mutant-{m['id']}", "focused", dst, env, checks["focused"],
                                timeout=cfg.get("timeout", 300), record=False)
        finally:
            target.write_bytes(original)
        (mdir / f"{m['id']}.diff").write_bytes(m["diff"].encode())
        timed_out = any(r["timed_out"] for r in rs)
        killed = any(r["exit"] != 0 for r in rs)
        status = "timeout" if timed_out else ("killed" if killed else "survived")
        results.append(dict({k: m[k] for k in ("id", "file", "line", "operator", "original", "replacement")},
                            status=status, diff=f"mutants/{m['id']}.diff",
                            log=rs[0]["log"], exit=rs[0]["exit"]))
    survived = [r["id"] for r in results if r["status"] == "survived"]
    killed = sum(r["status"] != "survived" for r in results)
    log(f"  mutation: {len(results)} mutants ({total} generated), {killed} killed, survivors={survived}")
    return dict(supported=True, generated=total, mutants=results, survived=survived,
                score=round(killed / len(results), 3) if results else None)


def verify_probe(work, capsule, env, checks, probe_diff, on, out_dir, name):
    """Re-run a reviewer-designed wrong implementation in a fresh program-made copy."""
    capsule = Path(capsule)
    tests = set(diff_paths((capsule / "tests.diff").read_bytes()))
    data = Path(probe_diff).read_bytes()
    touched = diff_paths(data)
    res = dict(name=name, on=on, diff_sha256=sha(data), touched=touched,
               touches_tests=bool(tests & set(touched)), empty=not data.strip())
    if res["empty"] or res["touches_tests"]:
        res["detected"] = None
        return res
    dst = envmod.worktree(work, f"verify-{name}", [capsule / ("fix.diff" if on == "fixed" else "tests.diff")])
    try:
        envmod.git(dst, "apply", "--whitespace=nowarn", str(probe_diff))
    except subprocess.CalledProcessError as e:
        res.update(detected=None, error="probe diff does not apply: " + e.stderr.decode(errors="replace")[-300:])
        return res
    runner = Runner(out_dir, checks.get("timeout", 1800))
    rs = runner.execute(f"probe-{name}", "focused", dst, env, checks["focused"])
    res.update(detected=any(r["exit"] != 0 and not r["timed_out"] for r in rs), runs=rs)
    return res
