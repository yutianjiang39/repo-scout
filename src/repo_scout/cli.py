from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .secrets import secret
from . import __version__
from .common import atomic_write, digest, now, redact, slug, write_json
from .config import load_config, write_default
from .database import Store
from .review_gate import NETWORK_ERRORS, ROLES


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repo-scout",
        description="可验证的上游贡献闭环：发现 → 验证 → 修复 → Review → 采纳 → Profile",
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "--home",
        type=Path,
        default=Path(".repo-scout"),
        help="配置、数据库、Worktree 与报告目录",
    )
    p.add_argument("--config", type=Path, help="自定义 YAML；密钥只从环境变量读取")
    commands = p.add_subparsers(dest="command", required=True)
    from . import shelf

    shelf.add_parser(commands)
    init = commands.add_parser("init", help="初始化本地管理目录")
    init.add_argument(
        "--username", help="省略时保留已有账号；全新配置默认为 yutianjiang39"
    )
    commands.add_parser("doctor", help="检查依赖和配置；不显示凭证，也不联网")
    demo = commands.add_parser(
        "demo", help="离线真实测试 + 模拟 Review 全闭环；不计入正式贡献"
    )
    demo.add_argument("--output", type=Path, help="必须为空的新演示目录")
    for name in (
        "discover",
        "screen",
        "analyze-maintainers",
        "score",
        "repositories",
        "run-discovery",
    ):
        commands.add_parser(name)
    ap = commands.add_parser("approve-repositories")
    ap.add_argument("repositories", nargs="+")
    ap.add_argument("--actor", required=True)
    ap.add_argument("--reason", required=True)
    for name in ("clone", "prepare", "audit", "verify-ai", "run-audit"):
        cp = commands.add_parser(name)
        cp.add_argument(
            "repositories",
            nargs="*",
            help="owner/name；省略时使用已批准或已 Clone 的仓库",
        )
    commands.add_parser("report", help="导出批次与当前生命周期报告")
    imports = commands.add_parser("import-findings")
    imports.add_argument("file", type=Path)
    commands.add_parser("findings")
    single = commands.add_parser("finding")
    single.add_argument("finding_id")
    for name in ("preflight", "refresh-revision", "cc-packet", "cc-compare-packet"):
        cp = commands.add_parser(name)
        cp.add_argument("finding_id")
    validate = commands.add_parser(
        "validate", help="导入独立 CC 报告，或显式调用已配置 Claude Code"
    )
    validate.add_argument("finding_id")
    validate.add_argument("--comparison", action="store_true")
    seed = commands.add_parser(
        "record-assessment", help="保存候选证据说明，不授予双验收或实施资格"
    )
    seed.add_argument("finding_id")
    seed.add_argument("file", type=Path)
    choice = validate.add_mutually_exclusive_group()
    choice.add_argument("--report", type=Path)
    choice.add_argument(
        "--cc", action="store_true", help="实际调用 Claude Code，产生模型费用"
    )
    validate.add_argument(
        "--attest-cc",
        action="store_true",
        help="确认导入报告来自已进行的独立 Claude Code 验证",
    )
    validate.add_argument("--model")
    validate.add_argument("--budget", type=float, default=2.0)
    commands.add_parser(
        "run-validation", help="为未验证的候选生成最新 preflight 和 CC 任务包"
    )
    approve = commands.add_parser("approve", help="第一道批准：具体实施/Review修订方案")
    approve.add_argument(
        "--confirm-digest", help="Confirm the exact approval_digest returned by preview"
    )
    for cmd in (
        "review-start",
        "review-packet",
        "review-import",
        "review-status",
        "review-network-failure",
        "review-gemini",
    ):
        sub = commands.add_parser(cmd)
        sub.add_argument("subject")
        sub.add_argument(
            "artifact", choices=("finding", "diff", "submission", "profile")
        )
        if cmd == "review-packet":
            sub.add_argument("reviewer", choices=ROLES)
        if cmd in ("review-packet", "review-gemini"):
            sub.add_argument("phase", choices=("independent", "final"))
        if cmd == "review-network-failure":
            sub.add_argument("reviewer", choices=ROLES)
            sub.add_argument("error", choices=NETWORK_ERRORS)
        if cmd == "review-import":
            sub.add_argument("file", type=Path)
    approve.add_argument("finding_id")
    approve.add_argument("--actor", required=True)
    approve.add_argument("--plan", required=True)
    approve.add_argument(
        "--scope",
        action="append",
        default=[],
        help="允许修改的相对文件路径；目录以 / 结尾；可重复",
    )
    implement = commands.add_parser("implement")
    implement.add_argument("finding_id")
    implement.add_argument(
        "--cc", action="store_true", help="CC 返回最小补丁；仍需独立执行测试及 Review"
    )
    implement.add_argument("--model")
    implement.add_argument("--budget", type=float, default=2)
    apply = commands.add_parser("apply-patch")
    apply.add_argument("finding_id")
    apply.add_argument("file", type=Path)
    test = commands.add_parser(
        "test", help="先检查脚本，再用受限 Docker 或显式批准的 host runner 实际执行"
    )
    test.add_argument("finding_id")
    test.add_argument(
        "--purpose",
        choices=["baseline", "regression", "suite", "lint", "benchmark"],
        default="regression",
    )
    test.add_argument("--reviewed", action="store_true")
    test.add_argument("--runner", choices=["docker", "host"])
    test.add_argument("--allow-host-execution", action="store_true")
    test.add_argument("--image")
    test.add_argument("--timeout", type=int)
    test.add_argument(
        "--argv",
        required=True,
        help='JSON argv，例如 \'["python", "-m", "pytest", "tests/test_order.py"]\'；不经过 shell',
    )
    commit = commands.add_parser("commit", help="只在贡献 Worktree 中本地提交")
    commit.add_argument("finding_id")
    commit.add_argument("--message", required=True)
    commit.add_argument("--name", required=True)
    commit.add_argument("--email", required=True)
    review = commands.add_parser(
        "review-diff", help="记录用户或独立审查者对当前真实 Diff 的结论"
    )
    review.add_argument("finding_id")
    review.add_argument("--actor", required=True)
    review.add_argument("--assessment", required=True)
    review.add_argument("--approve", action="store_true")
    ai_review = commands.add_parser(
        "review-ai", help="配置的 final_diff 模型审查，结果只作建议"
    )
    ai_review.add_argument("finding_id")
    for name in ("prepare-pr", "prepare-issue", "prepare-update"):
        cp = commands.add_parser(name)
        cp.add_argument("finding_id")
        cp.add_argument("--title", required=True)
        cp.add_argument("--body-file", type=Path, required=True)
        cp.add_argument("--risk", required=True)
        if name != "prepare-issue":
            cp.add_argument("--fork", required=True)
    final = commands.add_parser(
        "approve-submit", help="第二道批准：绑定完整草稿 Digest"
    )
    final.add_argument(
        "--confirm-digest", help="Confirm the exact approval_digest returned by preview"
    )
    final.add_argument("finding_id")
    final.add_argument("--digest", required=True)
    final.add_argument("--actor", required=True)
    final.add_argument("--reason", required=True)
    for name in ("submit", "reconcile-submit"):
        cp = commands.add_parser(name)
        cp.add_argument("finding_id")
    for name in ("submissions", "status", "run-contribution"):
        commands.add_parser(name)
    track = commands.add_parser("track", help="只读导入并验证自己已有的上游 PR/Issue")
    track.add_argument("repository")
    track.add_argument("number", type=int)
    track.add_argument("--type", choices=["PR", "ISSUE"], required=True)
    track.add_argument(
        "--finding-id", help="关联原有 Finding，用于同问题 Issue/PR 去重"
    )
    for name in ("watch", "check-pr", "check-issues", "run-followup"):
        cp = commands.add_parser(
            name, help="单次增量同步；可由 cron/任务计划程序定时调用"
        )
        cp.add_argument("--force", action="store_true", help="忽略配置的最小检查间隔")
        cp.add_argument(
            "--once", action="store_true", help="显式声明只运行一次（默认即如此）"
        )
    for name in ("feedback", "followup-report", "portfolio", "portfolio-sync"):
        commands.add_parser(name)
    accept = commands.add_parser(
        "accept-issue", help="明确记录维护者采纳证据，不能用关闭状态代替"
    )
    accept.add_argument("submission_id")
    accept.add_argument("--evidence-url", required=True)
    accept.add_argument("--actor", required=True)
    accept.add_argument("--rationale", required=True)
    preview = commands.add_parser(
        "portfolio-preview", help="本地 README 预览；发布前仍须 live profile-draft"
    )
    preview.add_argument("--readme", type=Path)
    commands.add_parser(
        "profile-draft", help="验证 Profile 仓库存在，读取实际 README 后只修改贡献区域"
    )
    profile_approve = commands.add_parser("approve-profile")
    profile_approve.add_argument(
        "--confirm-digest", help="Confirm the exact approval_digest returned by preview"
    )
    profile_approve.add_argument("--digest", required=True)
    profile_approve.add_argument("--actor", required=True)
    profile_approve.add_argument("--reason", required=True)
    commands.add_parser(
        "profile-publish", help="需独立批准和正确写 Token；以 README SHA 防并发覆盖"
    )
    history = commands.add_parser("history")
    history.add_argument("--kind")
    history.add_argument("--id")
    return p


def _public(value: Any) -> Any:
    return json.loads(redact(json.dumps(value, ensure_ascii=False, default=str)))


def _selected(store: Store, names: list[str], *, cloned: bool = False) -> list[dict]:
    if names:
        return [store.require("repository", slug(name)) for name in names]
    records = (
        [r for r in store.list("repository") if r.get("original_path")]
        if cloned
        else [r for r in store.list("repository") if r.get("status") == "APPROVED"]
    )
    if not records:
        raise ValueError(
            "No eligible repositories. Discover/screen/score, then approve-repositories owner/name"
        )
    return records


def _client(store: Store, config: dict):
    from .github import GitHubClient

    settings = config["github"]
    return GitHubClient(
        store,
        read_token=secret(settings["read_token_env"]),
        write_token=secret(settings["write_token_env"]),
        api_version=settings.get("api_version", "2026-03-10"),
    )


def _local_preview(store: Store, root: Path, username: str, existing: str = "") -> dict:
    from .portfolio import export_portfolio, render_profile

    export = export_portfolio(store, root / "portfolio", username=username)
    rendered = render_profile(store, existing, username=username)
    atomic_write(root / "portfolio" / "profile_readme_preview.md", rendered["content"])
    atomic_write(root / "portfolio" / "profile_readme_preview.diff", rendered["diff"])
    return {
        **export,
        "preview_path": str(root / "portfolio" / "profile_readme_preview.md"),
        "notice": "Local preview only. profile-draft fetches the actual README before publication.",
    }


def _status(store: Store, username: str) -> dict:
    from .portfolio import portfolio_metrics

    findings = store.list("finding")
    return {
        "metrics": portfolio_metrics(store, username=username),
        "repositories": dict(
            Counter(r.get("status", "UNKNOWN") for r in store.list("repository"))
        ),
        "findings": dict(Counter(f["status"] for f in findings)),
        "next_actions": [
            {
                "finding_id": f["finding_id"],
                "status": f["status"],
                "repository": f["repository"],
                "action": {
                    "CANDIDATE": "preflight / CC validation",
                    "CC_VALIDATING": "baseline / validate",
                    "CONFIRMED": "approve concrete implementation plan",
                    "HUMAN_APPROVED": "implement",
                    "IMPLEMENTING": "complete focused fix and actual tests",
                    "TESTING": "review-diff / prepare-pr",
                    "READY_FOR_REVIEW": "review draft, approve-submit",
                    "APPROVED_TO_SUBMIT": "submit",
                    "CHANGES_REQUESTED": "refresh-revision / approve revised plan",
                    "SUBMITTED": "watch",
                    "UNDER_REVIEW": "watch",
                }.get(f["status"], "inspect evidence"),
            }
            for f in findings
            if f["status"]
            not in {
                "MERGED",
                "ACCEPTED",
                "REJECTED",
                "ALREADY_FIXED",
                "DUPLICATE",
                "DESIGN_DECISION",
            }
        ],
    }


async def dispatch(
    args: argparse.Namespace, store: Store, root: Path, config: dict
) -> Any:
    from . import workflow
    from .implementation import (
        clone_repository,
        commit_changes,
        head,
        run_command,
        snapshot,
    )
    from .validation import (
        import_cc_report,
        make_cc_packet,
        preflight,
        run_cc_validation,
    )

    command = args.command
    if command in (
        "review-start",
        "review-packet",
        "review-import",
        "review-status",
        "review-network-failure",
        "review-gemini",
    ):
        from . import review_gate as gate

        subject, kind = args.subject, args.artifact
        if command == "review-network-failure":
            return gate.network_failure(store, subject, kind, args.reviewer, args.error)
        if command == "review-gemini":
            return await gate.collect_gemini(store, subject, kind, args.phase)
        if command == "review-start":
            return {"round_id": gate.start(store, subject, kind)}
        if command == "review-packet":
            return gate.packet(store, subject, kind, args.reviewer, args.phase)
        if command == "review-import":
            return gate.receive(
                store, subject, kind, json.loads(args.file.read_text("utf-8"))
            )
        return gate.status(store, subject, kind)
    username = config["username"]
    output = root / "reports" / "discovery"
    if command == "doctor":
        return {
            "python": sys.version.split()[0],
            "git": shutil.which("git"),
            "claude": shutil.which("claude"),
            "docker": shutil.which("docker"),
            "username": username,
            "profile_repository": config["profile_repository"],
            "credentials_configured": {
                name: bool(secret(name))
                for name in [
                    config["github"]["read_token_env"],
                    config["github"]["write_token_env"],
                    "ANTHROPIC_API_KEY",
                ]
            },
            "configured_roles": list(config["roles"]),
            "public_writes": "require explicit content-bound approval",
            "network_checked": False,
        }
    if command == "init":
        return {
            "home": str(root),
            "config": str(root / "config.yaml"),
            "database": str(store.path),
            "username": username,
            "next": "repo-scout doctor; repo-scout discover",
        }
    if command == "repositories":
        return store.list("repository")
    if command in {
        "discover",
        "screen",
        "analyze-maintainers",
        "score",
        "run-discovery",
    }:
        from . import discovery

        if command == "screen":
            return discovery.screen(store, config, output)
        if command == "score":
            return discovery.score(store, config, output)
        async with _client(store, config) as client:
            if command == "discover":
                return await discovery.discover(client, store, config, output)
            if command == "analyze-maintainers":
                return await discovery.analyze_maintainers(
                    client, store, config, output
                )
            results = {
                "discovery": await discovery.discover(client, store, config, output)
            }
            results["screening"] = discovery.screen(store, config, output)
            results["maintainers"] = await discovery.analyze_maintainers(
                client, store, config, output
            )
            results["scoring"] = discovery.score(store, config, output)
            results["next"] = (
                "Inspect evidence and approve target repositories explicitly"
            )
            return results
    if command == "approve-repositories":
        results = []
        for name in args.repositories:
            repo = store.require("repository", slug(name))
            if repo.get("status") in {"EXCLUDED", "NEEDS_DATA"}:
                raise ValueError(
                    "Resolve license/activity/exclusion evidence before approving "
                    + name
                )
            results.append(
                store.put(
                    "repository",
                    repo["repository"],
                    {**repo, "status": "APPROVED", "approval_reason": args.reason},
                    reason=args.reason,
                    actor=args.actor,
                    expected_version=repo["_version"],
                )
            )
        write_json(
            output / "repositories_approved.json",
            [r for r in store.list("repository") if r["status"] == "APPROVED"],
        )
        return results
    if command in {"clone", "prepare", "audit", "verify-ai", "run-audit"}:
        from .bundle import prepare_bundle
        from .audit import audit_bundle

        selected = _selected(
            store,
            args.repositories,
            cloned=command in {"prepare", "audit", "verify-ai"},
        )
        results = []
        for repo in selected:
            try:
                name = repo["repository"]
                if command in {"clone", "run-audit"}:
                    original = clone_repository(
                        root, name, default_branch=repo.get("default_branch", "main")
                    )
                    repo = store.put(
                        "repository",
                        name,
                        {
                            **repo,
                            "original_path": str(original),
                            "head_sha": head(original),
                            "clone_error": None,
                        },
                        reason="upstream clone refreshed",
                        actor="clone",
                    )
                if command == "clone":
                    results.append(
                        {
                            "repository": name,
                            "original_path": repo["original_path"],
                            "head_sha": repo["head_sha"],
                        }
                    )
                    continue
                bundle = (
                    root / "repositories" / name.replace("/", "__") / "review_bundle"
                )
                manifest = prepare_bundle(
                    Path(repo["original_path"]), bundle, repository=name
                )
                if command == "prepare":
                    results.append(manifest)
                    continue
                result = await audit_bundle(
                    bundle,
                    config,
                    store,
                    output=root / "reports" / "audit" / name.replace("/", "__"),
                )
                results.append(result)
            except (ValueError, OSError, RuntimeError) as exc:
                results.append(
                    {
                        "repository": repo["repository"],
                        "status": "BLOCKED",
                        "error": redact(str(exc)),
                    }
                )
                store.put(
                    "repository",
                    repo["repository"],
                    {**repo, "last_stage_error": redact(str(exc))},
                    reason="stage failure preserved for resume",
                    actor=command,
                )
        write_json(root / "reports" / "audit" / "batch_summary.json", results)
        return results
    if command == "import-findings":
        items = json.loads(args.file.read_text())
        if isinstance(items, dict):
            items = items.get("findings", [items])
        return [workflow.add_finding(store, item) for item in items]
    if command == "findings":
        return store.list("finding")
    if command == "finding":
        return store.require("finding", args.finding_id)
    if command in {"preflight", "refresh-revision", "run-validation"}:
        ids = (
            [args.finding_id]
            if command != "run-validation"
            else [
                f["finding_id"]
                for f in store.list("finding")
                if f["status"]
                in {
                    "CANDIDATE",
                    "NEEDS_VALIDATION",
                    "CC_VALIDATING",
                    "PROBABLE",
                    "UNVERIFIED",
                    "BLOCKED",
                }
            ]
        )
        results = []
        async with _client(store, config) as client:
            for fid in ids:
                if command == "refresh-revision":
                    from .followup import sync_submission

                    f = store.require("finding", fid)
                    await sync_submission(
                        store,
                        client,
                        f["submission_id"],
                        username=username,
                        trusted_maintainers=config["tracking"].get(
                            "trusted_maintainers"
                        ),
                    )
                await preflight(store, client, root, fid)
                packet = make_cc_packet(store, root, fid)
                results.append({k: v for k, v in packet.items() if k != "manifest"})
        return results
    if command in {"cc-packet", "cc-compare-packet"}:
        return make_cc_packet(
            store, root, args.finding_id, comparison=command == "cc-compare-packet"
        )
    if command == "validate":
        if args.cc:
            return run_cc_validation(
                store,
                root,
                args.finding_id,
                model=args.model,
                max_budget_usd=args.budget,
                comparison=args.comparison,
            )
        if args.report:
            return import_cc_report(
                store,
                root,
                args.finding_id,
                json.loads(args.report.read_text("utf-8")),
                attested_cc=args.attest_cc,
                comparison=args.comparison,
            )
        return make_cc_packet(store, root, args.finding_id, comparison=args.comparison)
    if command == "record-assessment":
        from .validation import _apply_assessment
        from .bundle import secret_reason

        text = args.file.read_text("utf-8")
        if secret_reason(text) or redact(text) != text:
            raise ValueError("Sensitive assessment withheld")
        return _apply_assessment(
            store,
            root,
            args.finding_id,
            json.loads(text),
            attested_cc=False,
            provenance="candidate-evidence",
        )
    if command == "approve":
        return workflow.approve_finding(
            store,
            args.finding_id,
            actor=args.actor,
            plan=args.plan,
            scope=args.scope,
            confirm_digest=args.confirm_digest,
        )
    if command == "implement":
        finding = store.require("finding", args.finding_id)
        if (
            finding.get("implementation_payload", {}).get("revision")
            and finding["status"] == "HUMAN_APPROVED"
        ):
            from .followup import sync_submission

            async with _client(store, config) as client:
                await sync_submission(
                    store,
                    client,
                    finding["submission_id"],
                    username=username,
                    trusted_maintainers=config["tracking"].get("trusted_maintainers"),
                )
        if args.cc:
            return workflow.implement_with_cc(
                store,
                root,
                args.finding_id,
                model=args.model,
                max_budget_usd=args.budget,
            )
        return workflow.begin_implementation(store, root, args.finding_id)
    if command == "apply-patch":
        return workflow.apply_implementation_patch(
            store, root, args.finding_id, args.file.read_text()
        )
    if command == "test":
        argv = json.loads(args.argv)
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) for a in argv)
        ):
            raise ValueError("--argv must be a nonempty JSON array of strings")
        finding = store.require("finding", args.finding_id)
        if args.purpose != "baseline":
            finding = workflow.mark_testing(store, args.finding_id)
        settings = config["execution"]
        run = run_command(
            store,
            root,
            finding,
            argv,
            purpose=args.purpose,
            reviewed=args.reviewed,
            runner=args.runner or settings["runner"],
            allow_host=args.allow_host_execution,
            image=args.image or settings["image"],
            timeout=args.timeout or settings["timeout_seconds"],
        )
        return run
    if command == "commit":
        finding = store.require("finding", args.finding_id)
        workflow._active_plan(store, finding)
        if finding["status"] not in {"IMPLEMENTING", "TESTING"}:
            raise ValueError("Commit only from an approved active implementation")
        return {
            "head_sha": commit_changes(
                Path(finding["workspace"]),
                message=args.message,
                name=args.name,
                email=args.email,
            )
        }
    if command == "review-diff":
        return workflow.record_diff_review(
            store,
            args.finding_id,
            actor=args.actor,
            assessment=args.assessment,
            approved=args.approve,
        )
    if command == "review-ai":
        from .audit import review_diff

        finding = store.require("finding", args.finding_id)
        state = snapshot(Path(finding["workspace"]), finding["base_sha"])
        review = await review_diff(
            state["diff"],
            config,
            store,
            context=canonical_safe(finding.get("validation", {})),
        )
        write_json(
            root
            / "reports"
            / "implementation"
            / args.finding_id
            / "ai_diff_review.json",
            review,
        )
        return review
    if command in {"prepare-pr", "prepare-issue", "prepare-update"}:
        return workflow.prepare_draft(
            store,
            root,
            args.finding_id,
            submission_type={
                "prepare-pr": "PR",
                "prepare-issue": "ISSUE",
                "prepare-update": "PR_UPDATE",
            }[command],
            title=args.title,
            body=args.body_file.read_text(),
            risk=args.risk,
            fork=getattr(args, "fork", ""),
        )
    if command == "approve-submit":
        return workflow.approve_submission(
            store,
            args.finding_id,
            reviewed_digest=args.digest,
            actor=args.actor,
            reason=args.reason,
            confirm_digest=args.confirm_digest,
        )
    if command in {"submit", "reconcile-submit"}:
        async with _client(store, config) as client:
            if command == "submit":
                return await workflow.submit(
                    store, client, root, config, args.finding_id
                )
            return await workflow.reconcile_submission(
                store, client, args.finding_id, username=username
            )
    if command == "submissions":
        return store.list("submission")
    if command == "status":
        return _status(store, username)
    if command == "run-contribution":
        prepared = []
        for finding in store.list("finding"):
            if (
                finding["status"] == "HUMAN_APPROVED"
                and finding.get("validation", {}).get("recommended_action") == "PR"
            ):
                prepared.append(
                    workflow.begin_implementation(store, root, finding["finding_id"])[
                        "finding_id"
                    ]
                )
        return {"worktrees_prepared": prepared, **_status(store, username)}
    if command == "track":
        from .followup import sync_submission

        repository = slug(args.repository)
        if args.number <= 0:
            raise ValueError("Submission number must be positive")
        fid = args.finding_id
        if fid and store.require("finding", fid)["repository"] != repository:
            raise ValueError("Finding and submission repositories differ")
        sid = "TRACK-" + digest([repository, args.type, args.number])[:16]
        previous = next(
            (
                s
                for s in store.list("submission")
                if s["repository"].casefold() == repository
                and s["submission_type"] == args.type
                and s["number"] == args.number
            ),
            None,
        )
        if previous:
            sid = previous["submission_id"]
        else:
            store.put(
                "submission",
                sid,
                {
                    "submission_id": sid,
                    "finding_id": fid,
                    "repository": repository,
                    "number": args.number,
                    "submission_type": args.type,
                    "status": "UNKNOWN",
                    "provenance": "live",
                    "submission_url": f"https://github.com/{repository}/{'pull' if args.type == 'PR' else 'issues'}/{args.number}",
                },
                reason="explicit upstream tracking request; verification still required",
                actor="user",
            )
        async with _client(store, config) as client:
            return await sync_submission(
                store,
                client,
                sid,
                username=username,
                trusted_maintainers=config["tracking"].get("trusted_maintainers"),
            )
    if command in {
        "watch",
        "check-pr",
        "check-issues",
        "run-followup",
        "portfolio-sync",
    }:
        from .followup import sync_submission, followup_report

        results, errors = [], []
        interval = timedelta(hours=config["tracking"]["interval_hours"])
        async with _client(store, config) as client:
            for submission in store.list("submission"):
                if submission.get("provenance") != "live":
                    continue
                if command == "check-pr" and submission["submission_type"] != "PR":
                    continue
                if (
                    command == "check-issues"
                    and submission["submission_type"] != "ISSUE"
                ):
                    continue
                checked = submission.get("last_checked_at")
                force = getattr(args, "force", False) or command == "portfolio-sync"
                if (
                    checked
                    and not force
                    and datetime.now(timezone.utc) - datetime.fromisoformat(checked)
                    < interval
                ):
                    continue
                try:
                    updated = await sync_submission(
                        store,
                        client,
                        submission["submission_id"],
                        username=username,
                        trusted_maintainers=config["tracking"].get(
                            "trusted_maintainers"
                        ),
                    )
                    results.append(
                        {
                            "submission_id": updated["submission_id"],
                            "status": updated["status"],
                            "actions": updated.get("required_actions", []),
                        }
                    )
                except (ValueError, RuntimeError, OSError) as exc:
                    errors.append(
                        {
                            "submission_id": submission["submission_id"],
                            "error": redact(str(exc)),
                        }
                    )
        atomic_write(
            root / "reports" / "followup" / "report.md", followup_report(store)
        )
        return {
            "synced": results,
            "errors": errors,
            "portfolio": _local_preview(store, root, username),
            "one_shot": True,
        }
    if command == "feedback":
        return store.list("feedback")
    if command == "followup-report":
        from .followup import followup_report

        result = followup_report(store)
        atomic_write(root / "reports" / "followup" / "report.md", result)
        return result
    if command == "accept-issue":
        from .followup import record_issue_acceptance, sync_submission

        async with _client(store, config) as client:
            await sync_submission(
                store,
                client,
                args.submission_id,
                username=username,
                trusted_maintainers=config["tracking"].get("trusted_maintainers"),
            )
        result = record_issue_acceptance(
            store,
            args.submission_id,
            evidence_url=args.evidence_url,
            actor=args.actor,
            rationale=args.rationale,
        )
        _local_preview(store, root, username)
        return result
    if command in {"portfolio", "portfolio-preview"}:
        return _local_preview(
            store,
            root,
            username,
            args.readme.read_text() if getattr(args, "readme", None) else "",
        )
    if command == "profile-draft":
        async with _client(store, config) as client:
            return await workflow.prepare_profile(store, client, root, config)
    if command == "approve-profile":
        return workflow.approve_profile(
            store,
            config,
            reviewed_digest=args.digest,
            actor=args.actor,
            reason=args.reason,
            confirm_digest=args.confirm_digest,
        )
    if command == "profile-publish":
        async with _client(store, config) as client:
            return await workflow.publish_profile(store, client, config)
    if command == "history":
        return store.events(args.kind, args.id)
    if command == "report":
        from .followup import followup_report

        state = _status(store, username)
        write_json(root / "reports" / "status.json", state)
        all_findings = store.list("finding")
        write_json(root / "reports" / "audit" / "batch_findings.json", all_findings)
        atomic_write(
            root / "reports" / "followup" / "report.md", followup_report(store)
        )
        return {
            "status": state,
            "portfolio": _local_preview(store, root, username),
            "reports": str(root / "reports"),
        }
    raise ValueError("Unsupported command")


def canonical_safe(value: Any) -> str:
    return json.dumps(_public(value), ensure_ascii=False, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.home.expanduser().resolve()
    try:
        if args.command == "shelf":
            from . import shelf

            result = shelf.run(args, root)
        elif args.command == "demo":
            from .demo import run_demo

            result = asyncio.run(run_demo((args.output or root / "demo").resolve()))
        else:
            if args.command == "init":
                write_default(root / "config.yaml")
                config = load_config(root / "config.yaml")
                if args.username:
                    config.update(
                        username=args.username,
                        profile_repository=args.username + "/" + args.username,
                    )
                atomic_write(
                    root / "config.yaml",
                    yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                )
            if args.config and not args.config.exists():
                raise ValueError("The specified configuration file does not exist")
            config = load_config(args.config or root / "config.yaml")
            with Store(root / "data" / "contribution.db") as store:
                result = asyncio.run(dispatch(args, store, root, config))
        if isinstance(result, str):
            print(redact(result))
        else:
            print(
                json.dumps(_public(result), ensure_ascii=False, indent=2, default=str)
            )
        if isinstance(result, dict) and result.get("status") in {"FAIL", "TIMEOUT"}:
            return 1
        if isinstance(result, dict) and result.get("errors"):
            return 1
        if isinstance(result, list) and any(
            isinstance(x, dict) and x.get("status") in {"BLOCKED", "PARTIAL"}
            for x in result
        ):
            return 1
        return 0
    except KeyboardInterrupt:
        print(
            "Interrupted. Durable records are preserved; reconcile any uncertain public write.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {"error": redact(str(exc)), "type": type(exc).__name__},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
