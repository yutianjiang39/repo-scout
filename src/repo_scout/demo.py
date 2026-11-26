"""Offline, isolated demonstration of the contribution lifecycle.

The Git repository and failing/passing Python tests are real local execution.
Claude Code decisions, user approvals and all GitHub/maintainer responses are
explicitly simulated. No network adapter or public write function is used.
Every database entity is forced to provenance=demo, including core functions
that normally upgrade independently fetched evidence to live provenance.
"""

from __future__ import annotations

import copy
import difflib
import json
from pathlib import Path
import sys

from .common import atomic_write, now, write_json
from .database import Store
from .followup import followup_report, sync_submission
from .implementation import commit_changes, git, head, run_command
from .portfolio import export_portfolio, render_profile, sync_ledger
from .validation import (
    _apply_assessment as accept_validation,
    make_cc_packet,
    preflight,
)
from .workflow import (
    add_finding,
    apply_implementation_patch,
    approve_finding,
    approve_submission,
    begin_implementation,
    mark_testing,
    prepare_draft,
    record_diff_review,
)


DEMO_REPOSITORY = "demo-maintainer/demo-order-ledger"
DEMO_USER = "demo-contributor"
FINDING_ID = "DEMO-FILL-001"
SUBMISSION_ID = "DEMO-SUBMISSION-001"
DEMO_NOTICE = (
    "DEMO — 离线合成示例，不是真实上游贡献。代码与测试在本机实际执行；"
    "CC 结论、人工审批、GitHub Review 与合并均为模拟。"
)

BUGGY_SOURCE = '''"""DEMO ONLY: aggregate execution messages for one order."""


def filled_quantity(fills):
    """Return filled quantity; repeated trade IDs are retransmitted events."""
    return sum(fill["quantity"] for fill in fills)
'''

FIXED_SOURCE = '''"""DEMO ONLY: aggregate execution messages for one order."""


def filled_quantity(fills):
    """Return filled quantity; repeated trade IDs are retransmitted events."""
    seen = set()
    quantity = 0
    for fill in fills:
        if fill["trade_id"] in seen:
            continue
        seen.add(fill["trade_id"])
        quantity += fill["quantity"]
    return quantity
'''

BASELINE_TEST = '''"""DEMO ONLY: a real regression test, requiring only Python stdlib."""
import unittest
from fills import filled_quantity


class FillTests(unittest.TestCase):
    def test_retransmitted_fill_counted_once(self):
        fills = [{"trade_id": "trade-a", "quantity": 2},
                 {"trade_id": "trade-a", "quantity": 2}]
        self.assertEqual(filled_quantity(fills), 2)


if __name__ == "__main__":
    unittest.main()
'''

REVIEWED_TEST = '''"""DEMO ONLY: real regression tests including simulated review feedback."""
import unittest
from fills import filled_quantity


class FillTests(unittest.TestCase):
    def test_retransmitted_fill_counted_once(self):
        fills = [{"trade_id": "trade-a", "quantity": 2},
                 {"trade_id": "trade-a", "quantity": 2}]
        self.assertEqual(filled_quantity(fills), 2)

    def test_distinct_trades_with_equal_quantity_are_preserved(self):
        fills = [{"trade_id": "trade-a", "quantity": 2},
                 {"trade_id": "trade-b", "quantity": 2}]
        self.assertEqual(filled_quantity(fills), 4)

    def test_nonadjacent_duplicate_is_ignored(self):
        fills = [{"trade_id": "trade-a", "quantity": 2},
                 {"trade_id": "trade-b", "quantity": 3},
                 {"trade_id": "trade-a", "quantity": 2}]
        self.assertEqual(filled_quantity(fills), 5)


if __name__ == "__main__":
    unittest.main()
'''


class _DemoStore(Store):
    """A provenance firewall scoped to a newly created demonstration database."""

    def put(
        self,
        kind: str,
        key: str,
        data: dict,
        *,
        reason: str = "",
        actor: str = "local",
        evidence=None,
        expected_version=None,
    ) -> dict:
        data = {**data, "provenance": "demo", "demo": True}
        if "preflight" in data:
            data["preflight"] = {
                **data["preflight"],
                "simulated": True,
                "notice": "DEMO in-memory GitHub fixture; no live API request",
            }
        return super().put(
            kind,
            key,
            data,
            reason="[DEMO] " + reason,
            actor="DEMO:" + actor,
            evidence=evidence,
            expected_version=expected_version,
        )


class _OfflineGitHub:
    """Only returns in-memory fixtures. Deliberately has no post/put method."""

    def __init__(self, upstream_sha: str):
        self.upstream_sha = upstream_sha
        self.read_calls = 0
        self.repo = {
            "id": 900001,
            "full_name": DEMO_REPOSITORY,
            "owner": {"login": "demo-maintainer"},
            "fork": False,
            "private": False,
            "archived": False,
            "default_branch": "main",
            "html_url": "https://github.com/" + DEMO_REPOSITORY,
            "demo": True,
        }
        self.snapshot: dict = {}

    async def repository(self, repository: str, fresh: bool = False) -> dict:
        if repository != DEMO_REPOSITORY or not fresh:
            raise ValueError(
                "Offline demo only supplies its own freshly selected fixture"
            )
        self.read_calls += 1
        return copy.deepcopy(self.repo)

    async def get(self, path: str, params=None, fresh: bool = False) -> dict:
        if path != f"/repos/{DEMO_REPOSITORY}/commits/main" or not fresh:
            raise ValueError("Unsupported offline demo API fixture")
        self.read_calls += 1
        return {"sha": self.upstream_sha, "demo": True}

    async def duplicates(self, repository: str, title: str) -> dict:
        if repository != DEMO_REPOSITORY:
            raise ValueError("Unsupported offline demo duplicate fixture")
        self.read_calls += 1
        return {
            "issues": [],
            "prs": [],
            "checked_at": now(),
            "complete": True,
            "query": "DEMO simulated duplicate check for " + title,
            "source": "demo_fixture",
            "simulated": True,
            "conclusion": "No live GitHub search was performed",
        }

    async def pull_snapshot(self, repository: str, number: int) -> dict:
        if repository != DEMO_REPOSITORY or number != 1:
            raise ValueError("Unsupported offline demo PR fixture")
        self.read_calls += 1
        return copy.deepcopy(self.snapshot)

    def open_pull(self, source_sha: str) -> None:
        stamp = now()
        self.snapshot = {
            "pull": {
                "id": 900002,
                "number": 1,
                "title": "[DEMO] Deduplicate retransmitted fills",
                "state": "open",
                "draft": False,
                "html_url": f"https://github.com/{DEMO_REPOSITORY}/pull/1",
                "user": {"login": DEMO_USER},
                "base": {"repo": self.repo},
                "head": {"sha": source_sha},
                "merged": False,
                "merged_at": None,
                "merge_commit_sha": None,
                "merged_by": None,
                "requested_reviewers": [{"login": "demo-maintainer"}],
                "created_at": stamp,
                "updated_at": stamp,
                "demo": True,
            },
            "reviews": [],
            "comments": [],
            "review_comments": [],
            "checks": [],
            "statuses": [],
            "complete": True,
            "demo": True,
        }

    def review(self, state: str, source_sha: str, body: str, review_id: int) -> None:
        self.snapshot["reviews"].append(
            {
                "id": review_id,
                "state": state,
                "commit_id": source_sha,
                "user": {"login": "demo-maintainer"},
                "author_association": "OWNER",
                "submitted_at": now(),
                "body": "[DEMO simulated maintainer] " + body,
                "html_url": f"https://github.com/{DEMO_REPOSITORY}/pull/1#pullrequestreview-{review_id}",
                "demo": True,
            }
        )


def _patch(path: str, old: str, new: str, *, new_file: bool = False) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="/dev/null" if new_file else "a/" + path,
            tofile="b/" + path,
        )
    )


def _notice_reports(root: Path) -> None:
    # Core reports normally describe live work. Make each demo report standalone
    # and unambiguously labeled even when opened without the main demo README.
    for path in sorted((root / "reports").rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix == ".md":
            text = path.read_text("utf-8")
            if not text.startswith("# DEMO"):
                atomic_write(
                    path, "# DEMO — 离线演练\n\n" + DEMO_NOTICE + "\n\n---\n\n" + text
                )
        elif path.suffix == ".json":
            value = json.loads(path.read_text("utf-8"))
            if isinstance(value, dict):
                write_json(
                    path,
                    {
                        **value,
                        "provenance": "demo",
                        "demo": True,
                        "demo_notice": DEMO_NOTICE,
                    },
                )


async def run_demo(root: Path) -> dict:
    """Exercise the real local workflow in a new empty directory, without APIs."""
    root = Path(root).absolute()
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValueError("Demo output must not use symlink paths")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError(
            "Demo requires a new or empty dedicated directory; existing work is never overwritten"
        )
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(
        root / "DEMO_ONLY.txt",
        DEMO_NOTICE + "\nNo network or model calls. No real maintainer review.\n",
    )
    original = root / "repositories" / "demo-order-ledger" / "original"
    original.mkdir(parents=True)
    atomic_write(original / "fills.py", BUGGY_SOURCE)
    atomic_write(original / "README.md", "# DEMO order ledger\n\n" + DEMO_NOTICE + "\n")
    atomic_write(
        original / "CONTRIBUTING.md",
        "# DEMO contribution policy\n\nKeep fixes small and add regression tests.\n",
    )
    atomic_write(original / ".gitignore", "__pycache__/\n")
    git(original, "init", "--initial-branch=main", "--quiet")
    upstream_sha = commit_changes(
        original,
        message="DEMO baseline: synthetic retransmitted-fill bug",
        name="Demo Fixture",
        email="demo@example.invalid",
    )
    client = _OfflineGitHub(upstream_sha)
    stage_records: list[dict] = []
    review_loop: list[dict] = []
    with _DemoStore(root / "data" / "contribution.db") as store:
        from .demo_review import FixtureCollector

        fixture = FixtureCollector(store)

        def stage(name: str, **details) -> None:
            stage_records.append({"stage": name, "provenance": "demo", **details})

        finding = add_finding(
            store,
            {
                "finding_id": FINDING_ID,
                "repository": DEMO_REPOSITORY,
                "title": "[DEMO] Replayed fills inflate filled quantity",
                "category": "correctness",
                "severity": "medium",
                "confidence": "MEDIUM",
                "file_path": "fills.py",
                "line_start": 4,
                "line_end": 6,
                "base_sha": upstream_sha,
                "trigger_condition": "DEMO: the same execution message is received twice",
                "expected_behavior": "Count each trade_id once for this order",
                "actual_behavior": "Both copies are summed",
                "impact": "Filled quantity is overstated",
                "proposed_fix": "Deduplicate by stable trade ID while preserving distinct trades",
                "test_plan": [
                    "Replay the same trade ID and assert its quantity is counted once"
                ],
            },
        )
        stage(
            "CANDIDATE",
            finding_id=FINDING_ID,
            note="Synthetic candidate; no model was called",
        )
        finding = await preflight(
            store, client, root, FINDING_ID, original_path=original
        )
        stage(
            "CC_VALIDATING",
            note="In-memory upstream metadata and duplicate fixture; no GitHub request",
        )
        validation_workspace = Path(finding["validation_workspace"])
        atomic_write(validation_workspace / "test_regression.py", BASELINE_TEST)
        command = [sys.executable, "-m", "unittest", "-v", "test_regression"]
        baseline = run_command(
            store,
            root,
            finding,
            command,
            purpose="baseline",
            reviewed=True,
            runner="host",
            allow_host=True,
            timeout=30,
        )
        if baseline["status"] != "FAIL" or not baseline["source_unchanged"]:
            raise RuntimeError(
                "Demo baseline did not produce the expected stable failure"
            )
        failure_signature = "AssertionError: 4 != 2"
        if failure_signature not in Path(baseline["log_path"]).read_text():
            raise RuntimeError(
                "Demo baseline failed for an unexpected reason; refusing simulated confirmation"
            )
        stage(
            "BASELINE_FAIL",
            run_id=baseline["run_id"],
            actual_execution=True,
            return_code=baseline["return_code"],
            failure_signature=failure_signature,
        )
        make_cc_packet(store, root, FINDING_ID)
        validation_report = {
            "finding_id": FINDING_ID,
            "validator": "claude-code",
            "session_id": "DEMO-SIMULATED-CC-NO-MODEL-CALL",
            "source_sha": upstream_sha,
            "result": "CONFIRMED",
            "root_cause": "DEMO simulated CC decision: sum() counts repeated trade IDs twice.",
            "evidence": [
                "Actual local baseline failure: " + failure_signature,
                "DEMO synthetic source fills.py:6; no real CC session was invoked.",
            ],
            "trigger_condition": "Same trade_id appears twice",
            "actual_behavior": "4 is returned for a replayed fill of size 2",
            "expected_behavior": "2 should be returned",
            "protection_analysis": "DEMO function has no deduplication guard",
            "design_compatibility": "DEMO docstring specifies repeated trade IDs are retransmitted events",
            "duplicate_assessment": "DEMO: in-memory empty fixture only; no live duplicate search",
            "already_fixed_assessment": "DEMO: original local commit still uses sum()",
            "recommended_action": "PR",
            "impact": "DEMO filled quantity is overstated",
            "regression_risk": "Distinct trade IDs with equal quantities must still both count",
            "baseline_run_id": baseline["run_id"],
            "failure_signature": failure_signature,
            "reproducible": True,
        }
        finding = accept_validation(
            store,
            root,
            FINDING_ID,
            validation_report,
            attested_cc=True,
            provenance="demo-simulated-cc-not-executed",
        )
        stage("CONFIRMED", simulated_cc=True, baseline_run_id=baseline["run_id"])
        fixture.review(FINDING_ID, "finding")
        fixture.approve(
            approve_finding,
            store,
            FINDING_ID,
            actor="DEMO-SIMULATED-OPERATOR",
            plan="[DEMO] Add trade-ID deduplication and the observed regression test",
            scope=["fills.py", "test_regression.py"],
        )
        stage("HUMAN_APPROVED", simulated_approval=True)
        finding = begin_implementation(store, root, FINDING_ID)
        workspace = Path(finding["workspace"])
        stage(
            "IMPLEMENTING", isolated_worktree=str(workspace), branch=finding["branch"]
        )
        patch = _patch("fills.py", BUGGY_SOURCE, FIXED_SOURCE) + _patch(
            "test_regression.py", "", BASELINE_TEST, new_file=True
        )
        apply_implementation_patch(store, root, FINDING_ID, patch)
        finding = mark_testing(store, FINDING_ID)
        regression = run_command(
            store,
            root,
            finding,
            command,
            purpose="regression",
            reviewed=True,
            runner="host",
            allow_host=True,
            timeout=30,
        )
        if regression["status"] != "PASS" or not regression["source_unchanged"]:
            raise RuntimeError("Demo fix did not pass the actual regression test")
        stage(
            "REGRESSION_PASS",
            run_id=regression["run_id"],
            actual_execution=True,
            return_code=regression["return_code"],
        )
        first_head = commit_changes(
            workspace,
            message="DEMO fix: deduplicate retransmitted fills",
            name="Demo Contributor",
            email="demo@example.invalid",
        )
        record_diff_review(
            store,
            FINDING_ID,
            actor="DEMO-SIMULATED-REVIEWER",
            approved=True,
            assessment="[DEMO] Exact patch adds only deduplication and the observed regression test.",
        )
        fixture.review(FINDING_ID, "diff")
        draft = prepare_draft(
            store,
            root,
            FINDING_ID,
            submission_type="PR",
            title="[DEMO] Deduplicate retransmitted fills",
            body="DEMO ONLY: synthetic local fixture. Real regression failed before the fix and passed after it. "
            "CC decisions, approval and upstream publication are simulated. No real PR will be created.",
            risk="DEMO: preserve distinct executions even when quantities match",
            fork=DEMO_USER + "/demo-order-ledger",
        )
        fixture.review(FINDING_ID, "submission")
        fixture.approve(
            approve_submission,
            store,
            FINDING_ID,
            reviewed_digest=draft["digest"],
            actor="DEMO-SIMULATED-OPERATOR",
            reason="[DEMO] Approve this exact fixture draft; no public write",
        )
        stage(
            "APPROVED_TO_SUBMIT",
            simulated_approval=True,
            digest=draft["digest"],
            public_write=False,
        )

        # Public submission is deliberately represented by an offline fixture.
        # Do not call submit(), push_fork(), a GitHub client, or any model here.
        client.open_pull(first_head)
        submission = {
            "submission_id": SUBMISSION_ID,
            "finding_id": FINDING_ID,
            "repository": DEMO_REPOSITORY,
            "submission_type": "PR",
            "number": 1,
            "submission_url": f"https://github.com/{DEMO_REPOSITORY}/pull/1",
            "title": "[DEMO] Deduplicate retransmitted fills",
            "body": draft["draft"]["body"],
            "status": "OPEN",
            "outcome": "PENDING",
            "head_sha": first_head,
            "branch": finding["branch"],
            "created_at": now(),
            "publication": "NOT_PUBLISHED",
            "changed_files": draft["draft"]["changed_files"],
            "tests": [regression["run_id"]],
        }
        store.put(
            "submission",
            SUBMISSION_ID,
            submission,
            reason="In-memory PR fixture, not a public submission",
            actor="demo-fixture",
        )
        finding = store.require("finding", FINDING_ID)
        store.put(
            "finding",
            FINDING_ID,
            {**finding, "status": "SUBMITTED", "submission_id": SUBMISSION_ID},
            reason="Simulated transition to offline follow-up fixture",
            actor="demo-fixture",
        )

        async def observe(label: str) -> dict:
            synced = await sync_submission(
                store, client, SUBMISSION_ID, username=DEMO_USER
            )
            review_loop.append(
                {
                    "event": label,
                    "status": synced["status"],
                    "head_sha": synced["head_sha"],
                    "provenance": "demo",
                    "official_contributions": len(
                        sync_ledger(store, username=DEMO_USER)
                    ),
                }
            )
            return synced

        await observe("simulated_review_requested")
        client.review(
            "CHANGES_REQUESTED",
            first_head,
            "Please cover distinct trades with equal quantities and nonadjacent duplicate messages.",
            11,
        )
        requested = await observe("simulated_changes_requested")
        if requested["status"] != "CHANGES_REQUESTED":
            raise RuntimeError(
                "Review request did not enter the expected revision loop"
            )
        fixture.approve(
            approve_finding,
            store,
            FINDING_ID,
            actor="DEMO-SIMULATED-OPERATOR",
            plan="[DEMO] Follow review: add equal-quantity and nonadjacent replay coverage; keep the existing fix",
            scope=["fills.py", "test_regression.py"],
        )
        finding = begin_implementation(store, root, FINDING_ID)
        apply_implementation_patch(
            store,
            root,
            FINDING_ID,
            _patch("test_regression.py", BASELINE_TEST, REVIEWED_TEST),
        )
        finding = mark_testing(store, FINDING_ID)
        revision_run = run_command(
            store,
            root,
            finding,
            command,
            purpose="regression",
            reviewed=True,
            runner="host",
            allow_host=True,
            timeout=30,
        )
        if revision_run["status"] != "PASS" or not revision_run["source_unchanged"]:
            raise RuntimeError("Demo review revision failed its actual tests")
        revised_head = commit_changes(
            workspace,
            message="DEMO review: cover distinct and nonadjacent fills",
            name="Demo Contributor",
            email="demo@example.invalid",
        )
        if revised_head == first_head:
            raise RuntimeError("Demo revision must produce a new actual Git commit")
        record_diff_review(
            store,
            FINDING_ID,
            actor="DEMO-SIMULATED-REVIEWER",
            approved=True,
            assessment="[DEMO] Three real regression tests pass; revision adds focused boundary coverage only.",
        )
        fixture.review(FINDING_ID, "diff")
        revised_draft = prepare_draft(
            store,
            root,
            FINDING_ID,
            submission_type="PR_UPDATE",
            title="[DEMO] Deduplicate retransmitted fills — review revision",
            body="DEMO ONLY: applies simulated maintainer feedback using two additional real regression tests. "
            "The local tests pass; no maintainer was contacted and no branch was pushed.",
            risk="DEMO: no behavioral scope change beyond the original deduplication fix",
            fork=DEMO_USER + "/demo-order-ledger",
        )
        fixture.review(FINDING_ID, "submission")
        fixture.approve(
            approve_submission,
            store,
            FINDING_ID,
            reviewed_digest=revised_draft["digest"],
            actor="DEMO-SIMULATED-OPERATOR",
            reason="[DEMO] Approve exact revised fixture; no public update",
        )
        stage(
            "REVIEW_REVISION_TESTED",
            actual_execution=True,
            run_id=revision_run["run_id"],
            first_head=first_head,
            revised_head=revised_head,
            simulated_approval=True,
        )
        client.snapshot["pull"]["head"]["sha"] = revised_head
        client.snapshot["pull"]["updated_at"] = now()
        finding = store.require("finding", FINDING_ID)
        store.put(
            "finding",
            FINDING_ID,
            {**finding, "status": "SUBMITTED"},
            reason="Simulated updated PR head; no push occurred",
            actor="demo-fixture",
        )
        await observe("simulated_new_head_awaits_re_review")
        client.review(
            "APPROVED", revised_head, "The revised tests cover the requested cases.", 12
        )
        await observe("simulated_maintainer_approved")
        client.snapshot["pull"].update(
            merged=True,
            state="closed",
            merged_at=now(),
            closed_at=now(),
            merge_commit_sha=revised_head,
            merged_by={"login": "demo-maintainer"},
        )
        merged = await observe("simulated_upstream_merged")
        stage("SIMULATED_MERGED", status=merged["status"], publication="NOT_PUBLISHED")
        exported = export_portfolio(store, root / "portfolio", username=DEMO_USER)
        profile = render_profile(
            store, "# DEMO contributor\n\n" + DEMO_NOTICE + "\n", username=DEMO_USER
        )
        atomic_write(root / "portfolio" / "profile_readme_draft.md", profile["content"])
        atomic_write(root / "portfolio" / "profile_readme.diff", profile["diff"])
        atomic_write(
            root / "reports" / "followup" / "followup_report.md", followup_report(store)
        )
        records = [
            json.loads(row[0])
            for row in store.connection.execute("SELECT data FROM records")
        ]
        all_demo = all(record.get("provenance") == "demo" for record in records)
        official_count = exported["metrics"]["verified_contributions"]
        if not all_demo or official_count != 0 or profile["entries"]:
            raise RuntimeError(
                "Demo provenance escaped the official-contribution exclusion gate"
            )
        if (
            head(original) != upstream_sha
            or (original / "fills.py").read_text() != BUGGY_SOURCE
        ):
            raise RuntimeError("Demo unexpectedly modified the original repository")
        _notice_reports(root)
        result = {
            "schema_version": 1,
            "provenance": "demo",
            "demo_notice": DEMO_NOTICE,
            "root": str(root),
            "database": str(root / "data" / "contribution.db"),
            "baseline_fail": baseline["status"] == "FAIL",
            "regression_pass": regression["status"] == "PASS",
            "revision_pass": revision_run["status"] == "PASS",
            "actual_test_runs": 3,
            "baseline_run_id": baseline["run_id"],
            "regression_run_id": regression["run_id"],
            "revision_run_id": revision_run["run_id"],
            "baseline_log": baseline["log_path"],
            "regression_log": regression["log_path"],
            "revision_log": revision_run["log_path"],
            "upstream_sha": upstream_sha,
            "first_head": first_head,
            "revised_head": revised_head,
            "original_unchanged": True,
            "stages": stage_records,
            "review_loop": review_loop,
            "official_contributions": official_count,
            "excluded_demo": all_demo and official_count == 0,
            "records_all_demo": all_demo,
            "records_count": len(records),
            "event_count": len(store.events()),
            "mock_read_calls": client.read_calls,
            "network_requests": 0,
            "model_requests": 0,
            "public_writes": 0,
            "cc_validation_simulated": True,
            "approvals_simulated": True,
            "maintainer_review_simulated": True,
            "report_path": str(root / "demo_report.md"),
            "json_path": str(root / "report.json"),
        }
        write_json(root / "report.json", result)
        lines = [
            "# DEMO — 从真实本地修复到模拟上游 Review 的闭环",
            "",
            DEMO_NOTICE,
            "",
            "## 实际运行结果",
            "",
            f"- 原始复现：**{baseline['status']}**，退出码 {baseline['return_code']}，观察到 `{failure_signature}`。",
            f"- 最小修复：**{regression['status']}**，退出码 {regression['return_code']}。",
            f"- Review 后补充测试：**{revision_run['status']}**，退出码 {revision_run['return_code']}，共三个测试。",
            "- 原始仓库保持不变；修复和后续修改在独立 Worktree 中产生两个真实本地 Commit。",
            "",
            "## 模拟 Review 闭环",
            "",
            "| 事件 | 状态 | 正式贡献数 |",
            "|---|---|---|",
        ]
        for entry in review_loop:
            lines.append(
                f"| {entry['event']} | {entry['status']} | {entry['official_contributions']} |"
            )
        lines += [
            "",
            "## 贡献真实性检查",
            "",
            "虽然模拟 PR 到达 MERGED，所有数据库实体始终标记为 demo，正式贡献台账仍为 **0**。",
            "Profile 草稿保留原有内容，并保持等待真实可验证贡献的占位说明。",
            "本次没有真实 CC 审查、GitHub 搜索、公开 PR、维护者回复或上游合并。",
            "",
            "## 可检查的文件",
            "",
            "- `report.json`：阶段、SHA、测试运行 ID 和真实性检查。",
            "- `data/contribution.db`：状态、批准和追加式历史。",
            "- `reports/tests/`：实际 subprocess 测试日志和命令。",
            "- `reports/validation/`：独立验证输入包与明确标记的模拟 CC 报告。",
            "- `reports/submissions/`：最终草稿、Diff 与批准摘要。",
            "- `reports/followup/`：模拟 Review 与回复草稿。",
            "- `portfolio/`：正式贡献数为零的台账及 Profile 草稿。",
            "",
        ]
        atomic_write(root / "demo_report.md", "\n".join(lines))
        return result
