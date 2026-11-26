"""Gate controller: the only component that changes review state.

Program-verifiable rules are enforced here; semantic judgment comes from reports, but
every claim a report makes about measurable facts is checked against program evidence.
"""

import json
from pathlib import Path
import sys
import tempfile
import uuid

from . import artifacts, config
from .ledger import opened
from .util import Refused, clone_base, dump, need, sha

TEXT_FIELDS = ("root_cause", "correctness", "compatibility", "test_adequacy", "risks", "evidence_review")
JUDGMENTS = ("equivalent", "irrelevant", "gap")


def split_flags(a):
    flags, rest = {}, []
    for x in a:
        if x.startswith("--") and "=" in x:
            k, v = x[2:].split("=", 1)
            flags[k] = v
        else:
            rest.append(x)
    return flags, rest


def text(r, k):
    return isinstance(r.get(k), str) and r[k].strip()


class Controller:
    def __init__(self, L):
        self.L = L

    @property
    def s(self):
        return self.L.s

    # ------------------------------------------------------------ helpers
    def next_gate(self):
        s = self.s
        role = next((r for r in s["gates"] if r not in s["first"]), None)
        s.update(phase="first" if role else "draft", current=role, attempt=None)

    def participants(self):
        return [r for r in self.s["gates"] if self.s["first"].get(r) == "PASS"]

    def code(self):
        return self.s.get("kind") == "code"

    def save(self, name, data):
        self.L.put(name, data)
        return name

    # ------------------------------------------------------------ freeze
    def freeze(self, a, kind):
        flags, a = split_flags(a)
        prior = dict(self.s)
        (artifacts.freeze_code if kind == "code" else artifacts.freeze_artifact)(self.L, prior, a, flags)
        self.s["deliverable_format"] = True
        self.next_gate()

    def set_env(self, path):
        s = self.s
        need(self.code(), "Shared envs only apply to code candidates")
        need(s["phase"] in ("first", "blocked") and not s.get("attempt"), "Register envs before preparing a gate")
        envs = json.loads(Path(path).read_bytes())
        wanted = set(json.loads(self.L.get("checks.json")).get("envs") or {"system": None})
        need(set(envs) == wanted and all("python" in e for e in envs.values()),
             "envs.json must describe exactly the envs in checks.json")
        s["envs"] = envs

    # ------------------------------------------------------------ first round
    def prepare(self, role):
        s, L = self.s, self.L
        need(s["phase"] == "first" and s["current"] == role and not s["attempt"], "Not this gate, or already prepared")
        need(not self.code() or s.get("envs"), "Build/register shared envs first (orchestrate.py env)")
        dst = Path(tempfile.mkdtemp(prefix=f"review-{role}-")).resolve()
        binding = dict(role=role, model=config.model(role), patch=s["patch"], manifest=s["manifest"])
        manifest = json.loads(L.get("MANIFEST.json"))
        if self.code():
            need(not dst.is_relative_to(Path(s["repo"])), "Work root must be outside the source repository")
            clone_base(dst, s["repo"], s["base"], s["tree"])
            binding.update(base=s["base"], tree=s["tree"])
        else:
            binding.update(kind="artifact", candidate=s["filename"])
        out = dst / "REVIEW"
        out.mkdir()
        attempt = uuid.uuid4().hex
        for name in manifest["files"]:
            (out / name).write_bytes(L.get(name))
        (out / "MANIFEST.json").write_bytes(L.get("MANIFEST.json"))
        (out / "binding.json").write_bytes(dump(dict(binding, attempt=attempt)))
        if s.get("envs"):
            (out / "envs.json").write_bytes(dump(s["envs"]))
        gates = s["gates"]
        py = f'"{sys.executable}" REVIEW/harness.py'
        tools = (f"Helpers: `{py} test <copy-dir>` runs the focused tests in any copy; "
                 f"`{py} probe-start <name> [--on baseline]` / `{py} probe-run <name>` record a wrong implementation.\n"
                 if self.code() else "")
        base_line = (f"- Base commit: `{s['base']}`" if self.code() else f"- Candidate file: `REVIEW/{s['filename']}`")
        (out / "STAGE.md").write_text(L.get("stage.md").decode().format(
            role=role, index=gates.index(role) + 1, total=len(gates), gates=" -> ".join(gates),
            model=config.model(role), patch=s["patch"], attempt=attempt, base_line=base_line, tools=tools),
            encoding="utf-8")
        s["attempt"] = attempt
        s["workspaces"][role] = str(dst)

    def attach(self, role, path):
        """Program evidence for the prepared gate, sealed before the reviewer starts."""
        s = self.s
        need(self.code() and s["phase"] == "first" and s["current"] == role and s["attempt"], "No prepared gate")
        need(role not in s["evidence"] or s["evidence"][role]["attempt"] != s["attempt"], "Evidence already sealed")
        raw = Path(path).read_bytes()
        ev = json.loads(raw)
        need(ev.get("attempt") == s["attempt"] and ev.get("patch") == s["patch"], "Evidence for another attempt/patch")
        name = self.save(f"{role}-evidence-{s['attempt']}.json", raw)
        s["evidence"][role] = dict(attempt=s["attempt"], file=name, sha256=sha(raw), problems=ev["problems"])
        if ev["problems"]:
            self.save(f"{s['attempt']}-failure.txt", ("preflight: " + "; ".join(ev["problems"])).encode())
            s["phase"] = "blocked"

    def fail(self, role, reason):
        s = self.s
        need(s["phase"] == "first" and role == s["current"] and s["attempt"] and reason.strip(),
             "No matching attempt or reason")
        self.save(f"{s['attempt']}-failure.txt", reason.encode())
        s["phase"] = "blocked"

    def retry(self):
        need(self.s["phase"] == "blocked", "No blocked first review")
        self.next_gate()

    def skip(self, role, kind, reason):
        s = self.s
        cfg = config.reviewers().get(role, {})
        need(s["phase"] in ("first", "blocked") and role == s["current"] and not cfg.get("required")
             and role in s["gates"] and reason.strip(), "Cannot skip this gate")
        need(kind == "operator" or (kind == "connection" and cfg.get("skippable_on_connection_failure")
                                    and s["phase"] == "blocked"),
             "Explicit user exception, or a recorded connection failure on a skippable gate, required")
        self.save(f"{role}-skip.txt", (kind + "\n" + reason).encode())
        s["first"][role] = "SKIPPED"
        self.next_gate()

    # ------------------------------------------------------------ reports
    def _check_common(self, role, session, r):
        s = self.s
        need(r.get("verdict") in ("PASS", "BLOCKED", "REVISE") and r.get("patch") == s["patch"]
             and r.get("role") == role and session.strip() and session != s["implementer"],
             "Invalid binding/session/verdict")
        need(isinstance(r.get("objections"), list) and text(r, "summary"), "Missing summary/objections")
        need(r["verdict"] != "PASS" or not r["objections"], "PASS with unresolved objections")
        need(r["verdict"] != "REVISE" or r["objections"], "REVISE needs objections with evidence")

    def _check_code_pass(self, r, ev, post):
        for k in TEXT_FIELDS:
            need(text(r, k), f"Report field '{k}' missing (semantic judgment required)")
        # Baseline classification must agree with what the program measured.
        bf = r.get("baseline_failure")
        need(bf in ("target-defect", "none", "setup"), "baseline_failure must be target-defect|none|setup")
        need(bf != "setup", "Baseline failed for setup reasons: evidence cannot show the defect (BLOCKED)")
        base_exits = [x["exit"] for x in ev["runs"] if x["phase"] == "baseline" and x["kind"] == "focused"]
        need(base_exits, "No baseline run in program evidence")
        if bf == "target-defect":
            need(any(e != 0 for e in base_exits), "Report says baseline fails, program saw it pass")
        else:
            need(all(e == 0 for e in base_exits), "Report says baseline passes, program saw it fail")
        # Every surviving mutant must be judged; a judged gap cannot PASS.
        survived = set((ev.get("mutation") or {}).get("survived", []))
        judged = {m.get("id"): m for m in r.get("mutants", []) if isinstance(m, dict)}
        need(survived <= set(judged), f"Surviving mutants not judged: {sorted(survived - set(judged))}")
        for mid in survived:
            m = judged[mid]
            need(m.get("judgment") in JUDGMENTS and text(m, "reason"), f"Mutant {mid}: judgment+reason required")
            need(m["judgment"] != "gap", f"Mutant {mid} judged a test gap: verdict cannot be PASS")
        # Reviewer-designed wrong implementations, re-run by the program.
        verified = post.get("probes", {})
        claimed = [p for p in r.get("probes", []) if isinstance(p, dict)]
        need(claimed, "No reviewer-designed wrong implementation reported")
        for p in claimed:
            res = verified.get(p.get("name"))
            need(res is not None, f"Probe {p.get('name')} not found in REVIEW/out/probes")
            actual = "invalid" if res.get("detected") is None else ("detected" if res["detected"] else "missed")
            need(p.get("claimed") == actual, f"Probe {p['name']}: report claims {p.get('claimed')}, program observed {actual}")
            need(text(p, "rationale"), f"Probe {p['name']}: rationale required")
            if actual == "missed":
                need(p.get("judgment") in ("equivalent", "irrelevant") and text(p, "reason"),
                     f"Probe {p['name']} was not detected: explain why it is not a gap, or REVISE")
        need(any(verified.get(p["name"], {}).get("detected") for p in claimed),
             "At least one reviewer-designed wrong implementation must be detected by the tests")

    def report(self, cmd, role, session, path, post_path=None):
        s, L = self.s, self.L
        raw = Path(path).read_bytes()
        r = json.loads(raw)
        self._check_common(role, session, r)
        verdict = r["verdict"]
        post, post_raw = {}, None
        if cmd == "recheck":
            need(s["phase"] in ("revise", "final-revise") and role == s.get("objector")
                 and session == s["sessions"].get(role) and text(r, "reconciliation"),
                 "Original reviewer must recheck evidence")
            final = s["phase"] == "final-revise"
        else:
            final = s["phase"] == "final"
        if final:
            need(role in self.participants() and session == s["sessions"][role] and role not in s["final"]
                 and r.get("attempt") == s["attempts"][role] and r.get("body") == s["body"]
                 and s.get("exchange", {}).get(role) == s["body"] and text(r, "reconciliation"),
                 "Final package/session mismatch")
        else:
            need((s["phase"] == "first" or cmd == "recheck") and role == s["current"]
                 and s["attempt"] and r.get("attempt") == s["attempt"], "Wrong gate/attempt")
            if cmd == "report":
                need(all(session not in st.get("sessions", {}).values() and session != st.get("implementer")
                         for st in L.all_states()), "Independent fresh session required")
            if self.code():
                sealed = s["evidence"].get(role)
                need(sealed and sealed["attempt"] == s["attempt"] and not sealed["problems"],
                     "Program evidence missing for this attempt (orchestrator preflight)")
                ev_raw = L.get(sealed["file"])
                need(sha(ev_raw) == sealed["sha256"], "Sealed evidence altered")
                need(post_path, "Post-run verification bundle required (run through orchestrate.py)")
                post_raw = Path(post_path).read_bytes()
                post = json.loads(post_raw)
                need(post.get("attempt") == s["attempt"] and not post.get("violations"),
                     "Post-run verification failed or isolation violation")
                if verdict == "PASS":
                    self._check_code_pass(r, json.loads(ev_raw), post)
            elif verdict == "PASS":
                checks = r.get("checks")
                need(isinstance(checks, list) and checks and all(
                    isinstance(c, dict) and all(text(c, k) for k in ("criterion", "method", "observed"))
                    for c in checks) and text(r, "probe"),
                    "Missing acceptance checks or counterexample evidence")
            s["sessions"][role] = session
            s["attempts"][role] = s["attempt"]
        tag = "recheck" if cmd == "recheck" else ("objection" if verdict == "REVISE" else ("final" if final else "first"))
        name = self.save(f"{role}-{tag}-{uuid.uuid4().hex}.json", raw)
        record = dict(role=role, file=name, sha256=sha(raw), session=session, verdict=verdict, tag=tag,
                      version=L.v)
        if post_raw is not None:
            record["post_sha256"] = sha(self.L.get(self.save(name[:-5] + "-post.json", post_raw)))
        if role in s["evidence"] and not final:
            record["evidence_sha256"] = s["evidence"][role]["sha256"]
        s["reports"].append(record)
        if verdict == "PASS":
            if final:
                s["final"][role] = name
                s["phase"] = "ready" if set(s["final"]) == set(self.participants()) else "final"
            else:
                s["first"][role] = "PASS"
                self.next_gate()
        elif verdict == "REVISE" or cmd == "recheck":
            s.update(phase="final-revise" if final else "revise", objector=role,
                     objection_scope=r.get("scope", "patch"))
        elif not final:
            s["phase"] = "blocked"

    # ------------------------------------------------------------ final round
    def draft(self, path):
        s = self.s
        need(s["phase"] in ("draft", "final", "ready", "authorized")
             or (s["phase"] == "final-revise" and s.get("objection_scope") == "body"),
             "First reviews incomplete or substantive patch objection open")
        body = Path(path).read_bytes()
        h = sha(body)
        need(body.strip() and h != s.get("body"), "Need new deliverable summary")
        s.update(body=h, final={}, exchange={}, phase="final")
        s.pop("authorization", None)
        s.pop("target", None)
        self.L.conn.execute("INSERT OR IGNORE INTO files VALUES (?,?,?)", (self.L.v, f"deliverable-{h}.md", body))

    def exchange(self, role):
        s, L = self.s, self.L
        need(s["phase"] == "final" and role in self.participants(), "Not a final reviewer")
        d = Path(s["workspaces"][role]) / "REVIEW" / "final" / s["body"]
        d.mkdir(parents=True, exist_ok=True)
        for rec in s["reports"]:
            if rec.get("version") == L.v and rec["tag"] in ("first", "objection", "recheck") \
                    and rec["role"] in self.participants():
                (d / f"{rec['role']}-{rec['tag']}-{rec['sha256'][:8]}.json").write_bytes(L.get(rec["file"]))
        for role2 in self.participants():
            ev = s["evidence"].get(role2)
            if ev:
                (d / f"{role2}-program-evidence.json").write_bytes(L.get(ev["file"]))
        for k in ("final.md", "contract.md", s.get("filename") if not self.code() else "fix.diff"):
            (d / k).write_bytes(L.get(k))
        (d / "deliverable.md").write_bytes(L.get(f"deliverable-{s['body']}.md"))
        s.setdefault("exchange", {})[role] = s["body"]

    def authorize(self, patch, body, target, quote):
        s = self.s
        need(s["phase"] == "ready" and (patch, body) == (s["patch"], s["body"]) and target.strip() and quote.strip(),
             "Not ready or hashes/authorization missing")
        s.update(phase="authorized", target=target, authorization=quote)

    def complete(self, receipt):
        need(self.s["phase"] == "authorized" and receipt.strip(), "No authorization or completion receipt")
        self.s.update(phase="completed", receipt=receipt)


READ_ONLY = ("status", "history", "export")


def run(L, cmd, a):
    c = Controller(L)
    if cmd in ("freeze", "freeze-task"):
        c.freeze(a, "code" if cmd == "freeze" else "artifact")
    else:
        need(not L.empty, "Freeze a candidate first")
        if cmd == "status":
            return L.s
        if cmd == "history":
            return L.history()
        if cmd == "export":
            dest = Path(a[0])
            dest.mkdir(parents=True, exist_ok=False)
            for name, data in L.files(int(a[1]) if len(a) > 1 else None):
                (dest / name).write_bytes(data)
            return str(dest)
        handlers = {
            "set-env": lambda: c.set_env(*a), "prepare": lambda: c.prepare(*a),
            "attach": lambda: c.attach(*a), "fail": lambda: c.fail(*a), "retry": lambda: c.retry(*a),
            "skip": lambda: c.skip(*a), "report": lambda: c.report("report", *a),
            "recheck": lambda: c.report("recheck", *a), "draft": lambda: c.draft(*a),
            "exchange": lambda: c.exchange(*a), "authorize": lambda: c.authorize(*a),
            "complete": lambda: c.complete(*a),
        }
        need(cmd in handlers, "Unknown command")
        try:
            handlers[cmd]()
        except TypeError as e:
            raise Refused(f"Bad arguments for {cmd}: {e}")
    L.commit(cmd)
    return L.s


def execute(db, cmd, *args):
    with opened(db) as L:
        return run(L, cmd, [str(x) for x in args])


COMMANDS = "freeze freeze-task status history export set-env prepare attach fail retry skip report recheck draft exchange authorize complete".split()
