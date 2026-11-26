import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ro import evidence, mutation
from ro.gates import execute
from ro.orchestrator import Task
from ro.reviewers import find_report, find_session
from ro.util import Refused, git

FIX = "def value(x):\n    if x < 0:\n        return -x\n    return x\n"
CHECK = "from value import value\nassert value(-2) == 2 and value(-5) == 5 and value(3) == 3 and value(0) == 0\n"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        for k, v in (("user.name", "F"), ("user.email", "f@localhost"), ("core.autocrlf", "false"),
                     ("commit.gpgsign", "false")):
            git(self.repo, "config", k, v)
        self.commit({"value.py": "def value(x):\n    return x\n"}, "base")
        self.commit({"value.py": FIX, "check.py": CHECK}, "fix")
        self.req = self.file("req.md", "value() must return the absolute value")
        self.cmd = self.file("cmd.md", "python check.py")
        self.checks = self.file("checks.json", json.dumps(dict(
            module="value", focused=[["{py}", "check.py"]], suite=[["{py}", "check.py"]])))
        self.db = self.root / "task" / "review.sqlite"
        self.cli = self.fake_cli()
        env = patch.dict(os.environ, {"REVIEW_OPENCODE_BIN": str(self.cli), "REVIEW_CLAUDE_BIN": str(self.cli),
                                      "FAKE_MODE": "pass", "REVIEW_MIRRORS": str(self.root / "mirrors"),
                                      # as when orchestrate.py is started from Git Bash elsewhere
                                      "PWD": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        p = patch("ro.reviewers._environment", lambda role: os.environ.copy())
        p.start()
        self.addCleanup(p.stop)
        self.logs = []

    def commit(self, files, msg):
        for k, v in files.items():
            (self.repo / k).write_text(v, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", msg)

    def file(self, name, text):
        p = self.root / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def fake_cli(self):
        if os.name == "nt":
            p = self.root / "fake.cmd"
            p.write_text(f'@"{sys.executable}" -B "{HERE / "fake_reviewer.py"}" %*\n')
        else:
            p = self.root / "fake"
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" -B "{HERE / "fake_reviewer.py"}" "$@"\n')
            p.chmod(0o755)
        return p

    def task(self):
        return Task(self.db, self.root / "ws", self.root / "cache", log=self.logs.append)

    def start(self, base="HEAD~1", tip="HEAD", **kw):
        return self.task().start(str(self.repo), base, tip, self.req, self.cmd, self.checks, "impl-1",
                                 ["check.py"], kw.pop("gates", "gpt,claude"), kw.pop("input_change", ""))

    def state(self):
        return execute(self.db, "status")


class Flow(Base):
    def test_program_driven_flow_to_ready(self):
        self.assertEqual(self.start(), 0, "\n".join(self.logs))
        s = self.state()
        self.assertEqual((s["phase"], s["first"]), ("draft", {"gpt": "PASS", "claude": "PASS"}))
        execute(self.db, "export", self.root / "exp")
        ev = json.loads((self.root / "exp" / s["evidence"]["gpt"]["file"]).read_text())
        exits = {(r["phase"], r["kind"]): r["exit"] for r in ev["runs"]}
        self.assertNotEqual(exits[("baseline", "focused")], 0)
        self.assertEqual((exits[("fixed", "focused")], exits[("fixed", "suite")]), (0, 0))
        self.assertTrue(ev["mutation"]["supported"])
        self.assertTrue(ev["mutation"]["survived"], "x<=0 should survive (equivalent at 0)")
        self.assertGreater(ev["mutation"]["score"], 0.5)
        cc = json.loads((self.root / "exp" / s["evidence"]["claude"]["file"]).read_text())
        self.assertEqual((ev["mutation"]["shared"], cc["mutation"]["shared"]), (False, True))
        self.assertEqual(cc["mutation"]["survived"], ev["mutation"]["survived"])
        self.assertTrue(any(r["phase"] == "baseline" for r in cc["runs"]), "tests still re-run per gate")
        gpt_ws = Path(s["workspaces"]["gpt"])
        cached = [p for p in (self.task().dir / "mutation").rglob("*") if p.is_file()]
        self.assertTrue(cached)
        for p in cached:
            self.assertEqual(evidence.leaks(p.read_bytes(), {gpt_ws}), [], f"{p} leaks the computing workspace")
        self.assertFalse((gpt_ws / "REVIEW" / "out").exists(), "PASS gate must be sealed")
        self.assertFalse((gpt_ws / ".review-runs").exists())
        self.assertEqual(self.task().final(self.file("pr.md", "fix: abs")), 0, "\n".join(self.logs))
        self.assertEqual(self.state()["phase"], "ready")

    def test_stalled_reviewer_aborted_with_progress_lines(self):
        os.environ["FAKE_MODE"] = "hang"
        t = self.task()
        t.stall = 3
        self.assertEqual(t.start(str(self.repo), "HEAD~1", "HEAD", self.req, self.cmd, self.checks, "impl-1",
                                 ["check.py"], "gpt,claude"), 1)
        log = "\n".join(self.logs)
        self.assertIn("API retry 1/9 (502)", log)
        self.assertIn("ABORT: no model progress for 3s", log)
        self.assertIn("BLOCKED: reviewer CLI aborted", log)
        self.assertEqual(self.state()["phase"], "blocked")

    def test_one_run_per_repository(self):
        from ro import runlock
        tasks = self.root / "repos" / "petl" / "tasks"
        held = runlock.acquire(tasks / "new-round" / "review.sqlite", "start")
        self.addCleanup(held.close)
        other = tasks / "old-round" / "review.sqlite"
        with self.assertRaises(Refused) as e:
            runlock.acquire(other, "run")
        self.assertIn("task new-round (start", str(e.exception))
        held.close()
        runlock.acquire(other, "run").close()

    def test_skipped_optional_gates_are_never_started(self):
        self.start()
        self.assertNotIn("deepseek", self.state()["workspaces"])

    def test_revise_stops_then_new_hash_restarts_from_gate_one(self):
        os.environ["FAKE_MODE"] = "revise"
        self.assertEqual(self.start(), 3)
        self.assertTrue((self.db.parent / "handoff" / "v1-gpt-objection.json").is_file())
        self.assertNotIn("claude", self.state()["workspaces"])
        os.environ["FAKE_MODE"] = "pass"
        self.commit({"value.py": FIX.replace("return -x", "return abs(x)")}, "v2")
        self.assertEqual(self.start(base="HEAD~2"), 0, "\n".join(self.logs))
        s = self.state()
        self.assertEqual((s["version"], s["first"]), (2, {"gpt": "PASS", "claude": "PASS"}))

    def test_recheck_withdrawal_continues(self):
        os.environ["FAKE_MODE"] = "revise-then-withdraw"
        self.assertEqual(self.start(), 3)
        t = self.task()
        self.assertEqual(t.recheck("gpt", self.file("ce.md", "zero is covered by check.py")), 0, "\n".join(self.logs))
        os.environ["FAKE_MODE"] = "pass"
        self.assertEqual(t.run(), 0)
        self.assertEqual(self.state()["first"], {"gpt": "PASS", "claude": "PASS"})


class Enforcement(Base):
    def blocked_with(self, mode, text):
        os.environ["FAKE_MODE"] = mode
        self.assertEqual(self.start(), 1)
        self.assertIn(text, "\n".join(self.logs))
        self.assertEqual(self.state()["phase"], "blocked")

    def test_judged_gap_cannot_pass(self):
        self.blocked_with("gap", "judged a test gap")

    def test_false_probe_claim_rejected(self):
        self.blocked_with("lie", "program observed detected")

    def test_reading_outside_workspace_voids_review(self):
        os.environ["FAKE_PEEK"] = f"cat {self.db.parent / 'handoff'}"
        self.blocked_with("peek", "referenced outside workspace")

    def test_wrong_model_receipt_voids_review(self):
        self.blocked_with("wrong-model", "model mismatch")

    def test_broken_candidate_blocked_before_any_model_runs(self):
        self.commit({"value.py": "def value(x):\n    return -x\n"}, "broken")
        self.assertEqual(self.start(base="HEAD~2"), 1)
        self.assertIn("not green", "\n".join(self.logs))
        self.assertFalse((self.db.parent / "transcripts").exists())

    def test_manual_report_without_program_evidence_refused(self):
        execute(self.db, "freeze", self.repo, "HEAD~1", "HEAD", self.req, self.cmd, "impl", "check.py",
                f"--checks={self.checks}", "--gates=gpt,claude")
        r = self.file("r.json", json.dumps(dict(role="gpt", verdict="PASS", summary="x", objections=[])))
        with self.assertRaises(Refused):
            execute(self.db, "report", "gpt", "s1", r)

    def test_required_gates_and_input_guard(self):
        args = [self.repo, "HEAD~1", "HEAD", self.req, self.cmd, "impl", "check.py", f"--checks={self.checks}"]
        with self.assertRaisesRegex(Refused, "Required"):
            execute(self.db, "freeze", *args, "--gates=gpt,deepseek")
        execute(self.db, "freeze", *args)
        changed = [self.repo, "HEAD~1", "HEAD", self.file("req.md", "reviewer said zero fails"), *args[4:]]
        with self.assertRaisesRegex(Refused, "input-change"):
            execute(self.db, "freeze", *changed)
        s = execute(self.db, "freeze", *changed, "--input-change=scope clarified")
        self.assertEqual(s["version"], 2)

    def test_artifact_task_gate_rules(self):
        execute(self.db, "freeze-task", self.file("manual.md", "Restore from backup"), self.req, self.cmd, "impl")
        with patch("tempfile.tempdir", str(self.root)):
            s = execute(self.db, "prepare", "gpt")
        review = Path(s["workspaces"]["gpt"]) / "REVIEW"
        self.assertIn("gate 1 of 4", (review / "STAGE.md").read_text(encoding="utf-8"))
        base = dict(role="gpt", attempt=s["attempt"], patch=s["patch"], verdict="PASS", summary="ok", objections=[])
        with self.assertRaisesRegex(Refused, "acceptance checks"):
            execute(self.db, "report", "gpt", "s1", self.file("r.json", json.dumps(base)))
        full = dict(base, checks=[dict(criterion="restore", method="read", observed="present")], probe="no backup")
        self.assertEqual(execute(self.db, "report", "gpt", "s1", self.file("r.json", json.dumps(full)))["current"],
                         "deepseek")

    def test_env_cache_key_follows_dependency_files_only(self):
        from ro.envs import dependency_key
        spec = {"install": [["{py}", "-m", "pip", "install", "-r", "dev.txt"]]}
        key = lambda: dependency_key(self.repo, "HEAD", spec)
        before = key()
        self.commit({"value.py": FIX + "# source-only change\n"}, "src")
        self.assertEqual(key(), before, "source-only change must reuse the env")
        self.commit({"requirements.txt": "attrs\n"}, "deps")
        after_req = key()
        self.assertNotEqual(after_req, before)
        self.commit({"dev.txt": "pytest\n"}, "named install file")
        self.assertNotEqual(key(), after_req, "files named in install commands count")
        self.assertNotEqual(dependency_key(self.repo, "HEAD", {"install": []}), key(), "spec counts")

    def test_checkouts_share_one_mirror_without_candidate(self):
        from ro.util import clone_base, git_text, mirror
        base = git_text(self.repo, "rev-parse", "HEAD~1")
        a = clone_base(self.root / "a", self.repo, base)
        b = clone_base(self.root / "b", self.repo, base)
        self.assertEqual(git_text(a, "rev-parse", "HEAD"), git_text(b, "rev-parse", "HEAD"))
        self.assertEqual(len(list((self.root / "mirrors").iterdir())), 1)
        m = mirror(self.repo, base)
        tip = git_text(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(subprocess.run(["git", "-C", str(m), "cat-file", "-e", tip], capture_output=True)
                            .returncode, 0, "candidate must never enter the shared mirror")

    def test_code_freeze_requires_checks(self):
        with self.assertRaisesRegex(Refused, "checks"):
            execute(self.db, "freeze", self.repo, "HEAD~1", "HEAD", self.req, self.cmd, "impl", "check.py")


class Units(unittest.TestCase):
    def test_scrub_removes_every_audited_form_of_workspace(self):
        ws = Path(tempfile.gettempdir()).resolve() / "开源贡献" / "review-gpt-abc"
        p = str(ws)
        fwd = p.replace("\\", "/")
        forms = [p, p.upper(), fwd, p.replace("\\", "\\\\"), p + "\\.review-runs\\mutation\\x.py"]
        if fwd[1] == ":":
            forms.append("/" + fwd[0].lower() + fwd[2:])
        text = ("$ pytest\n" + "\n".join(f"File \"{f}\", line 3" for f in forms) + "\n").encode("utf-8")
        self.assertTrue(evidence.leaks(text, {ws}))
        out = evidence.scrub_bytes(text, ws)
        self.assertEqual(evidence.leaks(out, {ws}), [])
        self.assertIn(evidence.WORKSPACE_TOKEN.encode() + b"\\.review-runs\\mutation\\x.py", out)
        other = Path(tempfile.gettempdir()).resolve() / "review-claude-xyz"
        self.assertIn(str(other).encode(), evidence.scrub_bytes(str(other).encode(), ws), "other paths untouched")

    def test_mutants_only_on_added_lines_and_keep_formatting(self):
        diff = (b"diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,4 @@\n def f(x):  # keep\n"
                b"+    if x is None:\n+        return 0\n     return x + 1\n")
        lines = mutation.added_lines(diff)
        self.assertEqual(lines, {"m.py": {2, 3}})
        src = "def f(x):  # keep\n    if x is None:\n        return 0\n    return x + 1  # é\n".encode()
        mutants, _ = mutation.generate({"m.py": (src, lines["m.py"])}, limit=50)
        ops = {m["operator"] for m in mutants}
        self.assertTrue({"swap-compare", "condition-false", "delete-statement"} <= ops)
        self.assertTrue(all(m["line"] in (2, 3) for m in mutants))
        self.assertTrue(all(b"# keep" in m["source"] and "# é".encode() in m["source"] for m in mutants))
        self.assertIn(b"x is not None", next(m for m in mutants if m["operator"] == "swap-compare")["source"])

    def test_output_parsing(self):
        body = json.dumps(dict(role="claude", verdict="PASS"))
        raw = json.dumps(dict(type="result", session_id="abc", result="Done\n```json\n" + body + "\n```"))
        self.assertEqual((find_session(raw), find_report(raw)["verdict"]), ("abc", "PASS"))
        self.assertEqual(evidence.observed_models(json.dumps({"modelUsage": {"claude-opus-5-5": {}}})),
                         ["claude-opus-5-5"])
        self.assertTrue(evidence.model_matches("vvvvvip/gpt-6.2-astra#high", ["vvvvvip/gpt-6.2-astra"]))
        self.assertFalse(evidence.model_matches("claude-opus-5-5", ["claude-haiku-5-5"]))

    def test_audit_path_forms(self):
        p = Path(tempfile.gettempdir()) / "task-x"
        fwd = str(p.resolve()).replace("\\", "/")
        self.assertEqual(evidence.audit(f"Read {fwd}/handoff", [p]), [str(p)])
        self.assertEqual(evidence.audit("Read REVIEW/STAGE.md", [p]), [])


if __name__ == "__main__":
    unittest.main()
