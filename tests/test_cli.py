"""Offline command-line contract: real exits, durable records, no remote APIs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repo_scout.database import Store
from repo_scout.implementation import commit_changes, git


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "scout"
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *argv, env=None):
        return subprocess.run([sys.executable, "-m", "repo_scout", "--home", str(self.home), *map(str, argv)],
                              cwd=self.root, env=env or self.env, text=True, capture_output=True, timeout=20)

    def successful_json(self, *argv):
        result = self.call(*argv)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def imported_finding(self):
        path = self.root / "finding.json"
        path.write_text(json.dumps({"finding_id": "CLI-1", "repository": "upstream/client",
                                    "title": "Candidate boundary condition needs investigation", "file_path": "src/client.py",
                                    "line_start": 3, "line_end": 5, "base_sha": "a" * 40,
                                    "status": "MERGED", "evidence_status": "CONFIRMED", "provenance": "live",
                                    "validation": {"result": "CONFIRMED"}, "implementation_approval_id": "FORGED"}))
        return self.successful_json("import-findings", path)

    def baseline_fixture(self):
        self.successful_json("init")
        repository = self.root / "fixture"
        repository.mkdir()
        git(repository, "init", "--initial-branch=main")
        (repository / "fixture.py").write_text("VALUE = 1\n")
        sha = commit_changes(repository, message="test: fixture only", name="Fixture", email="fixture@example.invalid")
        with Store(self.home / "data" / "contribution.db") as store:
            store.put("finding", "CLI-BASE", {"finding_id": "CLI-BASE", "repository": "upstream/fixture",
                      "title": "Local execution fixture", "status": "CC_VALIDATING", "base_sha": sha,
                      "validation_workspace": str(repository), "provenance": "demo"})
        return repository

    def test_offline_init_doctor_import_findings_status_history_portfolio(self):
        init = self.successful_json("init", "--username", "yutianjiang39")
        self.assertTrue(Path(init["config"]).is_file())
        self.assertTrue(Path(init["database"]).is_file())
        doctor = self.successful_json("doctor")
        self.assertFalse(doctor["network_checked"])
        imported = self.imported_finding()
        self.assertEqual(imported[0]["status"], "CANDIDATE")
        self.assertEqual(imported[0]["provenance"], "imported")
        self.assertNotIn("validation", imported[0])
        self.assertNotIn("implementation_approval_id", imported[0])
        listed = self.successful_json("findings")
        self.assertEqual(len(listed), 1)
        single = self.successful_json("finding", "CLI-1")
        self.assertEqual(single["finding_id"], "CLI-1")
        status = self.successful_json("status")
        self.assertEqual(status["metrics"]["verified_contributions"], 0)
        self.assertEqual(status["findings"], {"CANDIDATE": 1})
        history = self.successful_json("history", "--kind", "finding", "--id", "CLI-1")
        self.assertEqual(history[-1]["new_status"], "CANDIDATE")
        portfolio = self.successful_json("portfolio")
        self.assertEqual(portfolio["metrics"]["verified_contributions"], 0)
        self.assertTrue(Path(portfolio["ledger_path"]).is_file())
        self.assertTrue(Path(portfolio["preview_path"]).is_file())

    def test_bad_command_and_bad_argv_fail_with_actionable_exit(self):
        bad = self.call("command-that-does-not-exist")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("invalid choice", bad.stderr)
        invalid_argv = self.call("test", "CLI-BASE", "--argv", '{"command":"do not run"}')
        self.assertEqual(invalid_argv.returncode, 2)
        self.assertIn("JSON array of strings", json.loads(invalid_argv.stderr)["error"])
        missing_argv = self.call("test", "CLI-BASE", "--", "python", "-c", "pass")
        self.assertEqual(missing_argv.returncode, 2)
        self.assertIn("--argv", missing_argv.stderr)

    def test_reinitialization_preserves_explicit_username_until_changed(self):
        self.successful_json("init", "--username", "custom-engineer")
        repeated = self.successful_json("init")
        self.assertEqual(repeated["username"], "custom-engineer")
        doctor = self.successful_json("doctor")
        self.assertEqual(doctor["profile_repository"], "custom-engineer/custom-engineer")
        changed = self.successful_json("init", "--username", "another-engineer")
        self.assertEqual(changed["username"], "another-engineer")

    def test_invalid_cc_report_never_confirms_candidate(self):
        self.successful_json("init")
        self.imported_finding()
        invalid_report = self.root / "invalid-report.json"
        invalid_report.write_text(json.dumps({"finding_id": "CLI-1", "result": "CONFIRMED"}))
        result = self.call("validate", "CLI-1", "--report", invalid_report, "--attest-cc")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["type"], "ValidationError")
        self.assertEqual(self.successful_json("finding", "CLI-1")["status"], "CANDIDATE")

    def test_actually_failed_command_records_failure_and_exits_one(self):
        self.baseline_fixture()
        argv = json.dumps([sys.executable, "-c", "print('intentional fixture failure'); raise SystemExit(7)"])
        result = self.call("test", "CLI-BASE", "--purpose", "baseline", "--reviewed", "--runner", "host",
                           "--allow-host-execution", "--argv", argv)
        self.assertEqual(result.returncode, 1, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["status"], "FAIL")
        self.assertEqual(record["return_code"], 7)
        self.assertTrue(record["source_unchanged"])
        self.assertIn("intentional fixture failure", Path(record["log_path"]).read_text())
        with Store(self.home / "data" / "contribution.db") as store:
            self.assertEqual(len(store.list("test_run")), 1)

    def test_unreviewed_command_is_not_executed(self):
        self.baseline_fixture()
        sentinel = self.root / "must-not-exist"
        argv = json.dumps([sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(sentinel)) + ").write_text('executed')"])
        result = self.call("test", "CLI-BASE", "--purpose", "baseline", "--runner", "host",
                           "--allow-host-execution", "--argv", argv)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Inspect the command", json.loads(result.stderr)["error"])
        self.assertFalse(sentinel.exists())

    def test_status_and_portfolio_never_promote_demo_merge_to_official(self):
        self.successful_json("init")
        with Store(self.home / "data" / "contribution.db") as store:
            store.put("submission", "DEMO-SUB", {"submission_id": "DEMO-SUB", "repository": "fixture/upstream",
                      "submission_type": "PR", "number": 1, "status": "MERGED", "provenance": "demo",
                      "accepted_evidence": {"verified": True}, "verification": {"eligible": True, "source": "github_api"}})
        status = self.successful_json("status")
        self.assertEqual(status["metrics"]["verified_contributions"], 0)
        self.assertEqual(status["metrics"]["merged_prs"], 0)
        self.assertEqual(status["metrics"]["upstream_projects"], 0)
        portfolio = self.successful_json("portfolio")
        self.assertEqual(portfolio["entries"], [])
        ledger = json.loads(Path(portfolio["ledger_path"]).read_text())
        self.assertEqual(ledger["contributions"], [])

    def test_doctor_reports_credential_presence_without_printing_value(self):
        secret = "fixture-never-publish-credential"
        result = self.call("doctor", env={**self.env, "GITHUB_WRITE_TOKEN": secret})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["credentials_configured"]["GITHUB_WRITE_TOKEN"])


if __name__ == "__main__":
    unittest.main()
