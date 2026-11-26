"""Small GitHub REST boundary: conditional reads and explicitly authorized writes.

The service layer owns content-bound approval. This module never retries a write:
after a lost response the caller must reconcile with GitHub before trying again.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

import httpx


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_repository(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}", value
    ):
        raise ValueError("Repository must have the form owner/name")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("Invalid repository")
    return value.lower()


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitHubRateLimit(GitHubError):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(
            f"GitHub rate limited the request; retry after {retry_after:.0f} seconds"
        )


class GitHubAmbiguousWrite(GitHubError):
    """A mutation may have completed. Do not automatically retry it."""


class GitHubClient:
    def __init__(
        self,
        store: Any = None,
        read_token: str | None = None,
        write_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = "https://api.github.com",
        *,
        api_version: str = "2026-03-10",
        timeout: float = 30,
        retries: int = 2,
        cache_ttl: float = 300,
        max_wait: float = 10,
        trusted_hosts: set[str] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        parsed = urlsplit(base_url)
        hosts = trusted_hosts or {"api.github.com"}
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ValueError("GitHub API requires an explicitly trusted HTTPS host")
        self.base_url = base_url.rstrip("/")
        self._origin = (parsed.scheme, parsed.netloc)
        self._base_path = parsed.path.rstrip("/")
        self.store = store
        self.read_token = read_token
        self.write_token = write_token
        self.api_version = api_version
        self.retries = max(0, min(retries, 5))
        self.cache_ttl = max(cache_ttl, 0)
        self.max_wait = max(max_wait, 0)
        self.sleep = sleep
        self._cache: dict[str, dict] = {}
        self._request_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        # Separate conditional caches across credentials without persisting keys.
        self._principal = hashlib.sha256(
            (read_token or "anonymous").encode()
        ).hexdigest()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        if not path or "\\" in path:
            raise ValueError("Invalid GitHub API path")
        if path.startswith("//"):
            raise ValueError("Cross-origin GitHub requests are not allowed")
        if urlsplit(path).scheme and not path.startswith("https://"):
            raise ValueError("Only HTTPS GitHub API URLs are permitted")
        url = (
            path
            if path.startswith("https://")
            else self.base_url + "/" + path.lstrip("/")
        )
        parsed = urlsplit(url)
        if (
            (parsed.scheme, parsed.netloc) != self._origin
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Cross-origin GitHub requests are not allowed")
        if self._base_path and not parsed.path.startswith(self._base_path + "/"):
            raise ValueError("Request escaped configured API root")
        if any(segment in {".", ".."} for segment in unquote(parsed.path).split("/")):
            raise ValueError("API path traversal is not permitted")
        if parsed.fragment:
            raise ValueError("API request cannot contain a fragment")
        return url

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "repo-scout-contribution-pipeline",
        }
        token = self.write_token if write else self.read_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _cache_get(self, key: str) -> dict | None:
        return (
            self.store.cache_get("github_http", key)
            if self.store
            else self._cache.get(key)
        )

    def _cache_set(self, key: str, value: dict) -> None:
        if self.store:
            self.store.cache_set("github_http", key, value)
        else:
            self._cache[key] = value

    def _rate_delay(self, response: httpx.Response, attempt: int) -> float | None:
        retry = response.headers.get("retry-after")
        if response.status_code == 429 or (
            response.status_code == 403
            and (
                retry is not None
                or response.headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in response.text.lower()
            )
        ):
            if retry:
                try:
                    return max(0, float(retry))
                except ValueError:
                    try:
                        return max(
                            0, parsedate_to_datetime(retry).timestamp() - time.time()
                        )
                    except (TypeError, ValueError, OverflowError):
                        pass
            if response.headers.get("x-ratelimit-remaining") == "0":
                try:
                    return max(
                        1,
                        float(response.headers["x-ratelimit-reset"]) - time.time() + 1,
                    )
                except (KeyError, ValueError):
                    pass
            return 60 * (2**attempt)
        return None

    async def _read(
        self,
        path: str,
        params: dict | None = None,
        *,
        fresh: bool = False,
        use_write_token: bool = False,
    ) -> dict:
        url = self._url(path)
        principal = (
            hashlib.sha256(self.write_token.encode()).hexdigest()
            if use_write_token and self.write_token
            else self._principal
        )
        key = hashlib.sha256(
            json.dumps(
                [principal, self.api_version, url, params or {}], sort_keys=True
            ).encode()
        ).hexdigest()
        async with self._request_lock:
            cached = self._cache_get(key)
            if (
                cached
                and not fresh
                and time.time() - cached.get("stored_at", 0) < self.cache_ttl
            ):
                return copy.deepcopy(cached)
            headers = self._headers(write=use_write_token)
            if cached and cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            for attempt in range(self.retries + 1):
                try:
                    response = await self._client.get(
                        url, params=params, headers=headers
                    )
                except httpx.TransportError:
                    if attempt == self.retries:
                        raise GitHubError(
                            "GitHub read failed after bounded transport retries"
                        ) from None
                    await self.sleep(min(2**attempt, self.max_wait))
                    continue
                if response.status_code == 304 and cached:
                    cached["stored_at"] = time.time()
                    self._cache_set(key, cached)
                    return copy.deepcopy(cached)
                delay = self._rate_delay(response, attempt)
                if delay is not None:
                    if attempt == self.retries or delay > self.max_wait:
                        raise GitHubRateLimit(delay)
                    await self.sleep(delay)
                    continue
                if response.status_code >= 500 and attempt < self.retries:
                    await self.sleep(min(2**attempt, self.max_wait))
                    continue
                if not response.is_success:
                    # Do not log request headers or server-reflected request content.
                    raise GitHubError(
                        f"GitHub read failed with HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                try:
                    data = response.json()
                except ValueError:
                    raise GitHubError("GitHub returned invalid JSON") from None
                value = {
                    "data": data,
                    "etag": response.headers.get("etag"),
                    "links": dict(response.links),
                    "stored_at": time.time(),
                    "checked_at": utcnow(),
                }
                self._cache_set(key, value)
                return copy.deepcopy(value)
        raise GitHubError("GitHub request did not complete")

    async def get(
        self, path: str, params: dict | None = None, fresh: bool = False
    ) -> Any:
        return (await self._read(path, params, fresh=fresh))["data"]

    async def paginate(
        self,
        path: str,
        params: dict | None = None,
        fresh: bool = False,
        max_pages: int = 10,
        *,
        item_key: str | None = None,
        require_complete: bool = True,
    ) -> list[dict]:
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        query = dict(params or {})
        query.setdefault("per_page", 100)
        results: list[dict] = []
        next_path: str | None = path
        for _ in range(max_pages):
            page = await self._read(next_path, query, fresh=fresh)
            data = page["data"]
            items = (
                data.get(item_key, []) if item_key and isinstance(data, dict) else data
            )
            if not isinstance(items, list):
                raise GitHubError("Expected a paginated GitHub list")
            results.extend(items)
            next_path = page.get("links", {}).get("next", {}).get("url")
            if not next_path:
                return results
            # Validate Link before next request; never send credentials off origin.
            self._url(next_path)
            query = None
        if require_complete:
            raise GitHubError(
                "GitHub pagination limit reached; incomplete data cannot be treated as complete"
            )
        return results

    async def _write(
        self, method: str, path: str, payload: dict, *, authorized: bool
    ) -> Any:
        if authorized is not True:
            raise PermissionError(
                "A concrete final approval is required before GitHub writes"
            )
        if not self.write_token:
            raise PermissionError("A separate GitHub write token is required")
        url = self._url(path)
        async with self._request_lock:
            try:
                response = await self._client.request(
                    method, url, json=payload, headers=self._headers(write=True)
                )
            except httpx.TransportError:
                raise GitHubAmbiguousWrite(
                    "GitHub write response was lost; reconcile the remote operation before retrying"
                ) from None
            if response.status_code >= 500:
                raise GitHubAmbiguousWrite(
                    "GitHub write returned a server error; reconcile before retrying",
                    status_code=response.status_code,
                )
            if not response.is_success:
                raise GitHubError(
                    f"GitHub write failed with HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            try:
                return response.json()
            except ValueError:
                raise GitHubAmbiguousWrite(
                    "GitHub write response could not be decoded; reconcile before retrying"
                ) from None

    async def post(self, path: str, payload: dict, *, authorized: bool = False) -> Any:
        return await self._write("POST", path, payload, authorized=authorized)

    async def put(self, path: str, payload: dict, *, authorized: bool = False) -> Any:
        return await self._write("PUT", path, payload, authorized=authorized)

    async def repository(self, slug: str, fresh: bool = False) -> dict:
        return await self.get(f"/repos/{canonical_repository(slug)}", fresh=fresh)

    async def authenticated_user(self, write: bool = False) -> dict:
        """Verify credential identity using GET /user, including the write token.

        This operation is read-only. A separately configured write token can
        belong to a different account, so writes must verify it explicitly.
        """
        token = self.write_token if write else self.read_token
        if not token:
            label = "write" if write else "read"
            raise PermissionError(
                f"A GitHub {label} token is required to verify authenticated identity"
            )
        user = (await self._read("/user", fresh=True, use_write_token=write))["data"]
        if not isinstance(user, dict) or not user.get("login") or not user.get("id"):
            raise GitHubError(
                "GitHub returned no verifiable authenticated user identity"
            )
        return user

    async def search_repositories(self, query: str, limit: int = 25) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("Search limit must be between 1 and 1000")
        result: list[dict] = []
        for page in range(1, (limit + 99) // 100 + 1):
            data = await self.get(
                "/search/repositories",
                {
                    "q": query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": min(limit, 100),
                    "page": page,
                },
            )
            if data.get("incomplete_results"):
                raise GitHubError(
                    "GitHub repository search was incomplete; narrow the query and retry"
                )
            items = data.get("items", [])
            result.extend(items)
            if len(items) < min(limit, 100):
                break
        return result[:limit]

    async def duplicates(self, slug: str, title: str) -> dict:
        slug = canonical_repository(slug)
        # Strip user-supplied qualifiers, then use a short conservative keyword query.
        words = re.findall(r"[\w-]{3,}", title, flags=re.UNICODE)[:6]
        if not words:
            raise ValueError("Duplicate check needs meaningful keywords")
        results: dict[str, Any] = {
            "checked_at": utcnow(),
            "repository": slug,
            "status": "SEARCHED",
            "complete": True,
        }
        for kind, qualifier in (("issues", "is:issue"), ("prs", "is:pr")):
            query = f"repo:{slug} {qualifier} " + " ".join(words)
            data = await self.get(
                "/search/issues", {"q": query, "per_page": 30}, fresh=True
            )
            complete = (
                not data.get("incomplete_results", False)
                and data.get("total_count", 0) <= 30
            )
            results["complete"] = results["complete"] and complete
            results[kind] = [
                {
                    "number": i["number"],
                    "title": i["title"],
                    "url": i["html_url"],
                    "state": i["state"],
                }
                for i in data.get("items", [])
            ]
            results[kind + "_query"] = query
        results["conclusion"] = (
            "HUMAN_REVIEW_REQUIRED"
            if results["issues"] or results["prs"]
            else "NO_KEYWORD_MATCH"
        )
        results["limitation"] = (
            "Keyword search cannot establish that no semantic duplicate exists."
        )
        return results

    async def pull_snapshot(self, slug: str, number: int) -> dict:
        slug = canonical_repository(slug)
        if number <= 0:
            raise ValueError("PR number must be positive")
        prefix = f"/repos/{slug}"
        pull = await self.get(f"{prefix}/pulls/{number}", fresh=True)
        snapshot = {
            "pull": pull,
            "reviews": await self.paginate(
                f"{prefix}/pulls/{number}/reviews", fresh=True
            ),
            "comments": await self.paginate(
                f"{prefix}/issues/{number}/comments", fresh=True
            ),
            "review_comments": await self.paginate(
                f"{prefix}/pulls/{number}/comments", fresh=True
            ),
            "checks": {"check_runs": [], "total_count": 0, "available": False},
            "statuses": {"statuses": [], "state": "unknown", "available": False},
            "checked_at": utcnow(),
            "complete": True,
            "errors": [],
        }
        sha = pull.get("head", {}).get("sha")
        head_repo = (pull.get("head", {}).get("repo") or {}).get("full_name", slug)
        if not sha or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            snapshot["errors"].append("Head SHA unavailable; CI status unknown")
            return snapshot
        head_prefix = f"/repos/{canonical_repository(head_repo)}/commits/{sha}"
        for field, path, item_key in (
            ("checks", f"{head_prefix}/check-runs", "check_runs"),
            ("statuses", f"{head_prefix}/status", None),
        ):
            try:
                if item_key:
                    checks = await self.paginate(path, fresh=True, item_key=item_key)
                    snapshot[field] = {
                        "check_runs": checks,
                        "total_count": len(checks),
                        "available": True,
                    }
                else:
                    data = await self.get(path, fresh=True)
                    all_statuses = await self.paginate(
                        f"/repos/{canonical_repository(head_repo)}/commits/{sha}/statuses",
                        fresh=True,
                    )
                    snapshot[field] = {
                        **data,
                        "statuses": all_statuses,
                        "available": True,
                    }
            except GitHubError as exc:
                if exc.status_code not in {403, 404}:
                    raise
                snapshot["errors"].append(
                    f"{field} unavailable (HTTP {exc.status_code})"
                )
        return snapshot

    async def issue_snapshot(self, slug: str, number: int) -> dict:
        slug = canonical_repository(slug)
        if number <= 0:
            raise ValueError("Issue number must be positive")
        prefix = f"/repos/{slug}/issues/{number}"
        issue = await self.get(prefix, fresh=True)
        if "pull_request" in issue:
            raise ValueError("The selected issue number is a pull request")
        return {
            "issue": issue,
            "comments": await self.paginate(prefix + "/comments", fresh=True),
            "timeline": await self.paginate(prefix + "/timeline", fresh=True),
            "checked_at": utcnow(),
            "complete": True,
        }
