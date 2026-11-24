"""Evidence-first discovery and bounded maintainer sampling.

Scores describe observed signals. Missing openness evidence stays UNKNOWN and
cannot turn an otherwise interesting repository into an automatic rejection.
"""
from __future__ import annotations

import csv
import io
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .github import GitHubClient, GitHubError, canonical_repository, utcnow


DEFAULT_CATEGORIES = [
    {"name": "Hyperliquid Ecosystem", "query": "hyperliquid archived:false fork:false", "weight": 40},
    {"name": "Independent Trading Infrastructure", "query": "trading websocket archived:false fork:false", "weight": 28},
    {"name": "Quantitative & Data Analytics", "query": "portfolio analytics archived:false fork:false", "weight": 20},
    {"name": "Rust / Python Engineering Tools", "query": "async language:Rust archived:false fork:false", "weight": 12},
]
DEFAULT_LICENSES = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0",
    "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0",
    "GPL-2.0-only", "GPL-3.0-only", "LGPL-3.0-only", "AGPL-3.0-only",
    "Unlicense", "CC0-1.0", "0BSD", "BSL-1.0", "Zlib",
}


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _json(path: Path, data: Any) -> None:
    _write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    out = io.StringIO()
    writer = csv.DictWriter(out, columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for column in columns:
            value = row.get(column, "")
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                value = "'" + value
            safe[column] = value
        writer.writerow(safe)
    _write(path, out.getvalue())


def _allocation(limit: int, categories: list[dict]) -> list[int]:
    if not categories or limit < 1:
        raise ValueError("Search requires categories and a positive candidate limit")
    weights = [float(c.get("weight", 1)) for c in categories]
    if any(not math.isfinite(w) or w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("Search category weights must be finite, nonnegative and sum above zero")
    values = [limit * weight / sum(weights) for weight in weights]
    quotas = [int(v) for v in values]
    for index in sorted(range(len(values)), key=lambda i: values[i] - quotas[i], reverse=True)[:limit - sum(quotas)]:
        quotas[index] += 1
    return quotas


def normalize_repository(raw: dict, *, category: str | None = None) -> dict:
    slug = canonical_repository(raw["full_name"])
    owner = raw.get("owner") or {}
    license_data = raw.get("license") or {}
    parent = raw.get("parent") or {}
    return {
        "repository": slug, "github_id": raw.get("id"),
        "html_url": raw.get("html_url", f"https://github.com/{slug}"),
        "owner": owner.get("login", slug.split("/")[0]), "owner_type": owner.get("type"),
        "fork": bool(raw.get("fork")), "parent": parent.get("full_name"),
        "default_branch": raw.get("default_branch"), "head_sha": None,
        "license": license_data.get("spdx_id"), "license_url": license_data.get("url"),
        "stars": raw.get("stargazers_count"), "forks": raw.get("forks_count"),
        "size_kb": raw.get("size"), "language": raw.get("language"),
        "topics": raw.get("topics") or [], "description": raw.get("description") or "",
        "archived": bool(raw.get("archived")), "disabled": bool(raw.get("disabled")),
        "private": bool(raw.get("private")), "pushed_at": raw.get("pushed_at"),
        "updated_at": raw.get("updated_at"), "created_at": raw.get("created_at"),
        "open_issues_count": raw.get("open_issues_count"),
        "categories": [category] if category else [], "status": "DISCOVERED",
        "provenance": "live", "discovered_at": utcnow(), "observed_at": utcnow(),
        "evidence": [{"url": raw.get("html_url", f"https://github.com/{slug}"), "source": "GitHub REST", "observed_at": utcnow()}],
    }


async def discover(client: GitHubClient, store: Any, config: dict, output: Path) -> dict:
    search = config.get("search", {})
    categories = search.get("categories", DEFAULT_CATEGORIES)
    limit = int(search.get("limit", 25))
    allocations = _allocation(limit, categories)
    found: dict[str, dict] = {}
    queries: list[dict] = []
    for category, quota in zip(categories, allocations):
        if not quota:
            continue
        query = str(category["query"])
        items = await client.search_repositories(query, limit=quota)
        queries.append({"query": query, "category": category["name"], "quota": quota, "returned": len(items)})
        for raw in items:
            record = normalize_repository(raw, category=category["name"])
            key = record["repository"]
            prior = found.get(key) or store.get("repository", key)
            if prior:
                categories_seen = sorted(set(prior.get("categories", []) + record["categories"]))
                # Refresh public metrics while preserving approved state and workflow data.
                record = {**prior, **record, "status": prior.get("status", "DISCOVERED"),
                          "head_sha": prior.get("head_sha"), "categories": categories_seen,
                          "discovered_at": prior.get("discovered_at", record["discovered_at"])}
            found[key] = record
    for key, record in found.items():
        store.put("repository", key, record, reason="GitHub repository search refreshed", actor="github:read", evidence=record["evidence"])
    records = list(found.values())
    _json(output / "raw_candidates.json", records)
    manifest = {"discovered": len(records), "requested": limit, "queries": queries, "observed_at": utcnow(),
                "note": "Category allocations are search budgets, not quotas to be filled with low-quality projects."}
    _json(output / "discovery_manifest.json", manifest)
    return manifest


def screen(store: Any, config: dict, output: Path) -> dict:
    policy = config.get("screening", {})
    allowed = set(policy.get("allowed_licenses", DEFAULT_LICENSES))
    max_idle = int(policy.get("max_inactive_days", 180))
    username = config.get("username", config.get("contribution", {}).get("username", "yutianjiang39"))
    now = datetime.now(timezone.utc)
    shortlisted: list[dict] = []
    excluded: list[dict] = []
    for record in store.list("repository"):
        reasons: list[str] = []
        unknown: list[str] = []
        for flag in ("archived", "disabled", "private", "fork"):
            if record.get(flag):
                reasons.append(flag.upper())
        if record.get("owner", "").casefold() == username.casefold():
            reasons.append("OWN_REPOSITORY")
        license_id = record.get("license")
        if license_id in {None, "", "NOASSERTION", "OTHER"}:
            if not policy.get('allow_unknown_license', False):
                unknown.append("LICENSE_REVIEW_REQUIRED")
        elif license_id not in allowed:
            reasons.append("LICENSE_OUTSIDE_CONFIGURED_ALLOWLIST")
        if record.get('size_kb') is not None and record['size_kb'] > policy.get('maximum_size_kb', 50000):
            reasons.append('SIZE_LIMIT')
        pushed = _date(record.get("pushed_at"))
        age = (now - pushed).total_seconds() / 86400 if pushed else None
        if age is None:
            unknown.append("ACTIVITY_UNKNOWN")
        elif age > max_idle:
            reasons.append("INACTIVE")
        outcome = "EXCLUDED" if reasons else "NEEDS_DATA" if unknown else "SCREENED"
        if outcome == "SCREENED" and record.get("status") == "APPROVED":
            outcome = "APPROVED"
        record.update(status=outcome, screening={
            "status": outcome, "reasons": reasons, "unknown": unknown,
            "last_push_age_days": round(age, 1) if age is not None else None,
            "checked_at": utcnow(), "license": license_id,
            "evidence_url": record.get("html_url"),
        })
        saved = store.put("repository", record["repository"], record, reason="Repository screening", actor="screening", evidence=[record["screening"]])
        if not reasons and not unknown:
            shortlisted.append(saved)
        else:
            excluded.append({**saved, "exclusion_reasons": reasons + unknown})
    _csv(output / "shortlist.csv", shortlisted, ["repository", "html_url", "license", "stars", "forks", "language", "pushed_at", "status"])
    _csv(output / "excluded_repos.csv", excluded, ["repository", "status", "exclusion_reasons", "html_url"])
    return {"shortlisted": len(shortlisted), "excluded_or_needs_data": len(excluded)}


def is_maintainer(item: dict, owner: str, trusted: set[str] | None = None) -> bool:
    login = ((item.get("user") or item.get("actor") or {}).get("login") or "").casefold()
    if not login:
        return False
    # COLLABORATOR and MEMBER alone do not establish the repository's maintenance
    # policy/role. A user-confirmed trusted list may establish those identities.
    return login == owner.casefold() or login in {x.casefold() for x in (trusted or set())} or item.get("author_association") == "OWNER"


def _external(pr: dict, owner: str, trusted: set[str]) -> bool | None:
    user = pr.get('user') or {}
    if not user.get('login'):
        return None
    if user.get('type') == 'Bot' or user['login'].lower().endswith('[bot]'):
        return False
    if is_maintainer(pr, owner, trusted):
        return False
    if pr.get("author_association") in {"NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "CONTRIBUTOR"}:
        return True
    # Org MEMBER is not proof of a maintenance role or an external author.
    return None


async def _maintainer_sample(client: GitHubClient, record: dict, settings: dict) -> dict:
    slug = record["repository"]
    prefix = f"/repos/{slug}"
    owner = record["owner"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(settings.get("window_days", 180)))
    limit = max(1, min(int(settings.get("max_prs", 10)), 30))
    trusted = set(settings.get("trusted_maintainers", {}).get(slug, []))
    # Bounded sampling is intentionally not presented as a census.
    pulls = await client.paginate(prefix + "/pulls", {"state": "all", "sort": "updated", "direction": "desc"}, max_pages=2, require_complete=False)
    recent = [p for p in pulls if (_date(p.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
    external = [p for p in recent if _external(p, owner, trusted) is True][:limit]
    unknown_identity = sum(_external(p, owner, trusted) is None for p in recent)
    merged = closed = replied = substantive = 0
    response_hours: list[float] = []
    evidence: list[dict] = []
    for candidate in external:
        number = int(candidate["number"])
        pr = await client.get(prefix + f"/pulls/{number}")
        merged += bool(pr.get("merged_at"))
        closed += pr.get("state") == "closed" and not pr.get("merged_at")
        reviews = [r for r in await client.paginate(prefix + f"/pulls/{number}/reviews") if r.get("state") != "PENDING"]
        comments = await client.paginate(prefix + f"/issues/{number}/comments")
        inline = await client.paginate(prefix + f"/pulls/{number}/comments")
        maintainer_replies = [c for c in reviews + comments + inline if is_maintainer(c, owner, trusted)]
        replied += bool(maintainer_replies)
        created = _date(pr.get("created_at"))
        timestamps = [_date(c.get("submitted_at") or c.get("created_at")) for c in maintainer_replies]
        timestamps = [t for t in timestamps if t and created and t >= created]
        response = (min(timestamps) - created).total_seconds() / 3600 if timestamps else None
        if response is not None:
            response_hours.append(response)
        technical = any((c.get("path") and c.get("body")) or len((c.get("body") or "").strip()) >= 80 for c in maintainer_replies)
        substantive += technical
        evidence.append({"url": pr["html_url"], "external_author_association": pr.get("author_association"),
                         "merged_at": pr.get("merged_at"), "state": pr.get("state"),
                         "first_response_hours": round(response, 2) if response is not None else None,
                         "maintainer_response_urls": [c.get("html_url") for c in maintainer_replies if c.get("html_url")],
                         "substantive_review_proxy": bool(technical)})
    issues = await client.get(prefix + "/issues", {"state": "all", "per_page": 30, "sort": "updated", "direction": "desc"})
    issue_sample = [i for i in issues if "pull_request" not in i][:max(1, min(int(settings.get("max_issues", 5)), 10))]
    issue_replied = 0
    labels: set[str] = set()
    for issue in issue_sample:
        labels.update(str(label.get("name", "")).lower() for label in issue.get("labels", []) if isinstance(label, dict))
        if issue.get("comments", 0):
            comments = await client.paginate(prefix + f"/issues/{issue['number']}/comments")
            owner_comments = [c for c in comments if is_maintainer(c, owner, trusted)]
            issue_replied += bool(owner_comments)
            evidence.append({"url": issue["html_url"], "maintainer_response_urls": [c.get("html_url") for c in owner_comments if c.get("html_url")]})
    try:
        release = await client.get(prefix + "/releases/latest")
        latest_release = {"tag": release.get("tag_name"), "published_at": release.get("published_at"), "url": release.get("html_url")}
    except GitHubError as exc:
        if exc.status_code != 404:
            raise
        latest_release = None
    count = len(external)
    median_response = statistics.median(response_hours) if response_hours else None
    metrics = {
        "external_prs_sampled": count, "external_prs_merged": merged,
        "external_prs_closed_unmerged": closed, "external_prs_with_maintainer_reply": replied,
        "unknown_author_identity": unknown_identity,
        "first_response_median_hours": round(median_response, 2) if median_response is not None else None,
        "substantive_review_proxy_count": substantive,
        "issues_sampled": len(issue_sample), "issues_with_maintainer_reply": issue_replied,
        "invitation_labels_seen": sorted(labels & {"good first issue", "help wanted"}),
        "stale_open_prs_in_sample": sum(p.get("state") == "open" and (_date(p.get("updated_at")) or cutoff) < datetime.now(timezone.utc) - timedelta(days=30) for p in external),
    }
    openness = None
    status = "UNKNOWN"
    if count >= int(settings.get("min_external_prs", 3)):
        # A closed/merged resolved cohort is meaningful; open pending PRs are not rejections.
        resolved = merged + closed
        acceptance = merged / resolved * 100 if resolved else None
        responsiveness = max(0, 100 * (1 - median_response / (24 * 14))) if median_response is not None else None
        review_proxy = substantive / count * 100
        interaction = issue_replied / len(issue_sample) * 100 if issue_sample else None
        parts = [(acceptance, 35), (responsiveness, 25), (review_proxy, 25), (interaction, 15)]
        known = [(v, w) for v, w in parts if v is not None]
        if known:
            openness = round(sum(v * w for v, w in known) / sum(w for _, w in known), 1)
            status = "OBSERVED" if len(known) == 4 else "PARTIAL"
    return {
        "status": status, "openness_score": openness, "metrics": metrics,
        "latest_release": latest_release, "window_days": int(settings.get("window_days", 180)),
        "sample_limit": limit, "observed_at": utcnow(), "evidence": evidence,
        "limitations": [
            "Bounded recent-activity sample; not the complete PR population.",
            "Review quality uses inline/detail-length signals and is a proxy requiring human judgment.",
            "Unknown identities and insufficient external PR data are not evidence of hostility.",
        ],
    }


async def analyze_maintainers(client: GitHubClient, store: Any, config: dict, output: Path) -> dict:
    settings = dict(config.get("maintainer", {}))
    settings.setdefault("trusted_maintainers", config.get("tracking", {}).get("trusted_maintainers", {}))
    candidates = [r for r in store.list("repository") if r.get("status") not in {"EXCLUDED", "NEEDS_DATA"}]
    candidates = sorted(candidates, key=lambda r: (r.get("pushed_at") or ""), reverse=True)[:int(settings.get("max_repositories", 6))]
    reports = ["# Maintainer analysis", "", "Missing evidence is UNKNOWN. Scores are sampling signals, not judgments of people.", ""]
    analyzed = 0
    failures: list[dict] = []
    for record in candidates:
        try:
            analysis = await _maintainer_sample(client, record, settings)
        except GitHubError as exc:
            analysis = {"status": "UNKNOWN", "openness_score": None, "metrics": {}, "evidence": [], "observed_at": utcnow(), "error": str(exc)}
            failures.append({"repository": record["repository"], "error": str(exc)})
        else:
            analyzed += 1
        record["maintainer"] = analysis
        store.put("repository", record["repository"], record, reason="Bounded maintainer evidence refreshed", actor="github:read", evidence=analysis["evidence"])
        reports += [f"## {record['repository']}", "", f"Openness: **{analysis['status']}**; score: {analysis['openness_score']}", "",
                    "```json", json.dumps(analysis.get("metrics", {}), ensure_ascii=False, indent=2), "```", ""]
        for evidence in analysis["evidence"]:
            reports.append(f"- {evidence['url']}")
        reports.append("")
    _write(output / "maintainer_analysis.md", "\n".join(reports))
    return {"analyzed": analyzed, "failed": failures, "sampled_repositories": len(candidates)}


def score(store: Any, config: dict, output: Path) -> dict:
    settings = config.get("scoring", {})
    policy = config.get('screening', {})
    threshold = settings.get('threshold', 70)
    def preference(value, field, default):
        bounds = policy.get(field, default)
        return 100 if bounds['min'] <= value <= bounds['max'] else 70
    weights = settings.get("weights", {
        "maintainer_openness": 25, "repository_activity": 20, "technical_fit": 20,
        "community_attention": 15, "engineering_opportunities": 10, "contribution_scope": 10,
    })
    expected = {"maintainer_openness", "repository_activity", "technical_fit", "community_attention", "engineering_opportunities", "contribution_scope"}
    if set(weights) != expected or any(not math.isfinite(float(v)) or float(v) < 0 for v in weights.values()) or abs(sum(float(v) for v in weights.values()) - 100) > 0.001:
        raise ValueError("Scoring weights must cover the six dimensions and sum to 100")
    preferred_languages = {str(x).casefold() for x in settings.get("preferred_languages", ["Python", "Rust"])}
    now = datetime.now(timezone.utc)
    records = []
    for record in store.list("repository"):
        if record.get("status") in {"EXCLUDED", "NEEDS_DATA"}:
            continue
        source = record.get("html_url")
        dims: dict[str, dict] = {}
        def dimension(name: str, value: float | None, evidence: Any, caveat: str = "") -> None:
            dims[name] = {"value": round(max(0, min(100, value)), 2) if value is not None else None,
                          "weight": float(weights[name]), "evidence": evidence,
                          "url": source, "caveat": caveat}
        maintainer = record.get("maintainer", {})
        dimension("maintainer_openness", maintainer.get("openness_score"), maintainer.get("evidence", []), maintainer.get("status", "UNKNOWN"))
        pushed = _date(record.get("pushed_at"))
        age = (now - pushed).total_seconds() / 86400 if pushed else None
        activity = 100 if age is not None and age <= 30 else 80 if age is not None and age <= 60 else max(0, 80 - (age - 60) / 1.5) if age is not None else None
        dimension("repository_activity", activity, {"pushed_at": record.get("pushed_at"), "age_days": round(age, 1) if age is not None else None})
        language = record.get("language")
        technical = 100 if language and language.casefold() in preferred_languages else 60 if language else None
        dimension("technical_fit", technical, {"language": language, "categories": record.get("categories", [])}, "Language/category match is an initial fit signal; inspect architecture before approval.")
        stars, forks = record.get("stars"), record.get("forks")
        attention = None
        if stars is not None and forks is not None:
            attention = .7 * preference(stars, 'preferred_stars', {'min':50,'max':1000}) + .3 * preference(forks, 'preferred_forks', {'min':5,'max':200})
        dimension("community_attention", attention, {"stars": stars, "forks": forks}, "Small active projects remain eligible regardless of star count.")
        invitation = maintainer.get("metrics", {}).get("invitation_labels_seen", [])
        manual = record.get("opportunity_assessment")
        opportunity = float(manual["score"]) if isinstance(manual, dict) and manual.get("evidence") and "score" in manual else 70 if invitation else None
        dimension("engineering_opportunities", opportunity, manual or {"invitation_labels_seen": invitation}, "Invitations are not proof of bugs. Unknown means targeted inspection is still needed.")
        size = record.get("size_kb")
        scope = preference(size, 'preferred_size_kb', {'min':100,'max':10000}) if size is not None else None
        if scope is not None and record.get('owner_type') is not None and policy.get('preferred_owner_types', ['User']):
            scope = .95 * scope + (5 if record['owner_type'] in policy.get('preferred_owner_types', ['User']) else 0)
        dimension("contribution_scope", scope, {"repository_size_kb": size, 'owner_type':record.get('owner_type')}, "Size and owner type are scope preferences, not evidence of maintainer openness.")
        observed = sum(d["value"] * d["weight"] / 100 for d in dims.values() if d["value"] is not None)
        unknown_weight = sum(d["weight"] for d in dims.values() if d["value"] is None)
        full = unknown_weight == 0
        estimate = round(observed / (100 - unknown_weight) * 100, 1) if unknown_weight < 100 else None
        ranking = "EVIDENCE_REQUIRED"
        if full:
            ranking = "PRIORITY" if observed >= max(80, threshold) else "CANDIDATE" if observed >= threshold else "OBSERVE"
        result = {"total": round(observed, 1) if full else None, "provisional_estimate": estimate,
                  "range": [round(observed, 1), round(observed + unknown_weight, 1)],
                  "complete": full, "classification": ranking, "dimensions": dims, "scored_at": utcnow()}
        record["score"] = result
        if record.get("status") != "APPROVED":
            record["status"] = ranking
        saved = store.put("repository", record["repository"], record, reason="Evidence-backed repository scoring", actor="scoring", evidence=dims)
        records.append(saved)
    _json(output / "repository_scores.json", records)
    _json(output / "repositories_approved.json", [r for r in store.list("repository") if r.get("status") == "APPROVED"])
    return {"scored": len(records), "complete": sum(r["score"]["complete"] for r in records),
            "needs_evidence": sum(not r["score"]["complete"] for r in records)}
