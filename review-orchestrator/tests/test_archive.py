import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ro import archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        t = self.root / "repos" / "petl" / "tasks" / "issue-1"
        (t / "v1").mkdir(parents=True)
        (t / "v1" / "candidate.diff").write_text("diff\n")
        (t / "v1" / "gpt-first.md").write_text("PASS\n")
        (t / "opencode-gpt-1").mkdir()
        (t / "opencode-gpt-1" / "cli.jsonl").write_text("{}\n")
        (t / "checkout" / ".git").mkdir(parents=True)
        (t / "checkout" / "a.py").write_text("x = 1\n")
        self.task = t
        self.conn = archive.connect(self.root / "records.sqlite")

    def test_import_is_idempotent_and_skips_sessions_and_checkouts(self):
        self.assertEqual(archive.import_repos(self.conn, self.root / "repos"), 2)
        self.assertEqual(archive.import_repos(self.conn, self.root / "repos"), 0)
        rows = dict(self.conn.execute("SELECT relpath, kind FROM artifacts"))
        self.assertEqual(rows, {"v1/candidate.diff": "patch", "v1/gpt-first.md": "report"})
        self.assertEqual(self.conn.execute("SELECT files, session_files FROM tasks").fetchone(), (2, 1))

    def test_changed_or_missing_file_is_reported_and_history_kept(self):
        archive.import_repos(self.conn, self.root / "repos")
        (self.task / "v1" / "gpt-first.md").write_text("REVISE\n")
        (self.task / "v1" / "candidate.diff").unlink()
        self.assertEqual(sorted(k for k, _ in archive.verify(self.conn)), ["changed", "missing"])
        archive.import_repos(self.conn, self.root / "repos")
        n = self.conn.execute("SELECT COUNT(*) FROM artifacts WHERE relpath='v1/gpt-first.md'").fetchone()[0]
        self.assertEqual(n, 2, "old hash must be kept, new hash added")

    def test_events_are_append_only(self):
        archive.import_repos(self.conn, self.root / "repos")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM events")


if __name__ == "__main__":
    unittest.main()
