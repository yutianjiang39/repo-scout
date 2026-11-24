"""Independent regression tests for evidence, isolation and authorization gates."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_scout.common import digest, now
from repo_scout.database import ConflictError, Store
from repo_scout.implementation import create_worktree, git, head, run_command, snapshot
from repo_scout.validation import accept_validation


class DatabaseEdges(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_compare_and_swap_preserves_winner_and_history(self):
        first = self.store.put("finding", "F-1", {"status": "CANDIDATE"})
        self.store.put("finding", "F-1", {"status": "CC_VALIDATING"}, expected_version=first["_version"])
        with self.assertRaises(ConflictError):
            self.store.put("finding", "F-1", {"status": "CONFIRMED"}, expected_version=first["_version"])
        self.assertEqual(self.store.require("finding", "F-1")["status"], "CC_VALIDATING")
        self.assertEqual(len(self.store.events("finding", "F-1")), 2)

    def test_failed_transaction_rolls_back_record_and_event_together(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.atomic():
                self.store.put("finding", "F-2", {"status": "CANDIDATE"})
                raise RuntimeError("abort")
        self.assertIsNone(self.store.get("finding", "F-2"))
        self.assertEqual(self.store.events("finding", "F-2"), [])

    def test_events_cannot_be_edited_or_deleted(self):
        self.store.put("finding", "F-3", {"status": "CANDIDATE"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("UPDATE events SET new_status='MERGED'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM events")
        self.assertEqual(self.store.events()[0]["new_status"], "CANDIDATE")

    def test_approval_matches_content_and_is_single_use(self):
        content = digest({"body": "Reviewed final PR content", "head": "a" * 40})
        approval = self.store.approve("submit", "F-4", content, actor="operator", reason="Reviewed this exact diff")
        with self.assertRaises(ValueError):
            self.store.check_approval("submit", "F-4", digest({"body": "Different PR content"}))
        self.assertEqual(self.store.check_approval("submit", "F-4", content)["id"], approval["id"])
        consumed = self.store.check_approval("submit", "F-4", content, consume=True)
        self.assertIsNotNone(consumed["consumed_at"])
        with self.assertRaises(ValueError):
            self.store.check_approval("submit", "F-4", content, consume=True)

    def test_expired_approval_is_rejected(self):
        approval = self.store.approve("implementation", "F-5", "digest", actor="operator", reason="Reviewed change")
        self.store.connection.execute("UPDATE approvals SET expires_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", approval["id"]))
        with self.assertRaises(ValueError):
            self.store.check_approval("implementation", "F-5", "digest")

    def test_outbox_id_cannot_be_reused_for_different_request(self):
        self.store.begin_operation("op-1", {"title": "One issue"})
        with self.assertRaises(ConflictError):
            self.store.begin_operation("op-1", {"title": "Another issue"})
        self.assertEqual(self.store.operation("op-1")["payload"]["title"], "One issue")


class WorktreeEvidenceEdges(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / "original"
        self.original.mkdir()
        git(self.original, "init", "-q")
        git(self.original, "config", "user.name", "Local Test")
        git(self.original, "config", "user.email", "test@example.invalid")
        (self.original / "module.py").write_text("def increment(value):\n    return value + 2\n", encoding="utf-8")
        git(self.original, "add", "module.py")
        git(self.original, "commit", "-qm", "fixture")
        self.sha = head(self.original)
        self.store = Store(self.root / "data.db")
        self.validation_workspace, _ = create_worktree(self.root, self.original, "F-EDGE", self.sha, validation=True)
        self.workspace, self.branch = create_worktree(self.root, self.original, "F-EDGE", self.sha)
        self.finding = {
            "finding_id": "F-EDGE", "repository": "upstream/tool", "title": "Increment result is too high",
            "category": "correctness", "evidence_status": "INFERRED", "base_sha": self.sha,
            "status": "CC_VALIDATING", "validation_workspace": str(self.validation_workspace),
            "workspace": str(self.workspace), "provenance": "demo",
            "preflight": {"source": "github_api", "head_sha": self.sha, "checked_at": now(),
                          "duplicates": {"complete": True, "issues": [], "prs": []}},
        }
        self.store.put("finding", "F-EDGE", self.finding)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_fixture(self, code: str, *, purpose="baseline", extra_argv=None):
        return run_command(
            self.store, self.root, self.finding, [sys.executable, "-c", code, *(extra_argv or [])],
            purpose=purpose, reviewed=True, runner="host", allow_host=True, timeout=10,
        )

    def report(self, run):
        return {
            "finding_id": "F-EDGE", "session_id": "fixture-independent-cc", "source_sha": self.sha,
            "result": "CONFIRMED", "root_cause": "Increment adds two instead of one.",
            "evidence": ["module.py:2"], "trigger_condition": "Any integer input",
            "actual_behavior": "Input zero yields two", "expected_behavior": "Input zero yields one",
            "protection_analysis": "No caller normalization", "design_compatibility": "API promises increment",
            "duplicate_assessment": "Reviewed mock search", "already_fixed_assessment": "Current fixture reproduces",
            "recommended_action": "PR", "impact": "Incorrect result", "regression_risk": "Small pure-function fix",
            "baseline_run_id": run["run_id"], "failure_signature": "AssertionError: increment mismatch",
            "reproducible": True,
        }

    def test_worktree_edits_do_not_touch_original_or_other_finding(self):
        other, other_branch = create_worktree(self.root, self.original, "F-OTHER", self.sha)
        (self.workspace / "module.py").write_text("def increment(value):\n    return value + 1\n")
        self.assertIn("value + 2", (self.original / "module.py").read_text())
        self.assertIn("value + 2", (other / "module.py").read_text())
        self.assertNotEqual(self.branch, other_branch)
        self.assertEqual(git(self.original, "status", "--porcelain"), "")

    def test_source_hash_changes_for_untracked_test_and_file_edit(self):
        initial = snapshot(self.workspace, self.sha)
        (self.workspace / "new_test.py").write_text("assert True\n")
        added = snapshot(self.workspace, self.sha)
        self.assertNotEqual(initial["tree_hash"], added["tree_hash"])
        self.assertIn("new_test.py", added["changed_files"])
        (self.workspace / "module.py").write_text("raise RuntimeError('changed')\n")
        edited = snapshot(self.workspace, self.sha)
        self.assertNotEqual(added["tree_hash"], edited["tree_hash"])

    def test_source_hash_does_not_drop_leading_whitespace_filename(self):
        target = self.workspace / " leading_space.py"
        target.write_text("value = 1\n")
        git(self.workspace, "add", "--", target.name)
        git(self.workspace, "commit", "-qm", "fixture with legal whitespace filename")
        first = snapshot(self.workspace, self.sha)
        target.write_text("value = 2\n")
        second = snapshot(self.workspace, self.sha)
        self.assertNotEqual(first["tree_hash"], second["tree_hash"])
        self.assertIn(target.name, second["changed_files"])

    def test_preflight_worktree_can_advance_without_overwriting_old_evidence(self):
        note = self.validation_workspace / "investigation.txt"
        note.write_text("Evidence retained at previous upstream commit")
        (self.original / "module.py").write_text("def increment(value):\n    return value + 1\n")
        git(self.original, "add", "module.py")
        git(self.original, "commit", "-qm", "upstream advances")
        newer_sha = head(self.original)
        newer, _ = create_worktree(self.root, self.original, "F-EDGE", newer_sha, validation=True)
        self.assertEqual(head(newer), newer_sha)
        self.assertNotEqual(newer, self.validation_workspace)
        self.assertEqual(note.read_text(), "Evidence retained at previous upstream commit")

    def test_review_and_host_opt_in_are_required_before_execution(self):
        command = [sys.executable, "-c", "raise SystemExit(0)"]
        with self.assertRaises(ValueError):
            run_command(self.store, self.root, self.finding, command, purpose="baseline", reviewed=False, runner="host", allow_host=True)
        with self.assertRaises(ValueError):
            run_command(self.store, self.root, self.finding, command, purpose="baseline", reviewed=True, runner="host", allow_host=False)
        self.assertEqual(self.store.list("test_run"), [])

    def test_child_does_not_receive_parent_api_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret-openai-value", "GITHUB_TOKEN": "test-secret-github-value"}):
            run = self.run_fixture("import os; assert 'OPENAI_API_KEY' not in os.environ; assert 'GITHUB_TOKEN' not in os.environ; print('credentials absent')")
        self.assertEqual(run["status"], "PASS")
        self.assertIn("credentials absent", Path(run["log_path"]).read_text())
        self.assertNotIn("test-secret", Path(run["log_path"]).read_text())

    def test_command_and_output_do_not_persist_known_credentials(self):
        secret = "test-only-sensitive-api-credential-12345"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            run = self.run_fixture("import sys; print(sys.argv[1])", extra_argv=[secret])
        self.assertNotIn(secret, Path(run["log_path"]).read_text())
        self.assertNotIn(secret, json.dumps(self.store.require("test_run", run["run_id"])))
        self.assertNotIn(secret, (Path(run["log_path"]).parent / "run.json").read_text())

    def test_failure_is_recorded_as_failure_and_validates_only_actual_log(self):
        run = self.run_fixture("from module import increment; assert increment(0) == 1, 'increment mismatch'")
        self.assertEqual(run["status"], "FAIL")
        self.assertNotEqual(run["return_code"], 0)
        self.assertTrue(run["source_unchanged"])
        saved = accept_validation(self.store, self.root, "F-EDGE", self.report(run), attested_cc=True)
        self.assertEqual(saved["status"], "CONFIRMED")

    def test_passing_command_cannot_be_claimed_as_reproduction(self):
        run = self.run_fixture("print('AssertionError: increment mismatch')")
        self.assertEqual(run["status"], "PASS")
        with self.assertRaises(ValueError):
            accept_validation(self.store, self.root, "F-EDGE", self.report(run), attested_cc=True)

    def test_incomplete_duplicate_search_prevents_confirmation(self):
        run = self.run_fixture("from module import increment; assert increment(0) == 1, 'increment mismatch'")
        finding = self.store.require("finding", "F-EDGE")
        finding["preflight"]["duplicates"]["complete"] = False
        self.store.put("finding", "F-EDGE", finding)
        with self.assertRaises(ValueError):
            accept_validation(self.store, self.root, "F-EDGE", self.report(run), attested_cc=True)

    def test_modified_log_cannot_validate_a_bug(self):
        run = self.run_fixture("raise AssertionError('increment mismatch')")
        Path(run["log_path"]).write_text("AssertionError: increment mismatch\nmodified after capture")
        with self.assertRaises(ValueError):
            accept_validation(self.store, self.root, "F-EDGE", self.report(run), attested_cc=True)

    def test_modified_baseline_source_is_rejected_before_execution(self):
        (self.validation_workspace / "module.py").write_text("def increment(value):\n    return value + 1\n")
        with self.assertRaises(ValueError):
            self.run_fixture("raise AssertionError('increment mismatch')")
        self.assertEqual(self.store.list("test_run"), [])

    def test_source_modified_during_execution_cannot_validate_a_bug(self):
        run = self.run_fixture("from pathlib import Path; Path('module.py').write_text('changed'); raise AssertionError('increment mismatch')")
        self.assertFalse(run["source_unchanged"])
        with self.assertRaises(ValueError):
            accept_validation(self.store, self.root, "F-EDGE", self.report(run), attested_cc=True)

    def test_identical_bytes_new_commit_is_still_source_change(self):
        run = self.run_fixture(
            "import subprocess; subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', "
            "'commit', '--allow-empty', '-qm', 'execution changed HEAD'], check=True); "
            "raise AssertionError('increment mismatch')"
        )
        self.assertNotEqual(head(self.validation_workspace), self.sha)
        self.assertFalse(run["source_unchanged"])
        with self.assertRaises(ValueError):
            accept_validation(self.store, self.root, "F-EDGE", self.report(run), attested_cc=True)


if __name__ == "__main__":
    unittest.main()
