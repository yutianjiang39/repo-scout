import unittest
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from repo_scout import pilot


class QueueFlow(unittest.TestCase):
    def test_restart_review_and_retire(self):
        sha = "a" * 40
        records = dict(
            (key, data)
            for key, *data in pilot.findings(
                "example/sdk",
                sha,
                "README.md",
                "[Guide](docs/guide.md)\n",
                {"docs/Guide.md"},
            )
        )
        (key,) = records
        self.assertIn("+[Guide](docs/Guide.md)", records[key][-1])
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(pilot, "REPOS", ("example/sdk",)),
            patch.object(pilot.httpx, "Client") as client,
            patch.object(pilot, "scan", side_effect=[(1, records), (1, {})]) as scan,
        ):
            response = Mock(text=sha)
            client.return_value.__enter__.return_value.get.return_value.raise_for_status.return_value = response

            def run(*args):
                out = StringIO()
                with (
                    patch("sys.argv", ["pilot", "--home", str(Path(temp)), *args]),
                    redirect_stdout(out),
                ):
                    pilot.main()
                return out.getvalue()

            response.json.return_value = {
                "incomplete_results": False,
                "total_count": 0,
                "items": [],
            }
            run("tick")
            run("tick")
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(json.loads(run("queue"))[0]["id"], key)
            self.assertIn(key, run("review"))
            with self.assertRaises(SystemExit):
                run(
                    "judge",
                    key,
                    "worth",
                    "--sha",
                    "stale",
                    "--reason",
                    "verified casing",
                )
            run("judge", key, "worth", "--sha", sha, "--reason", "verified casing")
            self.assertEqual(json.loads(run("stats"))["worth_ratio"], 1)
            response.text = "b" * 40
            run("tick")
            self.assertEqual(json.loads(run("show", key))[0]["current"], 0)

    def test_duplicate_pr_and_incomplete_search(self):
        client = Mock()
        result = client.get.return_value.raise_for_status.return_value
        result.json.return_value = {
            "items": [{"title": "Fix README.md link", "body": "docs/guide.md"}],
            "total_count": 1,
            "incomplete_results": False,
        }
        title = "Broken relative link to docs/guide.md in README.md"
        self.assertTrue(pilot.upstream_duplicate(client, "example/sdk", title))
        result.json.return_value["incomplete_results"] = True
        with self.assertRaises(ValueError):
            pilot.upstream_duplicate(client, "example/sdk", title)
