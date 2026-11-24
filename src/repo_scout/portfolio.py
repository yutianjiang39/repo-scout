"""Evidence-first contribution ledger and minimally scoped Profile README drafts."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .followup import _hash, _same, _valid_date, _verify_identity

START_MARKER = "<!-- repo-scout:contributions:start -->"
END_MARKER = "<!-- repo-scout:contributions:end -->"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain(value: Any) -> str:
    """Render untrusted GitHub titles as literal single-line table content."""
    value = str(value or "").replace("\r", " ").replace("\n", " ")
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for character in ("\\", "`", "*", "_", "[", "]", "|", "~"):
        value = value.replace(character, "\\" + character)
    return value.strip()


def _safe_link(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing evidence link")
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc != "github.com" or url.query
            or re.search(r"[\s()<>\\]", value)):
        raise ValueError("Contribution links must be canonical public GitHub HTTPS URLs")
    return value


def _valid_evidence(store: Any, submission: dict, username: str) -> tuple[bool, str]:
    if submission.get("provenance") != "live":
        return False, "non_live_provenance"
    verification = submission.get("verification") or {}
    evidence = submission.get("accepted_evidence") or {}
    if (verification.get("source") != "github_api" or not verification.get("eligible")
            or evidence.get("verified") is not True):
        return False, "missing_verified_acceptance"
    snapshot_id = submission.get("last_snapshot_id")
    if not snapshot_id or evidence.get("snapshot_id") != snapshot_id:
        return False, "stale_acceptance_snapshot"
    saved_snapshot = store.get("snapshot", snapshot_id)
    if not saved_snapshot or saved_snapshot.get("source") != "github_api" or saved_snapshot.get("provenance") != "live":
        return False, "missing_live_snapshot"
    payload = saved_snapshot.get("payload", {})
    if _hash(payload) != saved_snapshot.get("payload_hash") or saved_snapshot.get("submission_id") != submission.get("submission_id"):
        return False, "snapshot_integrity_mismatch"
    kind = submission.get("submission_type")
    item = payload.get("pull" if kind == "PR" else "issue", {})
    try:
        fresh_check = _verify_identity(submission, payload.get("repository", {}), item, kind, username, payload)
    except (KeyError, TypeError, ValueError):
        return False, "invalid_snapshot_shape"
    if not fresh_check["eligible"]:
        return False, "upstream_or_author_not_verified"
    if kind == "PR":
        merged_by = item.get("merged_by", {}).get("login") if item.get("merged_by") else None
        if (submission.get("status") != "MERGED" or item.get("merged") is not True
                or not _valid_date(item.get("merged_at"))
                or not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(item.get("merge_commit_sha") or ""))
                or not merged_by or _same(merged_by, username)
                or evidence.get("kind") != "PR_MERGED"
                or evidence.get("url") != submission.get("submission_url")
                or evidence.get("merge_commit_sha") != item.get("merge_commit_sha")):
            return False, "merge_not_independently_accepted"
    elif kind == "ISSUE":
        if submission.get("status") != "ACCEPTED" or evidence.get("kind") != "ISSUE_ACCEPTED":
            return False, "issue_not_accepted"
        if item.get("state") == "closed" and item.get("state_reason") in {"not_planned", "duplicate"}:
            return False, "issue_rejected_or_duplicate"
        decision = store.get("issue_acceptance", submission["submission_id"])
        feedback = store.get("feedback", evidence.get("feedback_id", ""))
        if (not decision or not decision.get("human_confirmed") or not decision.get("actor")
                or not decision.get("rationale") or not feedback or not feedback.get("active")
                or not feedback.get("authority_verified") or feedback.get("source") != "github_api"
                or feedback.get("source_type") != "comment" or feedback.get("snapshot_id") != snapshot_id
                or feedback.get("payload_hash") != decision.get("feedback_payload_hash")
                or feedback.get("payload_hash") != evidence.get("feedback_payload_hash")
                or feedback.get("url") != evidence.get("url")
                or decision.get("evidence_url") != evidence.get("url")):
            return False, "issue_acceptance_evidence_changed"
    else:
        return False, "unsupported_submission_type"
    try:
        _safe_link(submission.get("submission_url"))
        _safe_link(evidence.get("url"))
    except ValueError:
        return False, "invalid_evidence_url"
    return True, "accepted"


def _category(finding: dict, submission: dict) -> str:
    configured = finding.get("portfolio_category") or submission.get("portfolio_category")
    if configured:
        return str(configured)
    text = " ".join(str(finding.get(key, "")) for key in ("title", "category", "impact", "file_path")).lower()
    if any(term in text for term in ("order", "exchange", "websocket", "execution", "market data", "交易", "订单")):
        return "Trading Infrastructure"
    if any(term in text for term in ("pnl", "funding", "portfolio", "backtest", "quant", "data consistency", "数据")):
        return "Data & Quantitative Engineering"
    if any(term in text for term in ("performance", "reliability", "race", "concurrency", "memory", "recovery", "可靠")):
        return "Performance & Reliability"
    return "Rust & Python Engineering"


def sync_ledger(store: Any, *, username: str = "yutianjiang39") -> list[dict]:
    """Recompute official entries from current verified evidence; retain withdrawals.

    An issue and subsequent PR for the same finding are one contribution. PR
    evidence supersedes the issue entry, with the issue kept as supporting proof.
    """
    eligible: list[dict] = []
    for submission in store.list("submission"):
        valid, _reason = _valid_evidence(store, submission, username)
        if valid:
            eligible.append(submission)
    eligible.sort(key=lambda sub: (sub.get("submission_type") != "PR", sub.get("submission_url", "")))
    grouped: dict[tuple[str, str], list[dict]] = {}
    for sub in eligible:
        group = (sub["repository"].casefold(), str(sub.get("finding_id") or sub["submission_url"]).casefold())
        grouped.setdefault(group, []).append(sub)
    entries: list[dict] = []
    active_ids: set[str] = set()
    seen_urls: set[str] = set()
    for subs in grouped.values():
        sub = subs[0]
        url = sub["submission_url"]
        canonical = url.casefold().rstrip("/")
        if canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        cid = "CON-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
        active_ids.add(cid)
        finding = store.get("finding", sub.get("finding_id", "")) or {}
        validation = finding.get("validation") or {}
        implementation = finding.get("implementation") or {}
        if not isinstance(implementation, dict):
            implementation = {}
        evidence = sub["accepted_evidence"]
        record = {"contribution_id": cid, "submission_id": sub["submission_id"],
                  "finding_id": sub.get("finding_id"), "repository": sub["repository"],
                  "repository_url": f"https://github.com/{sub['repository']}", "submission_url": url,
                  "submission_type": sub["submission_type"], "title": sub.get("title", finding.get("title", "")),
                  "category": _category(finding, sub), "problem": finding.get("impact") or sub.get("problem") or finding.get("title", ""),
                  "technical_solution": sub.get("technical_solution") or implementation.get("summary") or finding.get("implementation_payload", {}).get("plan") or finding.get("proposed_fix") or "",
                  "changed_files": sub.get("changed_files") or implementation.get("changed_files") or [],
                  "tests": sub.get("tests") or implementation.get("tests") or finding.get("tests") or [],
                  "validation": {"result": validation.get("result"), "evidence": validation.get("evidence", [])},
                  "submitted_at": sub.get("created_at") or sub.get("submitted_at"),
                  "status": "MERGED" if sub["submission_type"] == "PR" else "ACCEPTED",
                  "outcome": "ACCEPTED", "merged_at": sub.get("merged_at"),
                  "accepted_evidence": evidence, "maintainer_feedback": sub.get("maintainer_feedback", []),
                  "supporting_submissions": sorted({s["submission_url"] for s in subs[1:]}),
                  "official": True, "provenance": "live", "attribution": "Recorded with an AI-assisted workflow; upstream acceptance verified",
                  "author": username}
        # The JSON ledger remains useful outside SQLite: include actual recorded
        # test observations and original feedback links, not only internal IDs.
        record["test_evidence"] = []
        for test in record["tests"]:
            run = store.get("test_run", test) if isinstance(test, str) else None
            if run:
                record["test_evidence"].append({k: run.get(k) for k in
                    ("run_id", "command", "purpose", "status", "return_code", "source_sha", "tree_hash", "log_hash", "finished_at")})
        record["feedback_evidence"] = []
        for feedback_id in record["maintainer_feedback"]:
            item = store.get("feedback", feedback_id) if isinstance(feedback_id, str) else None
            if item:
                record["feedback_evidence"].append({k: item.get(k) for k in
                    ("feedback_id", "url", "author", "type", "body", "updated_at", "authority_verified")})
        previous = store.get("contribution", cid)
        previous_data = {k: v for k, v in (previous or {}).items() if k != "_version"}
        if previous_data != record:
            record = store.put("contribution", cid, record, reason="Verified upstream contribution ledger sync",
                               actor="portfolio_sync", evidence=[evidence["url"]])
        else:
            record = previous
        entries.append(record)
    for previous in store.list("contribution"):
        if previous.get("official") and previous["contribution_id"] not in active_ids:
            value = dict(previous, official=False, status="WITHDRAWN",
                         withdrawal_reason="Acceptance no longer current, excluded by policy, or superseded by a merged PR")
            store.put("contribution", previous["contribution_id"], value,
                      reason="Remove unverified or superseded item from official portfolio; preserve history",
                      actor="portfolio_sync")
    return sorted(entries, key=lambda e: (e["repository"].casefold(), e["submission_url"]))


def portfolio_metrics(store: Any, *, username: str = "yutianjiang39") -> dict:
    entries = sync_ledger(store, username=username)
    tracked = [s for s in store.list("submission") if s.get("provenance") == "live"]
    pending = [s for s in tracked if s.get("status") in {"OPEN", "DRAFT", "UNDER_REVIEW", "CHANGES_REQUESTED", "APPROVED"}]
    return {"verified_contributions": len(entries),
            "upstream_projects": len({e["repository"].casefold() for e in entries}),
            "merged_prs": sum(e["submission_type"] == "PR" for e in entries),
            "accepted_issues": sum(e["submission_type"] == "ISSUE" for e in entries),
            "awaiting_review_or_action": len(pending),
            "changes_requested": sum(s.get("status") == "CHANGES_REQUESTED" for s in pending),
            "approved_not_merged": sum(s.get("status") == "APPROVED" for s in pending),
            "metric_definition": "Official entries are distinct evidence-backed contributions to external nonfork upstreams; submitted activity is not acceptance."}


def _profile_section(entries: list[dict]) -> str:
    lines = [START_MARKER, "## Open Source Contributions", "",
             "Engineering contributions across independent upstream projects, focused on trading "
             "infrastructure, Rust/Python engineering, reliability and quantitative data systems.", ""]
    if not entries:
        lines.extend(["Verified upstream contributions will be listed here after a merge or explicit maintainer acceptance.", ""])
    else:
        lines.extend(["### Selected Contributions", "", "| Project | Contribution | Type | Status | Evidence |",
                      "|---|---|---|---|---|"])
        for entry in entries:
            lines.append(f"| [{_plain(entry['repository'])}]({_safe_link(entry['repository_url'])}) "
                         f"| {_plain(entry['title'])} | {entry['submission_type']} "
                         f"| {'Merged' if entry['status'] == 'MERGED' else 'Maintainer accepted'} "
                         f"| [Upstream record]({_safe_link(entry['submission_url'])}) · "
                         f"[Acceptance]({_safe_link(entry['accepted_evidence']['url'])}) |")
        lines.append("")
    lines.extend(["Work may use AI assistance; each listed contribution has independently verified upstream "
                  "acceptance evidence. Submitted or merely approved work is not presented as merged.", END_MARKER])
    return "\n".join(lines)


def render_profile(store: Any, existing: str, *, username: str = "yutianjiang39") -> dict:
    """Only replace the managed marker block, preserving all surrounding bytes."""
    entries = sync_ledger(store, username=username)
    start_count, end_count = existing.count(START_MARKER), existing.count(END_MARKER)
    if start_count != end_count or start_count > 1:
        raise ValueError("Profile README has missing or duplicate contribution markers; resolve them before drafting")
    section = _profile_section(entries)
    newline = "\r\n" if "\r\n" in existing else "\n"
    section = section.replace("\n", newline)
    if start_count:
        start, end = existing.index(START_MARKER), existing.index(END_MARKER)
        if end < start:
            raise ValueError("Profile contribution markers are reversed")
        content = existing[:start] + section + existing[end + len(END_MARKER):]
    else:
        separator = "" if not existing else newline if existing.endswith(newline) else newline + newline
        content = existing + separator + section + newline
    diff = "".join(difflib.unified_diff(existing.splitlines(keepends=True), content.splitlines(keepends=True),
                                      fromfile="README.md", tofile="README.md (draft)"))
    return {"content": content, "diff": diff, "entries": entries,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "requires_approval": True, "changed": content != existing}


def export_portfolio(store: Any, output: Path, *, username: str = "yutianjiang39") -> dict:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    entries = sync_ledger(store, username=username)
    metrics = portfolio_metrics(store, username=username)
    ledger = {"schema_version": 1, "generated_at": _now(), "username": username,
              "metrics": metrics, "contributions": [{k: v for k, v in e.items() if k != "_version"} for e in entries],
              "policy": "Only live, independently accepted contributions to verified external upstreams; one finding is one contribution."}
    ledger_path = output / "contribution_ledger.json"
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 可验证的开源贡献", "", f"GitHub: `{username}`", "",
             f"- 正式贡献：{metrics['verified_contributions']}",
             f"- 独立上游项目：{metrics['upstream_projects']}",
             f"- 已合并 PR：{metrics['merged_prs']}",
             f"- 明确采纳的 Issue：{metrics['accepted_issues']}",
             f"- 待 Review 或待处理：{metrics['awaiting_review_or_action']}",
             f"- Changes Requested：{metrics['changes_requested']}",
             f"- Approved 但尚未合并：{metrics['approved_not_merged']}", "",
             "已提交、已关闭、已获批准都不会自动计入正式成果。每项成果保留原始问题、测试和上游接受证据。", ""]
    for entry in entries:
        lines.extend([f"## {_plain(entry['repository'])} — {_plain(entry['title'])}", "",
                      f"- Category: {_plain(entry['category'])}", f"- Status: {entry['status']}",
                      f"- Upstream: {_safe_link(entry['submission_url'])}",
                      f"- Acceptance: {_safe_link(entry['accepted_evidence']['url'])}",
                      f"- Problem: {_plain(entry['problem']) or '见关联 Finding'}",
                      f"- Solution: {_plain(entry['technical_solution']) or '见上游讨论或补丁'}",
                      f"- Attribution: {entry['attribution']}", ""])
    if not entries:
        lines.extend(["暂无满足证据要求的正式贡献。示例数据不会进入正式战绩。", ""])
    summary_path = output / "contribution_summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return {"ledger_path": str(ledger_path), "summary_path": str(summary_path), "metrics": metrics, "entries": entries}
