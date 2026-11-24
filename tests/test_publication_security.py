from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_scout.audit import review_diff
from repo_scout.implementation import commit_changes, git
from repo_scout.validation import run_claude_json
from repo_scout.workflow import _inspect_publish_history


class ModelContextBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_final_diff_secret_is_blocked_before_any_provider(self):
        secret = "sk-" + "x" * 40
        with patch("repo_scout.audit.ProviderRegistry", side_effect=AssertionError("Provider must not be constructed")):
            with self.assertRaisesRegex(ValueError, "credential"):
                await review_diff("+api_key = '" + secret + "'", {}, None)

    def test_cc_context_secret_is_blocked_before_subprocess(self):
        with patch("repo_scout.validation.subprocess.run", side_effect=AssertionError("CC must not be executed")):
            with self.assertRaisesRegex(ValueError, "credential"):
                run_claude_json("api_key = 'sk-" + "x" * 40 + "'", {})


class HistoricalPublicationBoundaries(unittest.TestCase):
    def test_removed_secret_is_still_blocked_in_pushed_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git(root, "init", "--initial-branch=main")
            (root / "client.py").write_text("value = 1\n")
            base = commit_changes(root, message="fixture", name="Test", email="test@example.invalid")
            (root / "client.py").write_text("api_key = 'sk-" + "x" * 40 + "'\n")
            commit_changes(root, message="accidentally include credential fixture", name="Test", email="test@example.invalid")
            (root / "client.py").write_text("value = 2\n")
            commit_changes(root, message="remove fixture credential", name="Test", email="test@example.invalid")
            with self.assertRaisesRegex(ValueError, "history contains"):
                _inspect_publish_history(root, base, ["client.py"])
