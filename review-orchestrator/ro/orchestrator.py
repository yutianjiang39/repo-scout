"""Lifecycle driver. Program decides what runs next; models only judge.

Per first-round gate:
  prepare (fresh copy + capsule) -> preflight (program tests + mutants, sealed in ledger)
  -> reviewer CLI (judgment) -> post (isolation audit, model receipt, probe re-runs)
  -> gate controller validates report against evidence -> PASS: seal + next gate.
"""

import json
from pathlib import Path
import shutil
import tempfile
import threading

from . import config, engine, evidence
from . import envs as envmod
from .gates import execute
from .ledger import opened
from .reviewers import find_report, run_cli
from .util import ROOT, Refused, sha

EXIT = dict(PASS=0, BLOCKED=1, REVISE=3)
PROMPT_RECHECK = (
    "The implementer disputes your objection with evidence in REVIEW/recheck/counter-evidence.md; "
    "the candidate is unchanged. Re-examine it independently and keep or withdraw your objection. "
    "Write the updated report (same contract, plus `reconciliation`) to REVIEW/out/report.json and print it."
)


class Task:
    def __init__(self, db, work_root=None, cache=None, timeout=None, log=print, stall=None):
        self.db = Path(db).resolve()
        self.dir = self.db.parent
        self.dir.mkdir(parents=True, exist_ok=True)
        self.work_root = Path(work_root).resolve() if work_root else None
        self.cache = Path(cache or ROOT / ".cache" / "envs")
        self.timeout = timeout
        self.stall = stall
        self.log = log

    def _cli(self):
        return dict(timeout=self.timeout, stall=self.stall, log=self.log)

    # ------------------------------------------------------------ ledger access
    def gate(self, cmd, *args):
        return execute(self.db, cmd, *args)

    def state(self):
        return self.gate("status")

    def frozen(self, name):
        with opened(self.db) as L:
            return L.get(name)

    def all_workspaces(self):
        with opened(self.db) as L:
            return {w for st in L.all_states() for w in st.get("workspaces", {}).values()}

    def checks(self):
        return json.loads(self.frozen("checks.json") or b"{}")

    def envs(self):
        return envmod.from_state(self.state()["envs"])

    # ------------------------------------------------------------ freeze + env
    def start(self, repo, base, tip, requirement, commands, checks, implementer, tests, gates, input_change=""):
        if self.db.exists() and self._has_state():
            self.seal_all()
        flags = [f"--gates={gates}", f"--checks={checks}"] + ([f"--input-change={input_change}"] if input_change else [])
        s = self.gate("freeze", repo, base, tip, requirement, commands, implementer, *tests, *flags)
        self.log(f"frozen v{s['version']} patch={s['patch'][:16]} gates={' -> '.join(s['gates'])}")
        self.build_envs()
        return self.run()

    def _has_state(self):
        with opened(self.db) as L:
            return not L.empty

    def build_envs(self):
        s = self.state()
        built = envmod.build(self.checks(), s["repo"], s["base"], s["tree"], self.cache, self.log)
        envs = {k: v.as_dict() for k, v in built.items()}
        if s.get("envs") == envs:
            return
        path = self.dir / f"envs-v{s['version']}.json"
        path.write_text(json.dumps(envs, indent=2), encoding="utf-8")
        self.gate("set-env", path)

    # ------------------------------------------------------------ first round
    def run(self):
        while True:
            s = self.state()
            if s["phase"] != "first":
                break
            role = s["current"]
            if s.get("attempt"):
                self.log(f"[{role}] prepared but unfinished (interrupted). gate.py fail {role} <reason>; gate.py retry")
                return 2
            code = self.first_gate(role)
            if code:
                return code
        s = self.state()
        if s["phase"] == "draft":
            self.log(f"All gates {s['gates']} PASS for {s['patch']}. Next: orchestrate.py final <deliverable.md>")
            return 0
        self.log(f"phase={s['phase']}")
        return 0 if s["phase"] in ("ready", "authorized", "completed") else 1

    def _prepare(self, role):
        old = tempfile.tempdir
        if self.work_root:
            self.work_root.mkdir(parents=True, exist_ok=True)
            tempfile.tempdir = str(self.work_root)
        try:
            return self.gate("prepare", role)
        finally:
            tempfile.tempdir = old

    def first_gate(self, role):
        self.log(f"[{role}] preparing a fresh workspace (base checkout + sealed capsule)")
        s = self._prepare(role)
        n, attempt, work = s["version"], s["attempt"], Path(s["workspaces"][role])
        self.log(f"[{role}] gate {s['gates'].index(role) + 1}/{len(s['gates'])}  workspace {work}")
        if s["kind"] == "code":
            out = evidence.store_dir(self.dir, n, role, attempt)
            self.log(f"[{role}] preflight (program-run tests, mutants)")
            ev = engine.preflight(work, work / "REVIEW", self.envs(), self.checks(), out, self.log,
                                  mutation_cache=self.dir / "mutation" / f"v{n}")
            s = self.gate("attach", role, evidence.write(ev, out))
            if s["phase"] == "blocked":
                self.log(f"[{role}] BLOCKED before review: {ev['problems']}  evidence={out}")
                return 1
            evidence.publish_copy(out, work)
        tdir = evidence.store_dir(self.dir, n, role, attempt, "transcripts")
        self.log(f"[{role}] reviewer CLI running ({config.model(role)})", )
        res = run_cli(role, work, tdir, **self._cli())
        return self._record(role, work, res, tdir, "report", s["kind"] == "code")

    def post(self, role, work, res, tdir, code):
        s = self.state()
        forbidden = {self.dir, ROOT / "private"} | {Path(w) for w in self.all_workspaces() if Path(w) != work}
        if s.get("repo"):
            forbidden.add(Path(s["repo"]))
        violations = [f"referenced outside workspace: {p}" for p in evidence.audit(res["raw"], forbidden)]
        observed = evidence.observed_models(res["raw"])
        ok = evidence.model_matches(res["model"], observed) if observed else None
        if ok is False:
            violations.append(f"model mismatch: expected {res['model']}, CLI reported {observed}")
        probes = {}
        if code and not violations:
            first = next(iter(self.envs().values()))
            for name, p in evidence.collect_probes(work).items():
                r = engine.verify_probe(work, work / "REVIEW", first, self.checks(), p["diff"], p["on"],
                                        tdir / "probes", name)
                (tdir / "probes").mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p["diff"], tdir / "probes" / f"{name}.diff")
                r.pop("runs", None)
                probes[name] = r
                self.log(f"[{role}] probe {name}: program re-run -> "
                         f"{'invalid' if r['detected'] is None else ('detected' if r['detected'] else 'missed')}")
        post = dict(attempt=s["attempt"] if s["phase"] != "final" else s["attempts"].get(role),
                    transcript_sha256=sha(res["raw"].encode()), exit=res["exit"], violations=violations,
                    models=dict(expected=res["model"], observed=observed, ok=ok), probes=probes)
        path = tdir / "post.json"
        path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
        return post, path

    def _report_file(self, res, tdir, location):
        if location.is_file():
            data = location.read_bytes()
        else:
            found = find_report(res["raw"])
            data = json.dumps(found, ensure_ascii=False, indent=2).encode() if found else None
        if data is None:
            return None
        (tdir / "report.json").write_bytes(data)
        return tdir / "report.json"

    def _record(self, role, work, res, tdir, cmd, code):
        self.log(f"[{role}] post-checks: isolation audit, model receipt, probe re-runs")
        post, post_path = self.post(role, work, res, tdir, code)
        report = self._report_file(res, tdir, work / "REVIEW" / "out" / "report.json")
        reason = None
        if post["violations"]:
            reason = "; ".join(post["violations"])
        elif res.get("abort"):
            reason = f"reviewer CLI aborted: {res['abort']}"
        elif not report:
            reason = f"no report JSON (CLI exit {res['exit']})"
        elif not res["session"]:
            reason = "no session id in CLI output"
        if not reason:
            try:
                args = [role, res["session"], report] + ([post_path] if code else [])
                self.gate(cmd, *args)
                verdict = json.loads(report.read_bytes())["verdict"]
                self.log(f"[{role}] {verdict}  report sha256={sha(report.read_bytes())[:16]}  transcript={tdir}")
                return self._after(role, work, verdict, report)
            except (Refused, KeyError, TypeError, ValueError) as e:
                reason = f"report rejected by gate: {e}"
        self.log(f"[{role}] BLOCKED: {reason}  transcript={tdir}")
        try:
            self.gate("fail", role, reason)
        except Refused:
            pass
        return 1

    def _after(self, role, work, verdict, report):
        s = self.state()
        if verdict == "PASS":
            evidence.seal(work, evidence.store_dir(self.dir, s["version"], role, s["attempts"][role], "sealed"))
            return 0
        if verdict == "REVISE":
            d = self.dir / "handoff"
            d.mkdir(exist_ok=True)
            dest = d / f"v{s['version']}-{role}-objection.json"
            shutil.copyfile(report, dest)
            self.log(f"STOP: REVISE. Objection for the implementer: {dest}\n"
                     "Fix, then start again (new hash, from gate 1). Never copy objections into blind inputs.")
            return 3
        self.log("STOP: BLOCKED. Resolve, then gate.py retry and orchestrate.py run")
        return 1

    def seal_all(self):
        s = self.state()
        for role, w in s.get("workspaces", {}).items():
            if Path(w).exists():
                evidence.seal(w, evidence.store_dir(self.dir, s["version"], role, s["attempts"].get(role, "unknown0"), "sealed"))

    # ------------------------------------------------------------ recheck (evidence dispute, patch unchanged)
    def recheck(self, role, counter_evidence):
        s = self.state()
        if s["phase"] != "revise" or s.get("objector") != role:
            raise Refused("No open objection from this reviewer")
        work = Path(s["workspaces"][role])
        d = work / "REVIEW" / "recheck"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(counter_evidence, d / "counter-evidence.md")
        (work / "REVIEW" / "out" / "report.json").unlink(missing_ok=True)
        tdir = evidence.store_dir(self.dir, s["version"], role, s["attempts"][role], "transcripts") / "recheck"
        res = run_cli(role, work, tdir, final=True, session=s["sessions"][role], prompt=PROMPT_RECHECK,
                      **self._cli())
        return self._record(role, work, res, tdir, "recheck", s["kind"] == "code")

    # ------------------------------------------------------------ final round
    def final(self, body, serial=False):
        s = self.state()
        if s.get("body") != sha(Path(body).read_bytes()):
            s = self.gate("draft", body)
        roles = [r for r in s["gates"] if s["first"].get(r) == "PASS" and r not in s["final"]]
        results = {}

        def one(role):
            st = self.gate("exchange", role)
            work = Path(st["workspaces"][role])
            folder = f"REVIEW/final/{st['body']}"
            tdir = evidence.store_dir(self.dir, st["version"], role, st["attempts"][role], "transcripts") / f"final-{st['body'][:8]}"
            results[role] = (work, folder, tdir, run_cli(role, work, tdir, final=True, session=st["sessions"][role],
                                                          folder=folder, **self._cli()))

        if serial:
            for r in roles:
                one(r)
        else:
            threads = [threading.Thread(target=one, args=(r,)) for r in roles]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        worst = 0
        for role in roles:
            if role not in results:
                worst = max(worst, 1)
                continue
            work, folder, tdir, res = results[role]
            post, _ = self.post(role, work, res, tdir, code=False)
            report = self._report_file(res, tdir, work / folder / "report.json")
            if post["violations"] or not report:
                self.log(f"[{role}] final BLOCKED: {post['violations'] or 'no report'}")
                worst = max(worst, 1)
                continue
            try:
                self.gate("report", role, res["session"], report)
                verdict = json.loads(report.read_bytes())["verdict"]
            except (Refused, KeyError, TypeError, ValueError) as e:
                self.log(f"[{role}] final report rejected: {e}")
                worst = max(worst, 1)
                continue
            self.log(f"[{role}] final {verdict}")
            worst = max(worst, EXIT.get(verdict, 1))
            if verdict == "REVISE":
                d = self.dir / "handoff"
                d.mkdir(exist_ok=True)
                shutil.copyfile(report, d / f"v{self.state()['version']}-{role}-final-objection.json")
        s = self.state()
        self.log(f"phase={s['phase']}")
        return 0 if s["phase"] == "ready" else (worst or 1)

    def status(self):
        s = self.state()
        keep = {k: s.get(k) for k in ("version", "kind", "phase", "current", "patch", "body", "gates", "first", "manifest")}
        keep["final"] = sorted(s.get("final", {}))
        keep["evidence"] = {r: dict(sha256=e["sha256"][:16], problems=e["problems"]) for r, e in s.get("evidence", {}).items()}
        keep["reports"] = [{k: x[k] for k in ("version", "role", "tag", "verdict", "sha256", "session") if k in x}
                           for x in s.get("reports", [])]
        return keep
