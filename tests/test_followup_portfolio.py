from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from repo_scout.database import Store
from repo_scout.followup import (followup_report, record_issue_acceptance,
                                 sync_all, sync_submission)
from repo_scout.portfolio import (END_MARKER, START_MARKER, export_portfolio,
                                  portfolio_metrics, render_profile, sync_ledger)

USER = "yutianjiang39"
SLUG = "maintainer/stream-client"
SHA = "a" * 40
MERGE = "b" * 40


def repository() -> dict:
    return {"id": 42, "full_name": SLUG, "owner": {"login": "maintainer"},
            "fork": False, "private": False, "html_url": f"https://github.com/{SLUG}"}


def pull() -> dict:
    return {"id": 101, "number": 7, "title": "Fix reconnect state", "state": "open", "draft": False,
            "html_url": f"https://github.com/{SLUG}/pull/7", "user": {"login": USER},
            "base": {"repo": repository()}, "head": {"sha": SHA}, "merged": False,
            "merged_at": None, "merge_commit_sha": None, "merged_by": None,
            "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-02T12:00:00Z"}


def issue() -> dict:
    return {"id": 102, "number": 8, "title": "Reconnect loses sequence checkpoint", "state": "open",
            "state_reason": None, "html_url": f"https://github.com/{SLUG}/issues/8",
            "repository_url": f"https://api.github.com/repos/{SLUG}", "user": {"login": USER},
            "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-02T12:00:00Z"}


def review(state="CHANGES_REQUESTED", *, id=11, sha=SHA, association="OWNER", day=2) -> dict:
    return {"id": id, "state": state, "user": {"login": "maintainer"},
            "author_association": association, "body": "Please add a regression test.",
            "submitted_at": f"2026-11-{day:02d}T12:00:00Z", "commit_id": sha,
            "html_url": f"https://github.com/{SLUG}/pull/7#pullrequestreview-{id}"}


def comment(association="OWNER", *, id=99, body="Confirmed: this is a bug and we will fix it.") -> dict:
    return {"id": id, "user": {"login": "maintainer"}, "author_association": association,
            "body": body, "html_url": f"https://github.com/{SLUG}/issues/8#issuecomment-{id}",
            "created_at": "2026-11-03T10:00:00Z", "updated_at": "2026-11-03T10:00:00Z"}


class MockGitHub:
    def __init__(self):
        self.repo = repository()
        self.pr = {"pull": pull(), "reviews": [], "comments": [], "review_comments": [],
                   "checks": [], "statuses": [], "complete": True}
        self.issue = {"issue": issue(), "comments": [], "timeline": [], "complete": True}
        self.fail = False

    async def repository(self, slug, fresh=False):
        if self.fail:
            raise RuntimeError("do-not-persist-this-sensitive-message")
        if not fresh:
            raise AssertionError("Follow-up must refresh upstream evidence")
        return copy.deepcopy(self.repo)

    async def pull_snapshot(self, slug, number):
        return copy.deepcopy(self.pr)

    async def issue_snapshot(self, slug, number):
        return copy.deepcopy(self.issue)


class FollowupPortfolioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "contribution.db")
        self.client = MockGitHub()
        self.store.put("finding", "F-1", {"finding_id": "F-1", "repository": SLUG,
                       "title": "Reconnect loses sequence", "category": "reliability", "status": "SUBMITTED"})
        self.add_submission("S-PR", "PR")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_submission(self, sid, kind, **extra):
        number = 7 if kind == "PR" else 8
        value = {"submission_id": sid, "finding_id": "F-1", "repository": SLUG,
                 "submission_type": kind, "number": number, "status": "OPEN", "outcome": "PENDING",
                 "submission_url": f"https://github.com/{SLUG}/{'pull' if kind == 'PR' else 'issues'}/{number}",
                 "title": "Reconnect fix", "provenance": "live", "created_at": "2026-11-01T12:00:00Z"}
        value.update(extra)
        self.store.put("submission", sid, value)

    def merge(self):
        self.client.pr["pull"].update(merged=True, state="closed", merged_at="2026-11-04T12:00:00Z",
                                      merge_commit_sha=MERGE, merged_by={"login": "maintainer"})

    async def sync_pr(self):
        return await sync_submission(self.store, self.client, "S-PR")

    async def accept_issue(self):
        self.add_submission("S-ISSUE", "ISSUE")
        self.client.issue["comments"] = [comment()]
        await sync_submission(self.store, self.client, "S-ISSUE")
        return record_issue_acceptance(self.store, "S-ISSUE", evidence_url=comment()["html_url"],
                                       actor=USER, rationale="Maintainer explicitly confirms the bug and accepts a fix.")

    async def test_full_review_revision_merge_cycle(self):
        self.client.pr["reviews"] = [review()]
        changed = await self.sync_pr()
        self.assertEqual(changed["status"], "CHANGES_REQUESTED")
        self.assertEqual(self.store.require("finding", "F-1")["status"], "CHANGES_REQUESTED")
        self.assertTrue(changed["required_actions"][0]["requires_approval"])
        self.assertTrue(changed["review_digest"])
        self.assertEqual(sync_ledger(self.store), [])

        finding = self.store.require("finding", "F-1")
        finding["status"] = "IMPLEMENTING"
        self.store.put("finding", "F-1", finding)
        await self.sync_pr()
        self.assertEqual(self.store.require("finding", "F-1")["status"], "IMPLEMENTING")

        self.client.pr["reviews"].append(review("APPROVED", id=12, day=3))
        approved = await self.sync_pr()
        self.assertEqual(approved["status"], "APPROVED")
        self.assertEqual(sync_ledger(self.store), [])
        self.merge()
        merged = await self.sync_pr()
        self.assertEqual(merged["status"], "MERGED")
        self.assertEqual(self.store.require("finding", "F-1")["status"], "MERGED")
        entries = sync_ledger(self.store)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["accepted_evidence"]["merge_commit_sha"], MERGE)
        self.assertGreater(len(self.store.events("submission", "S-PR")), 3)

    async def test_old_head_approval_and_dismissed_review_are_not_current(self):
        self.client.pr["reviews"] = [review("APPROVED", sha="c" * 40)]
        value = await self.sync_pr()
        self.assertEqual(value["status"], "UNDER_REVIEW")
        self.assertEqual(value["stale_approvals"], [11])
        self.client.pr["reviews"] = [review("DISMISSED")]
        self.assertNotEqual((await self.sync_pr())["status"], "APPROVED")

    async def test_comment_review_does_not_erase_decisive_review(self):
        self.client.pr["reviews"] = [review("APPROVED"), review("COMMENTED", id=12, day=3)]
        self.assertEqual((await self.sync_pr())["status"], "APPROVED")

    async def test_change_request_persists_across_head_change(self):
        self.client.pr["reviews"] = [review()]
        self.client.pr["pull"]["head"]["sha"] = "d" * 40
        self.assertEqual((await self.sync_pr())["status"], "CHANGES_REQUESTED")

    async def test_closed_unmerged_and_test_merge_sha_not_counted(self):
        self.client.pr["pull"].update(state="closed", merge_commit_sha=MERGE, merged=False)
        value = await self.sync_pr()
        self.assertEqual(value["status"], "CLOSED_UNMERGED")
        self.assertEqual(sync_ledger(self.store), [])

    async def test_merged_pr_requires_foreign_nonfork_upstream(self):
        self.merge()
        for mutation in (lambda r: r.update(fork=True),
                         lambda r: r.update(owner={"login": USER}),
                         lambda r: r.update(private=True)):
            with self.subTest(mutation=mutation):
                self.client.repo = repository()
                mutation(self.client.repo)
                await self.sync_pr()
                self.assertEqual(sync_ledger(self.store), [])

    async def test_base_repository_mismatch_does_not_count(self):
        self.merge()
        self.client.pr["pull"]["base"]["repo"]["id"] = 900
        value = await self.sync_pr()
        self.assertFalse(value["verification"]["eligible"])
        self.assertEqual(sync_ledger(self.store), [])

    async def test_wrong_author_and_self_merge_do_not_count(self):
        self.merge()
        self.client.pr["pull"]["user"] = {"login": "somebody-else"}
        await self.sync_pr()
        self.assertEqual(sync_ledger(self.store), [])
        self.client.pr["pull"]["user"] = {"login": USER}
        self.client.pr["pull"]["merged_by"] = {"login": USER}
        await self.sync_pr()
        self.assertEqual(sync_ledger(self.store), [])

    async def test_response_identity_mismatch_preserves_current_state(self):
        self.merge()
        self.client.pr["pull"]["number"] = 999
        with self.assertRaises(ValueError):
            await self.sync_pr()
        self.assertEqual(self.store.require("submission", "S-PR")["status"], "OPEN")

    async def test_demo_and_imported_records_never_become_official(self):
        self.merge()
        for provenance in ("demo", "imported"):
            self.add_submission("S-PR", "PR", provenance=provenance)
            await self.sync_pr()
            self.assertEqual(sync_ledger(self.store), [])

    async def test_closed_issue_even_completed_is_not_acceptance(self):
        self.add_submission("S-ISSUE", "ISSUE")
        self.client.issue["issue"].update(state="closed", state_reason="completed")
        self.client.issue["comments"] = [comment(body="Thanks!")]
        value = await sync_submission(self.store, self.client, "S-ISSUE")
        self.assertEqual(value["status"], "CLOSED")
        self.assertEqual(value["outcome"], "UNDETERMINED")
        self.assertEqual(sync_ledger(self.store), [])

    async def test_explicit_acceptance_requires_real_fetched_maintainer_comment(self):
        await self.accept_issue()
        entries = sync_ledger(self.store)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "ACCEPTED")
        self.assertEqual(entries[0]["accepted_evidence"]["url"], comment()["html_url"])
        with self.assertRaises(ValueError):
            record_issue_acceptance(self.store, "S-ISSUE", evidence_url="https://github.com/fake/repo/issues/1#issuecomment-99",
                                    actor=USER, rationale="A copied unrelated link")

    async def test_unknown_collaborator_identity_does_not_prove_acceptance(self):
        self.add_submission("S-ISSUE", "ISSUE")
        self.client.issue["comments"] = [comment("COLLABORATOR")]
        await sync_submission(self.store, self.client, "S-ISSUE")
        with self.assertRaises(ValueError):
            record_issue_acceptance(self.store, "S-ISSUE", evidence_url=comment()["html_url"], actor=USER,
                                    rationale="Claim acceptance without verified maintainer permission")
        await sync_submission(self.store, self.client, "S-ISSUE", trusted_maintainers={SLUG: ["maintainer"]})
        record_issue_acceptance(self.store, "S-ISSUE", evidence_url=comment()["html_url"], actor=USER,
                                rationale="Trusted maintainer explicitly accepted the confirmed bug")
        self.assertEqual(len(sync_ledger(self.store)), 1)

    async def test_org_membership_alone_is_not_maintainer_authority(self):
        self.add_submission("S-ISSUE", "ISSUE")
        self.client.issue["comments"] = [comment("MEMBER")]
        await sync_submission(self.store, self.client, "S-ISSUE")
        with self.assertRaises(ValueError):
            record_issue_acceptance(self.store, "S-ISSUE", evidence_url=comment()["html_url"], actor=USER,
                                    rationale="Organization membership is insufficient evidence")
        self.assertFalse(self.store.list("feedback")[0]["authority_verified"])

    async def test_optional_ci_unavailable_does_not_suppress_verified_merge(self):
        self.merge()
        self.client.pr.update(checks={"available": False, "check_runs": []},
                              statuses={"available": False, "statuses": []},
                              errors=["checks unavailable (HTTP 403)", "statuses unavailable (HTTP 404)"])
        value = await self.sync_pr()
        self.assertEqual(value["ci"]["state"], "UNKNOWN")
        self.assertTrue(value["verification"]["eligible"])
        self.assertEqual(len(sync_ledger(self.store)), 1)

    async def test_incomplete_critical_snapshot_cannot_prove_acceptance(self):
        self.merge()
        self.client.pr["complete"] = False
        await self.sync_pr()
        self.assertEqual(sync_ledger(self.store), [])

    async def test_edited_acceptance_evidence_invalidates_ledger(self):
        await self.accept_issue()
        cid = sync_ledger(self.store)[0]["contribution_id"]
        self.client.issue["comments"][0]["body"] = "On second thought this is expected behavior."
        await sync_submission(self.store, self.client, "S-ISSUE")
        self.assertEqual(sync_ledger(self.store), [])
        self.assertFalse(self.store.require("contribution", cid)["official"])

    async def test_deleted_acceptance_evidence_invalidates_ledger(self):
        await self.accept_issue()
        self.client.issue["comments"] = []
        await sync_submission(self.store, self.client, "S-ISSUE")
        self.assertEqual(sync_ledger(self.store), [])
        self.assertFalse(self.store.list("feedback")[0]["active"])

    async def test_rejected_issue_cannot_be_promoted_to_accepted(self):
        await self.accept_issue()
        self.client.issue["issue"].update(state="closed", state_reason="not_planned")
        value = await sync_submission(self.store, self.client, "S-ISSUE")
        self.assertEqual(value["status"], "REJECTED")
        with self.assertRaises(ValueError):
            record_issue_acceptance(self.store, "S-ISSUE", evidence_url=comment()["html_url"],
                                    actor=USER, rationale="Old positive comment should not override rejection")
        self.assertEqual(sync_ledger(self.store), [])

    async def test_issue_then_merged_pr_is_one_contribution(self):
        await self.accept_issue()
        old_id = sync_ledger(self.store)[0]["contribution_id"]
        self.merge()
        await self.sync_pr()
        entries = sync_ledger(self.store)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["submission_type"], "PR")
        self.assertIn(issue()["html_url"], entries[0]["supporting_submissions"])
        self.assertFalse(self.store.require("contribution", old_id)["official"])

    async def test_ledger_and_snapshot_are_idempotent(self):
        self.merge()
        self.client.pr["checked_at"] = "2026-11-05T12:00:00Z"
        await self.sync_pr()
        first = sync_ledger(self.store)
        events = len(self.store.events("contribution"))
        self.client.pr["checked_at"] = "2026-11-05T13:00:00Z"
        await self.sync_pr()
        second = sync_ledger(self.store)
        self.assertEqual(first, second)
        self.assertEqual(events, len(self.store.events("contribution")))
        self.assertEqual(len(self.store.list("snapshot")), 1)

    async def test_metrics_count_evidence_and_distinct_projects(self):
        self.client.pr["reviews"] = [review("APPROVED")]
        await self.sync_pr()
        value = portfolio_metrics(self.store)
        self.assertEqual(value["verified_contributions"], 0)
        self.assertEqual(value["approved_not_merged"], 1)
        self.merge()
        await self.sync_pr()
        value = portfolio_metrics(self.store)
        self.assertEqual(value["upstream_projects"], 1)
        self.assertEqual(value["merged_prs"], 1)

    async def test_profile_preserves_personal_content_and_generates_diff(self):
        self.merge()
        await self.sync_pr()
        prefix, suffix = "# About me\r\n\r\nI build systems.\r\n", "\r\n\r\n## Personal Projects\r\nKeep this.\r\n"
        existing = prefix + START_MARKER + "\r\nOLD\r\n" + END_MARKER + suffix
        draft = render_profile(self.store, existing)
        self.assertTrue(draft["content"].startswith(prefix))
        self.assertTrue(draft["content"].endswith(suffix))
        self.assertNotIn("OLD", draft["content"])
        self.assertIn("Upstream record", draft["content"])
        self.assertTrue(draft["diff"])
        self.assertFalse(render_profile(self.store, draft["content"])["changed"])

    async def test_profile_rejects_invalid_markers(self):
        for text in (START_MARKER, END_MARKER + START_MARKER, START_MARKER + END_MARKER + START_MARKER + END_MARKER):
            with self.subTest(text=text), self.assertRaises(ValueError):
                render_profile(self.store, text)

    async def test_untrusted_titles_are_escaped_in_profile(self):
        self.merge()
        self.client.pr["pull"]["title"] = "Fix | unsafe [click](javascript:alert(1)) <script>\nextra"
        await self.sync_pr()
        draft = render_profile(self.store, "# Intro\n")
        self.assertNotIn("<script>", draft["content"])
        self.assertIn("\\|", draft["content"])
        self.assertIn("\\[click\\]", draft["content"])

    async def test_snapshot_tamper_or_forged_flag_is_not_evidence(self):
        self.merge()
        await self.sync_pr()
        sub = self.store.require("submission", "S-PR")
        raw = self.store.require("snapshot", sub["last_snapshot_id"])
        raw["payload"]["pull"]["title"] = "Tampered"
        self.store.put("snapshot", sub["last_snapshot_id"], raw)
        self.assertEqual(sync_ledger(self.store), [])

    async def test_ci_failures_and_unavailable_checks_remain_honest(self):
        self.client.pr["checks"] = {"available": False, "check_runs": []}
        self.client.pr["statuses"] = {"statuses": [{"id": 1, "context": "unit", "state": "success"}], "available": True}
        self.assertEqual((await self.sync_pr())["ci"]["state"], "UNKNOWN")
        self.client.pr["checks"] = [{"id": 1, "name": "test", "head_sha": SHA, "status": "completed",
                                     "conclusion": "failure", "html_url": "https://github.com/example/check"}]
        value = await self.sync_pr()
        self.assertEqual(value["ci"]["state"], "FAILURE")
        self.assertEqual(value["required_actions"][0]["action"], "INVESTIGATE_CI_FAILURE")

    async def test_sync_failure_preserves_state_and_does_not_persist_error_secrets(self):
        self.client.fail = True
        results = await sync_all(self.store, self.client)
        self.assertEqual(results[0]["sync_status"], "ERROR")
        self.assertNotIn("do-not-persist", str(results))
        self.assertEqual(self.store.require("submission", "S-PR")["status"], "OPEN")

    async def test_report_and_export_are_local_reviewable_outputs(self):
        self.client.pr["reviews"] = [review()]
        await self.sync_pr()
        report = followup_report(self.store)
        self.assertIn("Reply draft", report)
        self.assertIn(review()["html_url"], report)
        output = export_portfolio(self.store, Path(self.tmp.name) / "portfolio")
        self.assertTrue(Path(output["ledger_path"]).is_file())
        self.assertEqual(output["metrics"]["verified_contributions"], 0)


if __name__ == "__main__":
    unittest.main()
