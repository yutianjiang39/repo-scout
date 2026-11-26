"""Read-only upstream follow-up and evidence-bound human issue decisions.

GitHub comments are data, not commands. This module never posts replies, merges,
or treats an issue closure/positive sentence as acceptance.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _login(value: Any) -> str:
    return str(value.get("login", "")) if isinstance(value, dict) else str(value or "")


def _same(a: Any, b: Any) -> bool:
    return bool(a) and bool(b) and str(a).casefold() == str(b).casefold()


def _canonical_url(slug: str, kind: str, number: int) -> str:
    return f"https://github.com/{slug}/{'pull' if kind == 'PR' else 'issues'}/{number}"


def _url_matches(actual: Any, expected: str, *, fragment: bool = False) -> bool:
    if not isinstance(actual, str):
        return False
    parsed, wanted = urlsplit(actual), urlsplit(expected)
    return (
        parsed.scheme == "https"
        and parsed.netloc.casefold() == "github.com"
        and not parsed.query
        and parsed.path.rstrip("/").casefold() == wanted.path.casefold()
        and (fragment or not parsed.fragment)
    )


def _valid_date(value: Any) -> bool:
    try:
        return (
            bool(value)
            and datetime.fromisoformat(str(value).replace("Z", "+00:00")).tzinfo
            is not None
        )
    except (TypeError, ValueError):
        return False


def _trusted_for(slug: str, trusted: Any) -> set[str]:
    if isinstance(trusted, dict):
        values = next((v for k, v in trusted.items() if _same(k, slug)), [])
    else:
        values = trusted or []
    if isinstance(values, str):
        values = [values]
    return {str(x).casefold() for x in values}


def _authority(item: dict, slug: str, username: str, trusted: Any) -> tuple[bool, str]:
    login = _login(item.get("user"))
    if not login or _same(login, username):
        return False, "self_or_unknown"
    if str(item.get("author_association", "")).upper() == "OWNER" and _same(
        login, slug.split("/", 1)[0]
    ):
        return True, "github_repository_owner"
    if login.casefold() in _trusted_for(slug, trusted):
        return True, "explicit_trusted_maintainer_config"
    # Organization membership and COLLABORATOR alone do not prove maintainer
    # authority for this particular repository. Require user-reviewed trust.
    return False, "unknown"


def _feedback_type(item: dict, source_type: str) -> str:
    state = str(item.get("state", "")).upper()
    if source_type == "review" and state in {
        "APPROVED",
        "CHANGES_REQUESTED",
        "DISMISSED",
    }:
        return state
    # Content heuristics only suggest a triage action; never prove acceptance.
    body = str(item.get("body") or "").lower()
    if any(x in body for x in ("add a test", "add tests", "regression test", "测试")):
        return "NEEDS_TESTS"
    if "?" in body or "？" in body:
        return "TECHNICAL_QUESTION"
    return "GENERAL_DISCUSSION"


def _reply_draft(kind: str) -> str:
    if kind == "CHANGES_REQUESTED":
        return (
            "Thanks for the review. I will check each requested change against the original "
            "reproduction, keep the patch focused, and report the regression-test results "
            "when the revision is ready."
        )
    if kind == "NEEDS_TESTS":
        return (
            "Thanks for pointing out the missing coverage. I will check the relevant boundary "
            "conditions and add a focused regression test before requesting another review."
        )
    if kind == "TECHNICAL_QUESTION":
        return (
            "Thanks for the question. I will verify the behavior against the current code and "
            "reproduction before responding with the specific evidence."
        )
    return "Thanks for reviewing this contribution. I will check your feedback and follow up with the relevant evidence."


def _latest_reviews(
    reviews: Iterable[dict], head: str, slug: str, username: str, trusted: Any
) -> tuple[list[dict], list[dict]]:
    decisive: dict[str, dict] = {}
    for review in sorted(
        reviews, key=lambda r: (str(r.get("submitted_at") or ""), int(r.get("id") or 0))
    ):
        authority, _ = _authority(review, slug, username, trusted)
        state = str(review.get("state", "")).upper()
        if authority and state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            decisive[_login(review.get("user")).casefold()] = review
    active, stale = [], []
    for review in decisive.values():
        state = str(review.get("state", "")).upper()
        if state == "DISMISSED":
            continue
        if state == "APPROVED" and (not head or review.get("commit_id") != head):
            stale.append(review)
            continue
        active.append(review)
    return active, stale


def _ci_state(snapshot: dict, head: str) -> dict:
    checks = snapshot.get("checks") or []
    unavailable = isinstance(checks, dict) and checks.get("available") is False
    if isinstance(checks, dict):
        checks = checks.get("check_runs", [])
    runs: dict[str, dict] = {}
    for run in checks:
        if run.get("head_sha") and run["head_sha"] != head:
            continue
        key = f"{run.get('app', {}).get('id', '')}:{run.get('name', '')}"
        if key not in runs or int(run.get("id") or 0) > int(runs[key].get("id") or 0):
            runs[key] = run
    raw_statuses = snapshot.get("statuses") or []
    unavailable = unavailable or (
        isinstance(raw_statuses, dict) and raw_statuses.get("available") is False
    )
    if isinstance(raw_statuses, dict):
        raw_statuses = raw_statuses.get("statuses", [])
    statuses: dict[str, dict] = {}
    for status in sorted(
        raw_statuses,
        key=lambda s: (str(s.get("created_at") or ""), int(s.get("id") or 0)),
    ):
        statuses[str(status.get("context", "default"))] = status
    failed, pending = [], []
    for key, run in runs.items():
        if run.get("conclusion") in {
            "failure",
            "timed_out",
            "cancelled",
            "action_required",
            "startup_failure",
            "stale",
        }:
            failed.append(run.get("html_url") or key)
        elif run.get("status") != "completed" or run.get("conclusion") not in {
            "success",
            "neutral",
            "skipped",
        }:
            pending.append(run.get("html_url") or key)
    for key, status in statuses.items():
        if status.get("state") in {"failure", "error"}:
            failed.append(status.get("target_url") or key)
        elif status.get("state") != "success":
            pending.append(status.get("target_url") or key)
    state = (
        "FAILURE"
        if failed
        else "UNKNOWN"
        if unavailable
        else "PENDING"
        if pending
        else "SUCCESS"
        if runs or statuses
        else "UNKNOWN"
    )
    return {
        "state": state,
        "failed": failed,
        "pending": pending,
        "checks_observed": len(runs),
        "statuses_observed": len(statuses),
        "head_sha": head,
    }


def _verify_identity(
    submission: dict,
    repository: dict,
    item: dict,
    kind: str,
    username: str,
    snapshot: dict,
) -> dict:
    slug = submission["repository"]
    number = int(submission["number"])
    url = _canonical_url(slug, kind, number)
    reasons: list[str] = []
    same_repository = _same(repository.get("full_name"), slug)
    repository_id = repository.get("id")
    owner = _login(repository.get("owner"))
    foreign_upstream = (
        same_repository
        and isinstance(repository_id, int)
        and repository_id > 0
        and repository.get("fork") is False
        and bool(owner)
        and not _same(owner, username)
        and repository.get("private") is False
        and _url_matches(repository.get("html_url"), f"https://github.com/{slug}")
    )
    if not foreign_upstream:
        reasons.append("repository_is_not_verified_public_foreign_nonfork_upstream")
    if submission.get("provenance") != "live":
        reasons.append("submission_provenance_is_not_live")
    identity = item.get("number") == number and _url_matches(item.get("html_url"), url)
    if not identity:
        reasons.append("submission_identity_mismatch")
    author_verified = _same(_login(item.get("user")), username)
    if not author_verified:
        reasons.append("configured_user_is_not_submission_author")
    if kind == "PR":
        base = item.get("base", {}).get("repo") or {}
        if (
            base.get("id") != repository_id
            or not _same(base.get("full_name"), slug)
            or base.get("fork") is not False
        ):
            foreign_upstream = False
            reasons.append("pull_base_repository_mismatch")
    elif item.get("pull_request"):
        identity = False
        reasons.append("issue_endpoint_returned_pull_request")
    else:
        expected_api = f"https://api.github.com/repos/{slug}"
        if str(item.get("repository_url", "")).casefold() != expected_api.casefold():
            identity = False
            reasons.append("issue_repository_url_mismatch")
    if snapshot.get("complete") is False:
        reasons.append("snapshot_incomplete")
    return {
        "source": "github_api",
        "checked_at": _now(),
        "repository": slug,
        "repository_id": repository_id,
        "upstream_verified": foreign_upstream,
        "author_verified": author_verified,
        "identity_verified": identity,
        "eligible": not reasons,
        "reasons": reasons,
        "payload_hash": _hash(snapshot),
    }


def _persist_feedback(
    store: Any,
    submission: dict,
    snapshot: dict,
    snapshot_id: str,
    username: str,
    trusted: Any,
) -> list[dict]:
    slug, sid = submission["repository"], submission["submission_id"]
    canonical = _canonical_url(
        slug, submission["submission_type"], int(submission["number"])
    )
    # GitHub issue-comment links for PRs use /pull/N; accept /issues/N too.
    alternative = _canonical_url(slug, "ISSUE", int(submission["number"]))
    seen: set[str] = set()
    feedback: list[dict] = []
    for source_type, source in (
        ("comment", snapshot.get("comments", [])),
        ("review", snapshot.get("reviews", [])),
        ("review_comment", snapshot.get("review_comments", [])),
    ):
        for item in source or []:
            if not item.get("id"):
                continue
            fid = f"{sid}:{source_type}:{item['id']}"
            seen.add(fid)
            authoritative, method = _authority(item, slug, username, trusted)
            url = str(item.get("html_url") or "")
            linked = bool(urlsplit(url).fragment) and (
                _url_matches(url, canonical, fragment=True)
                or _url_matches(url, alternative, fragment=True)
            )
            kind = _feedback_type(item, source_type)
            digest = _hash(
                {
                    "id": item["id"],
                    "body": item.get("body"),
                    "user": item.get("user"),
                    "state": item.get("state"),
                    "author_association": item.get("author_association"),
                    "html_url": url,
                    "commit_id": item.get("commit_id"),
                    "updated_at": item.get("updated_at"),
                }
            )
            value = {
                "feedback_id": fid,
                "submission_id": sid,
                "repository": slug,
                "source": "github_api",
                "source_type": source_type,
                "external_id": item["id"],
                "author": _login(item.get("user")),
                "author_association": item.get("author_association", "UNKNOWN"),
                "authority_verified": authoritative and linked,
                "authority_method": method,
                "url": url,
                "body": str(item.get("body") or ""),
                "body_hash": _hash(str(item.get("body") or "")),
                "payload_hash": digest,
                "type": kind,
                "classification_basis": "review_state"
                if source_type == "review"
                else "triage_heuristic",
                "state": item.get("state"),
                "commit_id": item.get("commit_id"),
                "created_at": item.get("submitted_at") or item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "active": True,
                "snapshot_id": snapshot_id,
                "provenance": submission.get("provenance", "imported"),
                "reply_draft": _reply_draft(kind),
                "reply_requires_approval": True,
            }
            previous = store.get("feedback", fid)
            if not previous or any(previous.get(k) != v for k, v in value.items()):
                value = store.put(
                    "feedback",
                    fid,
                    value,
                    reason="Fetched upstream feedback",
                    actor="github_sync",
                    evidence=[url],
                )
            else:
                value = previous
            feedback.append(value)
    if snapshot.get("complete", True):
        for previous in store.list("feedback"):
            if (
                previous.get("submission_id") == sid
                and previous.get("active")
                and previous["feedback_id"] not in seen
            ):
                value = dict(
                    previous,
                    active=False,
                    inactive_reason="not_in_latest_complete_snapshot",
                )
                store.put(
                    "feedback",
                    previous["feedback_id"],
                    value,
                    reason="Feedback absent from complete upstream snapshot",
                    actor="github_sync",
                )
    return feedback


def _acceptance_valid(
    store: Any, submission: dict, feedback: list[dict]
) -> dict | None:
    acceptance = store.get("issue_acceptance", submission["submission_id"])
    if (
        not acceptance
        or not acceptance.get("human_confirmed")
        or not acceptance.get("actor")
    ):
        return None
    if not submission.get("verification", {}).get("eligible"):
        return None
    for item in feedback:
        if (
            item.get("feedback_id") == acceptance.get("feedback_id")
            and item.get("active")
            and item.get("authority_verified")
            and item.get("source") == "github_api"
            and item.get("payload_hash") == acceptance.get("feedback_payload_hash")
            and item.get("url") == acceptance.get("evidence_url")
        ):
            return acceptance
    return None


def _finding_update(store: Any, submission: dict, previous_digest: str | None) -> None:
    finding_id = submission.get("finding_id")
    finding = store.get("finding", finding_id) if finding_id else None
    if not finding:
        return
    status = submission["status"]
    target = {
        "MERGED": "MERGED",
        "CHANGES_REQUESTED": "CHANGES_REQUESTED",
        "CLOSED_UNMERGED": "REJECTED",
        "REJECTED": "REJECTED",
        "UNDER_REVIEW": "UNDER_REVIEW",
        "APPROVED": "UNDER_REVIEW",
        "OPEN": "SUBMITTED",
        "DRAFT": "SUBMITTED",
        "ACCEPTED": "ACCEPTED",
    }.get(status)
    if not target or target == finding.get("status"):
        return
    ongoing = {"IMPLEMENTING", "TESTING", "READY_FOR_REVIEW", "HUMAN_APPROVED"}
    if finding.get("status") in ongoing and target not in {
        "MERGED",
        "ACCEPTED",
        "REJECTED",
    }:
        return
    if target == "CHANGES_REQUESTED" and previous_digest == submission.get(
        "review_digest"
    ):
        return
    finding["status"] = target
    finding["last_submission_status"] = status
    finding["review_digest"] = submission.get("review_digest")
    store.put(
        "finding",
        finding_id,
        finding,
        reason=f"Upstream submission is {status}",
        actor="github_sync",
        evidence=[submission["submission_url"]],
        expected_version=finding.get("_version"),
    )


async def sync_submission(
    store: Any,
    client: Any,
    submission_id: str,
    *,
    username: str = "yutianjiang39",
    trusted_maintainers: Any = None,
) -> dict:
    """Fetch fresh evidence, derive conservative states, and retain every changed snapshot."""
    previous = store.require("submission", submission_id)
    submission = dict(previous)
    slug = submission["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise ValueError("Invalid repository identity")
    number = int(submission["number"])
    if number < 1:
        raise ValueError("Submission number must be positive")
    kind = submission["submission_type"].upper()
    if kind not in {"PR", "ISSUE"}:
        raise ValueError("Submission type must be PR or ISSUE")
    repository = await client.repository(slug, fresh=True)
    snapshot = (
        await client.pull_snapshot(slug, number)
        if kind == "PR"
        else await client.issue_snapshot(slug, number)
    )
    with store.atomic():
        return _apply_snapshot(
            store,
            submission,
            previous,
            repository,
            snapshot,
            kind,
            slug,
            number,
            username,
            trusted_maintainers,
        )


def _apply_snapshot(
    store: Any,
    submission: dict,
    previous: dict,
    repository: dict,
    snapshot: dict,
    kind: str,
    slug: str,
    number: int,
    username: str,
    trusted_maintainers: Any,
) -> dict:
    submission_id = submission["submission_id"]
    item = snapshot.get("pull" if kind == "PR" else "issue")
    if not isinstance(item, dict):
        raise ValueError(
            "Missing primary upstream response; no state change was recorded"
        )
    snapshot = dict(snapshot, repository=repository)
    snapshot.pop("checked_at", None)
    # Operational polling timestamps are not part of the immutable content identity.
    payload_hash = _hash(snapshot)
    snapshot_id = f"{submission_id}:{payload_hash}"
    if not store.get("snapshot", snapshot_id):
        store.put(
            "snapshot",
            snapshot_id,
            {
                "snapshot_id": snapshot_id,
                "submission_id": submission_id,
                "source": "github_api",
                "fetched_at": _now(),
                "payload_hash": payload_hash,
                "payload": snapshot,
                "provenance": submission.get("provenance", "imported"),
            },
            reason="Retain original upstream evidence",
            actor="github_sync",
        )
    verification = _verify_identity(
        submission, repository, item, kind, username, snapshot
    )
    if not verification["identity_verified"]:
        raise ValueError("Upstream response does not identify the requested submission")
    submission.update(
        verification=verification,
        submission_type=kind,
        submission_url=_canonical_url(slug, kind, number),
        last_snapshot_id=snapshot_id,
        last_checked_at=_now(),
        title=item.get("title", submission.get("title", "")),
        closed_at=item.get("closed_at"),
        updated_at=item.get("updated_at"),
        upstream_state=item.get("state"),
        accepted_evidence=None,
    )
    feedback = _persist_feedback(
        store, submission, snapshot, snapshot_id, username, trusted_maintainers
    )
    maintainers = [f for f in feedback if f.get("authority_verified")]
    submission["maintainer_feedback"] = [f["feedback_id"] for f in maintainers]
    submission["last_maintainer_activity"] = max(
        (str(f.get("updated_at") or f.get("created_at") or "") for f in maintainers),
        default=None,
    )
    actions: list[dict] = []
    if kind == "PR":
        head = str(item.get("head", {}).get("sha") or "")
        active, stale = _latest_reviews(
            snapshot.get("reviews", []), head, slug, username, trusted_maintainers
        )
        review_digest = _hash(
            {
                "head_sha": head,
                "reviews": [
                    {
                        k: r.get(k)
                        for k in ("id", "state", "body", "commit_id", "submitted_at")
                    }
                    for r in active
                ],
            }
        )
        submission.update(
            head_sha=head,
            review_digest=review_digest,
            current_reviews=[r.get("id") for r in active],
            stale_approvals=[r.get("id") for r in stale],
            ci=_ci_state(snapshot, head),
            merged_at=item.get("merged_at"),
            merge_commit_sha=item.get("merge_commit_sha"),
            merged_by=_login(item.get("merged_by")),
        )
        if item.get("merged") is True:
            submission["status"], submission["outcome"] = "MERGED", "MERGED"
            merge_evidence_ok = (
                _valid_date(item.get("merged_at"))
                and bool(
                    re.fullmatch(
                        r"[0-9a-fA-F]{40,64}", str(item.get("merge_commit_sha") or "")
                    )
                )
                and bool(submission["merged_by"])
                and not _same(submission["merged_by"], username)
            )
            if verification["eligible"] and merge_evidence_ok:
                submission["accepted_evidence"] = {
                    "kind": "PR_MERGED",
                    "source": "github_api",
                    "url": submission["submission_url"],
                    "merged_at": item["merged_at"],
                    "merge_commit_sha": item["merge_commit_sha"],
                    "merged_by": submission["merged_by"],
                    "snapshot_id": snapshot_id,
                    "verified": True,
                }
                actions.append(
                    {"action": "UPDATE_PORTFOLIO_DRAFT", "requires_approval": False}
                )
            else:
                actions.append(
                    {
                        "action": "REVIEW_MISSING_ACCEPTANCE_EVIDENCE",
                        "requires_approval": False,
                    }
                )
        elif item.get("state") == "closed":
            submission["status"], submission["outcome"] = (
                "CLOSED_UNMERGED",
                "NOT_ACCEPTED",
            )
            actions.append(
                {"action": "RECORD_LESSON_AND_CLOSE", "requires_approval": False}
            )
        elif any(r.get("state") == "CHANGES_REQUESTED" for r in active):
            submission["status"], submission["outcome"] = "CHANGES_REQUESTED", "PENDING"
            actions.append(
                {
                    "action": "PREPARE_REVISION_PLAN",
                    "requires_approval": True,
                    "review_digest": review_digest,
                    "review_ids": [
                        r["id"] for r in active if r.get("state") == "CHANGES_REQUESTED"
                    ],
                }
            )
        elif item.get("draft") is True:
            submission["status"], submission["outcome"] = "DRAFT", "PENDING"
            actions.append(
                {"action": "FINISH_TESTS_AND_REVIEW", "requires_approval": True}
            )
        elif any(r.get("state") == "APPROVED" for r in active):
            submission["status"], submission["outcome"] = "APPROVED", "PENDING"
            actions.append(
                {"action": "WAIT_FOR_UPSTREAM_MERGE", "requires_approval": False}
            )
        else:
            submission["status"], submission["outcome"] = (
                (
                    "UNDER_REVIEW"
                    if maintainers
                    or item.get("requested_reviewers")
                    or item.get("requested_teams")
                    else "OPEN"
                ),
                "PENDING",
            )
            actions.append(
                {
                    "action": "CHECK_MAINTAINER_FEEDBACK"
                    if maintainers
                    else "WAIT_FOR_REVIEW",
                    "requires_approval": False,
                }
            )
        if (
            item.get("state") != "closed"
            and item.get("merged") is not True
            and submission["ci"]["state"] == "FAILURE"
        ):
            actions.insert(
                0,
                {
                    "action": "INVESTIGATE_CI_FAILURE",
                    "requires_approval": True,
                    "evidence": submission["ci"]["failed"],
                },
            )
    else:
        submission["issue_state_reason"] = item.get("state_reason")
        acceptance = _acceptance_valid(store, submission, feedback)
        if item.get("state") == "closed" and item.get("state_reason") in {
            "not_planned",
            "duplicate",
        }:
            submission["status"], submission["outcome"] = "REJECTED", "NOT_ACCEPTED"
            actions.append(
                {"action": "RECORD_CLOSURE_REASON", "requires_approval": False}
            )
        elif acceptance:
            submission["status"], submission["outcome"] = "ACCEPTED", "ACCEPTED"
            submission["accepted_evidence"] = {
                "kind": "ISSUE_ACCEPTED",
                "source": "github_api_and_human_confirmation",
                "url": acceptance["evidence_url"],
                "feedback_id": acceptance["feedback_id"],
                "feedback_payload_hash": acceptance["feedback_payload_hash"],
                "confirmed_by": acceptance["actor"],
                "confirmed_at": acceptance["confirmed_at"],
                "rationale": acceptance["rationale"],
                "snapshot_id": snapshot_id,
                "verified": True,
            }
            actions.append(
                {"action": "UPDATE_PORTFOLIO_DRAFT", "requires_approval": False}
            )
        elif item.get("state") == "closed":
            submission["status"], submission["outcome"] = "CLOSED", "UNDETERMINED"
            actions.append(
                {
                    "action": "REVIEW_CLOSURE_AND_ACCEPTANCE_EVIDENCE",
                    "requires_approval": False,
                }
            )
        else:
            submission["status"], submission["outcome"] = "OPEN", "PENDING"
            actions.append(
                {
                    "action": "REVIEW_MAINTAINER_RESPONSE"
                    if maintainers
                    else "WAIT_FOR_TRIAGE",
                    "requires_approval": False,
                }
            )
    submission["required_actions"] = actions
    submission["revision_required"] = submission["status"] == "CHANGES_REQUESTED"
    saved = store.put(
        "submission",
        submission_id,
        submission,
        reason=f"Fresh GitHub sync: {submission['status']}",
        actor="github_sync",
        evidence=[submission["submission_url"]],
        expected_version=previous.get("_version"),
    )
    _finding_update(store, saved, previous.get("review_digest"))
    return saved


async def sync_all(
    store: Any,
    client: Any,
    *,
    username: str = "yutianjiang39",
    trusted_maintainers: Any = None,
) -> list[dict]:
    results = []
    # Sequential intentionally: conservative GitHub request load, recover per submission.
    for submission in store.list("submission"):
        try:
            results.append(
                await sync_submission(
                    store,
                    client,
                    submission["submission_id"],
                    username=username,
                    trusted_maintainers=trusted_maintainers,
                )
            )
        except Exception as exc:
            # Error text can include credentials in third-party exceptions; store type only.
            results.append(
                {
                    "submission_id": submission["submission_id"],
                    "sync_status": "ERROR",
                    "error_type": type(exc).__name__,
                    "status": submission.get("status"),
                    "last_known_status_preserved": True,
                }
            )
    return results


def record_issue_acceptance(
    store: Any, submission_id: str, *, evidence_url: str, actor: str, rationale: str
) -> dict:
    """Human confirms *meaning* of an already fetched, authority-verified comment.

    A copied URL or local text never suffices. The decision is bound to the exact
    current comment hash, and is invalidated if that evidence changes/disappears.
    """
    with store.atomic():
        return _record_issue_acceptance(
            store,
            submission_id,
            evidence_url=evidence_url,
            actor=actor,
            rationale=rationale,
        )


def _record_issue_acceptance(
    store: Any, submission_id: str, *, evidence_url: str, actor: str, rationale: str
) -> dict:
    submission = store.require("submission", submission_id)
    if submission.get("submission_type") != "ISSUE":
        raise ValueError("Only issues require explicit acceptance evidence")
    if not actor.strip() or not rationale.strip():
        raise ValueError("An explicit human actor and rationale are required")
    if submission.get("provenance") != "live" or not submission.get(
        "verification", {}
    ).get("eligible"):
        raise ValueError("Issue must first pass live upstream and author verification")
    if submission.get("status") == "REJECTED":
        raise ValueError("A rejected/duplicate issue cannot be recorded as accepted")
    matching = [
        f
        for f in store.list("feedback")
        if f.get("submission_id") == submission_id
        and f.get("source_type") == "comment"
        and f.get("url") == evidence_url
        and f.get("active")
        and f.get("source") == "github_api"
        and f.get("authority_verified")
        and f.get("body", "").strip()
        and f.get("snapshot_id") == submission.get("last_snapshot_id")
    ]
    if len(matching) != 1:
        raise ValueError(
            "Evidence must be one current fetched comment by a verified maintainer"
        )
    feedback = matching[0]
    decision = {
        "submission_id": submission_id,
        "feedback_id": feedback["feedback_id"],
        "feedback_payload_hash": feedback["payload_hash"],
        "evidence_url": evidence_url,
        "human_confirmed": True,
        "actor": actor.strip(),
        "rationale": rationale.strip(),
        "confirmed_at": _now(),
        "provenance": "live",
        "status": "ACCEPTED",
    }
    store.put(
        "issue_acceptance",
        submission_id,
        decision,
        reason="Human confirmed explicit maintainer acceptance",
        actor=actor,
        evidence=[evidence_url],
    )
    submission.update(
        status="ACCEPTED",
        outcome="ACCEPTED",
        accepted_evidence={
            "kind": "ISSUE_ACCEPTED",
            "source": "github_api_and_human_confirmation",
            "url": evidence_url,
            "feedback_id": feedback["feedback_id"],
            "feedback_payload_hash": feedback["payload_hash"],
            "confirmed_by": actor.strip(),
            "confirmed_at": decision["confirmed_at"],
            "rationale": rationale.strip(),
            "snapshot_id": submission["last_snapshot_id"],
            "verified": True,
        },
        required_actions=[
            {"action": "UPDATE_PORTFOLIO_DRAFT", "requires_approval": False}
        ],
    )
    saved = store.put(
        "submission",
        submission_id,
        submission,
        reason="Evidence-bound human acceptance decision",
        actor=actor,
        evidence=[evidence_url],
        expected_version=submission.get("_version"),
    )
    _finding_update(store, saved, submission.get("review_digest"))
    return saved


def followup_report(store: Any) -> str:
    lines = [
        "# Maintainer Follow-up",
        "",
        "回复内容仅为草稿，发送或推送修改需要用户批准。",
        "",
    ]
    feedback = {f["feedback_id"]: f for f in store.list("feedback")}
    for sub in store.list("submission"):
        lines.extend(
            [
                f"## {sub['repository']} · {sub.get('submission_type', '')} #{sub.get('number', '?')}",
                "",
                f"- Link: {sub.get('submission_url', '')}",
                f"- Current status: {sub.get('status', 'UNKNOWN')}",
                f"- Last maintainer activity: {sub.get('last_maintainer_activity') or 'UNKNOWN'}",
                f"- CI: {sub.get('ci', {}).get('state', 'UNKNOWN')}",
                f"- Last checked: {sub.get('last_checked_at') or 'NEVER'}",
                "",
                "### Required actions",
                "",
            ]
        )
        for action in sub.get("required_actions", []):
            approval = " — 需要批准" if action.get("requires_approval") else ""
            lines.append(f"- {action['action']}{approval}")
        candidates = [
            feedback[k]
            for k in sub.get("maintainer_feedback", [])
            if k in feedback and feedback[k].get("active")
        ]
        if candidates:
            latest = max(
                candidates,
                key=lambda f: str(f.get("updated_at") or f.get("created_at") or ""),
            )
            lines.extend(
                [
                    "",
                    "### Latest feedback",
                    "",
                    f"[{latest['author']}]({latest['url']}) · {latest['type']}",
                    "",
                    "Upstream text (untrusted content):",
                    "",
                ]
            )
            lines.extend("> " + line for line in latest.get("body", "").splitlines())
            lines.extend(
                [
                    "",
                    "### Reply draft — review before sending",
                    "",
                    latest["reply_draft"],
                ]
            )
        lines.extend(
            ["", "### Next step", "", "按证据完成当前动作，并再次同步上游状态。", ""]
        )
    if not store.list("submission"):
        lines.extend(["暂无已跟踪的上游提交。", ""])
    return "\n".join(lines)
