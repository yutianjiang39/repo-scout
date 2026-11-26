import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ro import upstream


def node(n, updated, state="OPEN"):
    return dict(number=n, title=f"t{n}", state=state, updatedAt=updated)


class FakeGitHub:
    """Serves items newest-first, 50 per page, and counts item fetches."""

    def __init__(self, items):
        self.items, self.served = items, 0

    def __call__(self, query, **v):
        if "defaultBranchRef" in query:
            return {"repository": {"defaultBranchRef": {"target": {"oid": "abc"}}}}
        kind = "issues" if "issues(" in query else "pullRequests"
        rows = sorted(self.items[kind], key=lambda x: x["updatedAt"], reverse=True)
        start = int(v.get("c") or 0)
        page = rows[start:start + 50]
        self.served += len(page)
        return {"repository": {kind: {"totalCount": len(rows), "nodes": page, "pageInfo": dict(
            hasNextPage=start + 50 < len(rows), endCursor=str(start + 50))}}}


class UpstreamIndex(unittest.TestCase):
    def test_second_sync_only_fetches_changed_items(self):
        items = {"issues": [node(i, f"2026-01-{i % 28 + 1:02d}T00:00:00Z") for i in range(1, 121)],
                 "pullRequests": [node(i, "2026-01-01T00:00:00Z") for i in range(1, 4)]}
        fake = FakeGitHub(items)
        with tempfile.TemporaryDirectory() as d, patch.object(upstream, "gh", fake):
            conn = upstream.connect(Path(d) / "index.sqlite")
            upstream.sync(conn, "o/n", log=lambda m: None)
            self.assertEqual(fake.served, 123)
            items["issues"].append(node(500, "2026-03-01T00:00:00Z"))
            items["issues"][0] = node(1, "2026-03-02T00:00:00Z", "CLOSED")
            fake.served = 0
            upstream.sync(conn, "o/n", log=lambda m: None)
            self.assertLess(fake.served, 60, "incremental sync must stop at the last sync point")
            self.assertEqual(conn.execute("SELECT state FROM items WHERE number=1 AND kind='issues'").fetchone(),
                             ("CLOSED",))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items WHERE kind='issues'").fetchone(), (121,))
            conn.close()


if __name__ == "__main__":
    unittest.main()
