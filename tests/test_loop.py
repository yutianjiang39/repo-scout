"""Exercise the contribution loop across real local Git, SQLite, and mocked GitHub.

Only the tiny repository created by this test is executed. Provider/CC reports
and GitHub reads/writes are fixtures; no account, credential, or network required.
"""
from __future__ import annotations

import base64
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_scout.common import digest, now
from repo_scout.database import Store
from repo_scout.followup import record_issue_acceptance, sync_submission
from repo_scout.github import GitHubError
from repo_scout.implementation import (commit_changes, git, head, run_command)
from repo_scout.portfolio import sync_ledger
from repo_scout.validation import _apply_assessment as accept_validation, preflight
from repo_scout.workflow import (add_finding, approve_finding, approve_profile,
                                 approve_submission, begin_implementation,
                                 mark_testing, prepare_draft, prepare_profile,
                                 publish_profile, reconcile_submission,
                                 record_diff_review, submit)

USER = "yutianjiang39"
UPSTREAM = "engine-maintainer/arithmetic-client"
FORK = USER + "/arithmetic-client"
PROFILE = USER + "/" + USER
COMMAND = [sys.executable, "-c", "from maths import double; assert double(2) == 4, 'DOUBLE_REGRESSION'"]


class MockGitHub:
    def __init__(self, sha: str):
        self.sha = sha
        self.upstream = {"id": 101, "full_name": UPSTREAM, "owner": {"login": "engine-maintainer"},
                         "fork": False, "private": False, "archived": False,
                         "html_url": "https://github.com/" + UPSTREAM, "default_branch": "main"}
        self.profile = {"id": 102, "full_name": PROFILE, "owner": {"login": USER}, "fork": False,
                        "private": False, "html_url": "https://github.com/" + PROFILE, "default_branch": "main"}
        self.fork = {"id": 103, "full_name": FORK, "owner": {"login": USER}, "fork": True,
                     "private": False, "parent": self.upstream, "default_branch": "main"}
        self.original_readme = "# My engineering work\n\nPersonal introduction stays exactly here.\n\n## Projects\nKeep these projects.\n"
        self.readme = self.original_readme
        self.readme_sha = "c" * 40
        self.posts: list[dict] = []
        self.puts: list[dict] = []
        self.remote_issue: dict | None = None
        self.remote_pull: dict | None = None
        self.comments: list[dict] = []
        self.reviews: list[dict] = []
        self.pushed_sha = ""
        self.push_count = 0
        self.lose_post_response = False
        self.reject_post_status = None
        self.expected_approved_sha = None
        self.search_items: list[dict] = []

    async def repository(self, slug, fresh=False):
        if not fresh:
            raise AssertionError("Contribution decisions must use fresh upstream reads")
        return copy.deepcopy({UPSTREAM: self.upstream, PROFILE: self.profile, FORK: self.fork}[slug])

    async def authenticated_user(self, *, write=False):
        if not write:
            raise AssertionError("Submission must verify the write identity")
        return {"login": USER, "id": 301}

    async def get(self, path, params=None, fresh=False):
        if not fresh:
            raise AssertionError("Final review must use a fresh read")
        if path == f"/repos/{UPSTREAM}/commits/main":
            return {"sha": self.sha}
        if path in {f"/repos/{PROFILE}/readme", f"/repos/{PROFILE}/contents/README.md"}:
            return {"path": "README.md", "sha": self.readme_sha, "encoding": "base64",
                    "content": base64.b64encode(self.readme.encode()).decode(),
                    "html_url": f"https://github.com/{PROFILE}/blob/main/README.md"}
        if path.startswith(f"/repos/{FORK}/git/ref/heads/"):
            return {"object": {"sha": self.pushed_sha}}
        if path == f"/repos/{UPSTREAM}/pulls/7":
            return copy.deepcopy(self.remote_pull)
        raise AssertionError(f"Unexpected mocked read: {path}")

    async def duplicates(self, slug, title):
        return {"checked_at": now(), "repository": slug, "status": "SEARCHED", "complete": True,
                "issues": copy.deepcopy(self.search_items), "prs": [],
                "issues_query": "fixture", "prs_query": "fixture",
                "conclusion": "HUMAN_REVIEW_REQUIRED" if self.search_items else "NO_KEYWORD_MATCH"}

    async def post(self, path, payload, *, authorized):
        if not authorized:
            raise AssertionError("Write must carry explicit authorization")
        self.posts.append({"path": path, "payload": copy.deepcopy(payload)})
        if self.reject_post_status:
            raise GitHubError("Simulated definitive server rejection", status_code=self.reject_post_status)
        common = {"id": 201, "user": {"login": USER}, "title": payload["title"], "body": payload["body"],
                  "state": "open", "created_at": now(), "updated_at": now(), "closed_at": None}
        if path == f"/repos/{UPSTREAM}/issues":
            self.remote_issue = {**common, "number": 8, "state_reason": None,
                                 "html_url": f"https://github.com/{UPSTREAM}/issues/8",
                                 "repository_url": f"https://api.github.com/repos/{UPSTREAM}"}
            response = self.remote_issue
        elif path == f"/repos/{UPSTREAM}/pulls":
            self.remote_pull = {**common, "number": 7, "html_url": f"https://github.com/{UPSTREAM}/pull/7",
                                "base": {"repo": self.upstream}, "head": {"sha": self.pushed_sha},
                                "merged": False, "draft": False, "merged_by": None, "merged_at": None,
                                "merge_commit_sha": None}
            response = self.remote_pull
        else:
            raise AssertionError(f"Unexpected mocked mutation: {path}")
        if self.lose_post_response:
            raise TimeoutError("Simulated accepted write with lost response")
        return copy.deepcopy(response)

    async def put(self, path, payload, *, authorized):
        if path != f"/repos/{PROFILE}/contents/README.md" or not authorized:
            raise AssertionError("Only an explicitly approved Profile update is expected")
        if payload["sha"] != self.readme_sha:
            raise AssertionError("Stale optimistic content version")
        self.puts.append(copy.deepcopy(payload))
        self.readme = base64.b64decode(payload["content"]).decode()
        self.readme_sha = digest(self.readme)[:40]
        return {"content": {"sha": self.readme_sha, "html_url": f"https://github.com/{PROFILE}/blob/main/README.md"}}

    async def paginate(self, path, params=None, fresh=False, max_pages=10):
        if path == f"/repos/{UPSTREAM}/issues":
            return [copy.deepcopy(self.remote_issue)] if self.remote_issue else []
        if path == f"/repos/{UPSTREAM}/pulls":
            return [copy.deepcopy(self.remote_pull)] if self.remote_pull else []
        raise AssertionError(f"Unexpected mocked pagination: {path}")

    async def issue_snapshot(self, slug, number):
        return {"issue": copy.deepcopy(self.remote_issue), "comments": copy.deepcopy(self.comments),
                "timeline": [], "complete": True}

    async def pull_snapshot(self, slug, number):
        return {"pull": copy.deepcopy(self.remote_pull), "reviews": copy.deepcopy(self.reviews),
                "comments": [], "review_comments": [], "complete": True,
                "checks": {"available": False, "check_runs": []},
                "statuses": {"available": False, "statuses": []},
                "errors": ["checks unavailable (HTTP 403)"]}

    def push(self, path, *, fork, branch, token, head_sha):
        if fork != FORK or not branch.startswith("contrib/") or token != "mock-token-not-a-real-credential":
            raise AssertionError("Unexpected push destination or authorization")
        if not head_sha or head_sha != self.expected_approved_sha or head(path) != head_sha:
            raise AssertionError("Push must use the immutable explicitly approved commit SHA")
        self.push_count += 1
        self.pushed_sha = head_sha
        if self.remote_pull:
            self.remote_pull["head"]["sha"] = self.pushed_sha


class ContributionLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / "original"
        self.original.mkdir()
        git(self.original, "init", "--initial-branch=main")
        (self.original / "maths.py").write_text("def double(value):\n    return value + 1\n")
        (self.original / "README.md").write_text("# Arithmetic\n\n`double(value)` returns value * 2.\n")
        self.sha = commit_changes(self.original, message="fixture: initial source", name="Fixture", email="fixture@example.invalid")
        self.store = Store(self.root / "contribution.db")
        from repo_scout.demo_review import FixtureCollector
        self.fixture = FixtureCollector(self.store)
        self.client = MockGitHub(self.sha)
        self.config = {"username": USER, "profile_repository": PROFILE,
                       "validation": {"preflight_max_age_hours": 24},
                       "github": {"write_token_env": "SCOUT_TEST_WRITE_TOKEN"},
                       "tracking": {"trusted_maintainers": {}}}
        self.env = patch.dict(os.environ, {"SCOUT_TEST_WRITE_TOKEN": "mock-token-not-a-real-credential"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.store.close()
        self.temp.cleanup()

    async def validated(self, action="ISSUE"):
        add_finding(self.store, {"finding_id": "F-LOOP", "repository": UPSTREAM,
                               "title": "Double returns the wrong result for two", "category": "correctness",
                               "file_path": "maths.py", "line_start": 1, "line_end": 2, "base_sha": self.sha,
                               "impact": "The arithmetic result differs from the documented contract"})
        value = await preflight(self.store, self.client, self.root, "F-LOOP", original_path=self.original)
        baseline = run_command(self.store, self.root, value, COMMAND, purpose="baseline", reviewed=True,
                               runner="host", allow_host=True, timeout=10)
        self.assertEqual(baseline["status"], "FAIL")
        report = {"finding_id": "F-LOOP", "validator": "claude-code", "session_id": "mock-independent-cc-session",
                  "source_sha": self.sha, "result": "CONFIRMED", "root_cause": "Addition is used where multiplication is documented.",
                  "evidence": [f"{UPSTREAM}/maths.py:2", f"Actual local baseline {baseline['run_id']}"],
                  "trigger_condition": "Call double(2)", "actual_behavior": "Returns 3", "expected_behavior": "Returns 4",
                  "protection_analysis": "No caller normalizes this result", "design_compatibility": "README promises multiplication",
                  "duplicate_assessment": "Mocked issue/PR search is empty; no semantic duplicate observed",
                  "already_fixed_assessment": "Latest verified fixture SHA still uses addition", "recommended_action": action,
                  "impact": "Incorrect arithmetic", "regression_risk": "Small; input boundary tests required",
                  "baseline_run_id": baseline["run_id"], "failure_signature": "DOUBLE_REGRESSION", "reproducible": True}
        accept_validation(self.store, self.root, "F-LOOP", report, attested_cc=True,
                                 provenance="test-fixture-independent-validator")
        self.fixture.review('F-LOOP','finding')
        return self.store.require('finding','F-LOOP')

    async def prepared_issue(self):
        await self.validated()
        self.fixture.approve(approve_finding,self.store, "F-LOOP", actor=USER, plan="Discuss the documented arithmetic behavior with upstream", scope=[])
        return prepare_draft(self.store, self.root, "F-LOOP", submission_type="ISSUE",
                             title="double(2) returns 3 instead of documented 4",
                             body="Reproduction: call double(2) at the verified commit. Expected 4; observed 3. "
                                  "The local regression command fails with DOUBLE_REGRESSION. Please confirm the contract.",
                             risk="No code change; maintainer discussion requested")

    async def approved_issue(self):
        draft = await self.prepared_issue()
        self.fixture.review('F-LOOP','submission')
        self.fixture.approve(approve_submission,self.store, "F-LOOP", reviewed_digest=draft["digest"], actor=USER,
                           reason="Reviewed the precise upstream issue and reproduction")
        return draft

    async def accepted_issue(self):
        await self.approved_issue()
        sub = await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.client.comments = [{"id": 501, "user": {"login": "engine-maintainer"}, "author_association": "OWNER",
                                 "body": "Confirmed: this violates the intended contract. A focused fix is welcome.",
                                 "html_url": f"https://github.com/{UPSTREAM}/issues/8#issuecomment-501",
                                 "created_at": now(), "updated_at": now()}]
        await sync_submission(self.store, self.client, sub["submission_id"])
        self.assertEqual(sync_ledger(self.store), [])
        return record_issue_acceptance(self.store, sub["submission_id"],
                                        evidence_url=self.client.comments[0]["html_url"], actor=USER,
                                        rationale="Maintainer explicitly confirmed the bug and welcomed the fix.")

    async def submitted_pr(self):
        await self.validated("PR")
        self.fixture.approve(approve_finding,self.store, "F-LOOP", actor=USER, plan="Replace addition with multiplication in the one affected function", scope=["maths.py"])
        implementation = begin_implementation(self.store, self.root, "F-LOOP")
        workspace = Path(implementation["workspace"])
        (workspace / "maths.py").write_text("def double(value):\n    return value * 2\n")
        commit_changes(workspace, message="fix: honor double arithmetic contract", name="Fixture", email="fixture@example.invalid")
        finding = mark_testing(self.store, "F-LOOP")
        run = run_command(self.store, self.root, finding, COMMAND, purpose="regression", reviewed=True,
                          runner="host", allow_host=True, timeout=10)
        self.assertEqual(run["status"], "PASS")
        record_diff_review(self.store, "F-LOOP", actor="independent-test-reviewer",
                           assessment="The one-line patch fixes the reproduced contract violation without unrelated changes", approved=True)
        self.fixture.review('F-LOOP','diff')
        draft = prepare_draft(self.store, self.root, "F-LOOP", submission_type="PR", fork=FORK,
                              title="Fix documented double arithmetic contract",
                              body="Fixes the reproduced wrong result with a focused arithmetic change. "
                                   "The same baseline command now passes against the committed patch.", risk="Small arithmetic behavior correction")
        self.fixture.review('F-LOOP','submission')
        self.fixture.approve(approve_submission,self.store, "F-LOOP", reviewed_digest=draft["digest"], actor=USER, reason="Reviewed diff and recorded test results")
        self.client.expected_approved_sha = draft["draft"]["head_sha"]
        return await submit(self.store, self.client, self.root, self.config, "F-LOOP", push_function=self.client.push)

    async def test_issue_contribution_to_approved_profile_preserves_intro(self):
        accepted = await self.accepted_issue()
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.push_count, 0)
        self.assertEqual(len(sync_ledger(self.store)), 1)
        profile = await prepare_profile(self.store, self.client, self.root, self.config)
        self.assertTrue(profile["draft"]["content"].startswith(self.client.original_readme))
        self.assertIn("Maintainer accepted", profile["draft"]["content"])
        self.assertIn(self.client.comments[0]["html_url"], profile["draft"]["content"])
        with self.assertRaises(ValueError):
            await publish_profile(self.store, self.client, self.config)
        self.assertEqual(self.client.puts, [])
        self.fixture.review(PROFILE,'profile')
        self.fixture.approve(approve_profile,self.store, self.config, reviewed_digest=profile["digest"], actor=USER,
                        reason="Reviewed contributions-only Profile diff")
        first = await publish_profile(self.store, self.client, self.config)
        again = await publish_profile(self.store, self.client, self.config)
        self.assertEqual(first, again)
        self.assertEqual(len(self.client.puts), 1)
        self.assertTrue(self.client.readme.startswith(self.client.original_readme))

    async def test_changed_issue_payload_invalidates_final_approval(self):
        draft = await self.approved_issue()
        current = self.store.require("draft", "F-LOOP")
        current["draft"]["body"] += "\nAn additional unreviewed claim."
        current["digest"] = digest(current["draft"])
        self.store.put("draft", "F-LOOP", current, reason="simulate an edited pending draft")
        self.assertNotEqual(current["digest"], draft["digest"])
        with self.assertRaises(ValueError):
            await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(self.client.posts, [])

    async def test_submission_without_second_approval_has_no_side_effect(self):
        await self.prepared_issue()
        with self.assertRaises(ValueError):
            await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.store.list("submission"), [])

    async def test_reopening_review_after_approval_blocks_submit_before_public_writes(self):
        from repo_scout import review_gate as gate
        await self.approved_issue()
        gate.start(self.store,'F-LOOP','submission')
        with self.assertRaises(ValueError):
            await submit(self.store,self.client,self.root,self.config,'F-LOOP')
        self.assertEqual((self.client.posts,self.client.push_count),([],0))

    async def test_log_tamper_invalidates_issue_review(self):
        await self.approved_issue()
        baseline = self.store.list('test_run')[0]
        Path(baseline['log_path']).write_text('different output')
        with self.assertRaises(ValueError):
            await submit(self.store,self.client,self.root,self.config,'F-LOOP')
        self.assertEqual(self.client.posts,[])

    async def test_approved_issue_can_be_edited_but_old_digest_is_invalid(self):
        first = await self.approved_issue()
        edited = prepare_draft(self.store, self.root, "F-LOOP", submission_type="ISSUE",
                               title="Clarify the documented arithmetic mismatch",
                               body="Reproduction: double(2) produces 3 instead of 4. This revision adds precise environment "
                                    "information and asks whether the documented contract is intended.",
                               risk="Discussion only; no code change")
        self.assertNotEqual(first["digest"], edited["digest"])
        self.assertEqual(self.store.require("finding", "F-LOOP")["status"], "READY_FOR_REVIEW")
        with self.assertRaises(ValueError):
            approve_submission(self.store, "F-LOOP", reviewed_digest=first["digest"], actor=USER, reason="Stale approval")
        with self.assertRaises(ValueError):
            await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(self.client.posts, [])
        self.fixture.review('F-LOOP','submission')
        self.fixture.approve(approve_submission,self.store, "F-LOOP", reviewed_digest=edited["digest"], actor=USER, reason="Reviewed amended issue content")
        submitted = await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(submitted["title"], edited["draft"]["title"])
        self.assertEqual(len(self.client.posts), 1)

    async def test_definitive_rejection_needs_fresh_approval_before_retry(self):
        draft = await self.approved_issue()
        self.client.reject_post_status = 422
        with self.assertRaises(GitHubError):
            await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        operation_id = "submit:F-LOOP:" + draft["digest"]
        self.assertEqual(self.store.operation(operation_id)["status"], "FAILED")
        self.assertIsNone(self.client.remote_issue)
        self.assertEqual(len(self.client.posts), 1)
        self.client.reject_post_status = None
        with self.assertRaises(ValueError):
            await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(len(self.client.posts), 1)
        self.fixture.approve(approve_submission,self.store, "F-LOOP", reviewed_digest=draft["digest"], actor=USER,
                           reason="Server rejection resolved; explicitly approve retry of the same reviewed content")
        result = await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(result["number"], 8)
        self.assertEqual(self.store.operation(operation_id)["status"], "SUCCEEDED")
        self.assertEqual(len(self.client.posts), 2)

    async def test_new_duplicate_evidence_blocks_a_previously_approved_submission(self):
        await self.approved_issue()
        self.client.search_items = [{"number": 90, "title": "Same arithmetic bug already reported", "state": "open",
                                     "url": f"https://github.com/{UPSTREAM}/issues/90"}]
        with self.assertRaisesRegex(ValueError, "Duplicate search"):
            await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(self.client.posts, [])

    async def test_new_review_invalidates_an_approved_revision_plan(self):
        sub = await self.submitted_pr()
        self.client.reviews = [{"id": 601, "user": {"login": "engine-maintainer"}, "author_association": "OWNER",
                                "state": "CHANGES_REQUESTED", "body": "Check the zero case.",
                                "commit_id": self.client.pushed_sha, "submitted_at": now(),
                                "html_url": f"https://github.com/{UPSTREAM}/pull/7#pullrequestreview-601"}]
        await sync_submission(self.store, self.client, sub["submission_id"])
        self.fixture.approve(approve_finding,self.store, "F-LOOP", actor=USER, plan="Address the approved zero-case review", scope=["maths.py"])
        self.client.reviews.append({**self.client.reviews[0], "id": 602, "submitted_at": now(),
                                    "body": "A new request: discuss negative quantities before editing.",
                                    "html_url": f"https://github.com/{UPSTREAM}/pull/7#pullrequestreview-602"})
        await sync_submission(self.store, self.client, sub["submission_id"])
        with self.assertRaisesRegex(ValueError, "review or remote head changed"):
            begin_implementation(self.store, self.root, "F-LOOP")
        self.assertEqual(self.client.push_count, 1)

    async def test_recent_failing_test_cannot_be_hidden_by_an_older_pass(self):
        sub = await self.submitted_pr()
        self.client.reviews = [{"id": 601, "user": {"login": "engine-maintainer"}, "author_association": "OWNER",
                                "state": "CHANGES_REQUESTED", "body": "Check stability of the regression suite.",
                                "commit_id": self.client.pushed_sha, "submitted_at": now(),
                                "html_url": f"https://github.com/{UPSTREAM}/pull/7#pullrequestreview-601"}]
        await sync_submission(self.store, self.client, sub["submission_id"])
        self.fixture.approve(approve_finding,self.store, "F-LOOP", actor=USER, plan="Investigate the reported test instability", scope=["maths.py"])
        begin_implementation(self.store, self.root, "F-LOOP")
        finding = mark_testing(self.store, "F-LOOP")
        external_state = self.root / "fixture-state.txt"
        command = [sys.executable, "-c", "from pathlib import Path; assert Path(" + repr(str(external_state)) + ").read_text() == 'pass'"]
        external_state.write_text("pass")
        passed = run_command(self.store, self.root, finding, command, purpose="suite", reviewed=True,
                             runner="host", allow_host=True, timeout=10)
        external_state.write_text("fail")
        failed = run_command(self.store, self.root, finding, command, purpose="suite", reviewed=True,
                             runner="host", allow_host=True, timeout=10)
        self.assertEqual(passed["status"], "PASS")
        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(passed["tree_hash"], failed["tree_hash"])
        with self.assertRaisesRegex(ValueError, "older PASS"):
            prepare_draft(self.store, self.root, "F-LOOP", submission_type="PR_UPDATE", fork=FORK,
                          title="Revise arithmetic contribution following review",
                          body="The current tests need to pass before this revision can be submitted.",
                          risk="Investigating an observed unstable test")

    async def test_lost_write_response_is_reconciled_without_duplicate_issue(self):
        await self.approved_issue()
        self.client.lose_post_response = True
        with self.assertRaises(TimeoutError):
            await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        with self.assertRaises(ValueError):
            await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(len(self.client.posts), 1)
        value = await reconcile_submission(self.store, self.client, "F-LOOP", username=USER)
        self.assertEqual(value["number"], 8)
        again = await submit(self.store, self.client, self.root, self.config, "F-LOOP")
        self.assertEqual(again["submission_id"], value["submission_id"])
        self.assertEqual(len(self.client.posts), 1)

    async def test_profile_readme_changed_remotely_blocks_overwrite(self):
        await self.accepted_issue()
        profile = await prepare_profile(self.store, self.client, self.root, self.config)
        self.fixture.review(PROFILE,'profile')
        self.fixture.approve(approve_profile,self.store, self.config, reviewed_digest=profile["digest"], actor=USER, reason="Reviewed Profile diff")
        self.client.readme += "\nNew manual project description.\n"
        self.client.readme_sha = "f" * 40
        with self.assertRaises(ValueError):
            await publish_profile(self.store, self.client, self.config)
        self.assertEqual(self.client.puts, [])
        self.assertIn("New manual project description.", self.client.readme)

    async def test_pr_review_requires_revision_approval_then_merger_proof(self):
        sub = await self.submitted_pr()
        self.client.reviews = [{"id": 601, "user": {"login": "engine-maintainer"}, "author_association": "OWNER",
                                "state": "CHANGES_REQUESTED", "body": "Please check the zero case as well.",
                                "commit_id": self.client.pushed_sha, "submitted_at": now(),
                                "html_url": f"https://github.com/{UPSTREAM}/pull/7#pullrequestreview-601"}]
        changed = await sync_submission(self.store, self.client, sub["submission_id"])
        self.assertEqual(changed["status"], "CHANGES_REQUESTED")
        self.assertEqual(sync_ledger(self.store), [])
        with self.assertRaises(ValueError):
            begin_implementation(self.store, self.root, "F-LOOP")
        self.fixture.approve(approve_finding,self.store, "F-LOOP", actor=USER, plan="Check zero behavior and keep revision within the arithmetic module", scope=["maths.py"])
        revision = begin_implementation(self.store, self.root, "F-LOOP")
        self.assertTrue(revision["revision"])
        self.assertEqual(head(Path(revision["workspace"])), self.client.pushed_sha)
        self.assertEqual(revision["implementation_payload"]["review_digest"], changed["review_digest"])
        self.client.remote_pull.update(state="closed", merged=True, merged_at=now(),
                                       merge_commit_sha="e" * 40, merged_by={"login": "engine-maintainer"})
        await sync_submission(self.store, self.client, sub["submission_id"])
        entries = sync_ledger(self.store)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "MERGED")
        self.assertEqual(self.store.require("finding", "F-LOOP")["status"], "MERGED")


if __name__ == "__main__":
    unittest.main()
