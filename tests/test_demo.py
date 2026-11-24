from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repo_scout.database import Store
from repo_scout.demo import BUGGY_SOURCE, DEMO_USER, FINDING_ID, run_demo
from repo_scout.portfolio import sync_ledger


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_local_failure_fix_review_revision_and_no_official_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated-demo"
            # Any accidental public write or network use must fail this test.
            with patch("socket.socket.connect", side_effect=AssertionError("Demo must remain offline")), \
                    patch("repo_scout.workflow.submit", side_effect=AssertionError("Demo must not submit")), \
                    patch("repo_scout.implementation.push_fork", side_effect=AssertionError("Demo must not push")):
                report = await run_demo(root)
            self.assertTrue(report["baseline_fail"])
            self.assertTrue(report["regression_pass"])
            self.assertTrue(report["revision_pass"])
            self.assertTrue(report["original_unchanged"])
            self.assertNotEqual(report["first_head"], report["revised_head"])
            self.assertEqual(report["actual_test_runs"], 3)
            self.assertEqual(report["official_contributions"], 0)
            self.assertTrue(report["excluded_demo"])
            self.assertTrue(report["records_all_demo"])
            self.assertEqual((report["network_requests"], report["model_requests"], report["public_writes"]), (0, 0, 0))
            statuses = [entry["status"] for entry in report["review_loop"]]
            self.assertEqual(statuses, ["UNDER_REVIEW", "CHANGES_REQUESTED", "CHANGES_REQUESTED", "APPROVED", "MERGED"])
            self.assertIn("AssertionError: 4 != 2", Path(report["baseline_log"]).read_text())
            self.assertIn("Ran 3 tests", Path(report["revision_log"]).read_text())
            self.assertIn("OK", Path(report["revision_log"]).read_text())
            self.assertIn("DEMO", (root / "reports/validation" / FINDING_ID / "report.md").read_text())
            self.assertTrue((root / "portfolio/profile_readme_draft.md").read_text().startswith("# DEMO contributor"))
            self.assertEqual(json.loads((root / "portfolio/contribution_ledger.json").read_text())["contributions"], [])
            with Store(root / "data/contribution.db") as store:
                self.assertEqual(sync_ledger(store, username=DEMO_USER), [])
                self.assertEqual(store.require("finding", FINDING_ID)["provenance"], "demo")
                self.assertEqual([r["status"] for r in sorted(store.list("test_run"), key=lambda r: r["started_at"])],
                                 ["FAIL", "PASS", "PASS"])
                rows = [json.loads(row[0]) for row in store.connection.execute("SELECT data FROM records")]
                self.assertTrue(all(row["provenance"] == "demo" for row in rows))
                self.assertTrue(all(event["actor"].startswith("DEMO:") for event in store.events()))

    async def test_existing_work_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.txt"
            existing.write_text("keep me")
            with self.assertRaisesRegex(ValueError, "empty dedicated"):
                await run_demo(root)
            self.assertEqual(existing.read_text(), "keep me")
            self.assertEqual(list(root.iterdir()), [existing])


if __name__ == "__main__":
    unittest.main()
