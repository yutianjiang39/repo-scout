from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from repo_scout.github import (
    GitHubClient,
    GitHubError,
    GitHubRateLimit,
    GitHubAmbiguousWrite,
)
from repo_scout.discovery import (
    discover,
    screen,
    score,
    analyze_maintainers,
    is_maintainer,
    normalize_repository,
)


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.cache = {}

    def put(self, kind, key, data, **kwargs):
        self.data[(kind, key)] = copy.deepcopy(data)
        return copy.deepcopy(data)

    def get(self, kind, key):
        return copy.deepcopy(self.data.get((kind, key)))

    def list(self, kind):
        return [copy.deepcopy(v) for (k, _), v in self.data.items() if k == kind]

    def cache_get(self, namespace, key):
        return copy.deepcopy(self.cache.get((namespace, key)))

    def cache_set(self, namespace, key, value):
        self.cache[(namespace, key)] = copy.deepcopy(value)


def repo(slug="upstream/tool", **overrides):
    value = {
        "full_name": slug,
        "html_url": f"https://github.com/{slug}",
        "owner": {"login": slug.split("/")[0], "type": "User"},
        "license": {"spdx_id": "MIT"},
        "default_branch": "main",
        "stargazers_count": 3,
        "forks_count": 1,
        "size": 500,
        "language": "Python",
        "fork": False,
        "archived": False,
        "pushed_at": datetime.now(timezone.utc).isoformat(),
    }
    value.update(overrides)
    return value


class GitHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_etag_fresh_reads_and_credential_cache_separation(self):
        store = MemoryStore()
        calls = []

        def handler(request):
            calls.append(request)
            if request.headers.get("if-none-match") == '"revision1"':
                return httpx.Response(304)
            return httpx.Response(
                200, json={"value": len(calls)}, headers={"etag": '"revision1"'}
            )

        async with GitHubClient(
            store, read_token="private-read", transport=httpx.MockTransport(handler)
        ) as client:
            self.assertEqual((await client.get("/user"))["value"], 1)
            self.assertEqual((await client.get("/user"))["value"], 1)
            self.assertEqual((await client.get("/user", fresh=True))["value"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].headers["if-none-match"], '"revision1"')
        async with GitHubClient(
            store, transport=httpx.MockTransport(handler)
        ) as anonymous:
            self.assertEqual((await anonymous.get("/user"))["value"], 3)
        self.assertNotIn("private-read", repr(store.cache))

    async def test_transport_and_server_retry_reads(self):
        count = 0
        delays = []

        async def sleep(delay):
            delays.append(delay)

        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                raise httpx.ConnectError("network", request=request)
            if count == 2:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        async with GitHubClient(
            transport=httpx.MockTransport(handler), sleep=sleep
        ) as client:
            self.assertTrue((await client.get("/repos/owner/tool"))["ok"])
        self.assertEqual(count, 3)
        self.assertEqual(delays, [1, 2])

    async def test_rate_limit_does_not_retry_before_server_deadline(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                429, headers={"Retry-After": "300"}, json={"message": "rate limited"}
            )

        async with GitHubClient(
            transport=httpx.MockTransport(handler), max_wait=1
        ) as client:
            with self.assertRaises(GitHubRateLimit) as context:
                await client.get("/rate_limit")
        self.assertEqual(context.exception.retry_after, 300)
        self.assertEqual(len(calls), 1)

    async def test_write_requires_approval_and_separate_token(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(201, json={"number": 7})

        async with GitHubClient(
            read_token="read", transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(PermissionError):
                await client.post("/repos/upstream/tool/issues", {}, authorized=True)
        async with GitHubClient(
            read_token="read",
            write_token="write",
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(PermissionError):
                await client.post("/repos/upstream/tool/issues", {})
            self.assertEqual(
                await client.post(
                    "/repos/upstream/tool/issues", {"title": "test"}, authorized=True
                ),
                {"number": 7},
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["authorization"], "Bearer write")

    async def test_authenticated_user_checks_each_token_with_get_only(self):
        calls = []

        def handler(request):
            calls.append(request)
            token = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "id": 1 if token == "Bearer read" else 2,
                    "login": "reader" if token == "Bearer read" else "writer",
                },
            )

        async with GitHubClient(
            read_token="read",
            write_token="write",
            transport=httpx.MockTransport(handler),
        ) as client:
            self.assertEqual((await client.authenticated_user())["login"], "reader")
            self.assertEqual(
                (await client.authenticated_user(write=True))["login"], "writer"
            )
        self.assertEqual([r.method for r in calls], ["GET", "GET"])
        self.assertEqual([r.url.path for r in calls], ["/user", "/user"])
        self.assertEqual(
            [r.headers["authorization"] for r in calls], ["Bearer read", "Bearer write"]
        )
        async with GitHubClient(
            read_token="read", transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(PermissionError):
                await client.authenticated_user(write=True)
        self.assertEqual(len(calls), 2)

    async def test_ambiguous_write_is_never_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("response lost", request=request)

        async with GitHubClient(
            write_token="write", transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(GitHubAmbiguousWrite):
                await client.post(
                    "/repos/upstream/tool/issues", {"title": "test"}, authorized=True
                )
        self.assertEqual(len(calls), 1)

    async def test_cross_origin_and_redirect_refused(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                302, headers={"location": "https://evil.invalid/collect"}
            )

        async with GitHubClient(
            read_token="secret", transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(ValueError):
                await client.get("https://evil.invalid/data")
            with self.assertRaises(ValueError):
                await client.get("//evil.invalid/data")
            with self.assertRaises(GitHubError):
                await client.get("/redirect")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.host, "api.github.com")

    async def test_pagination_preserves_pages_and_fails_on_truncation(self):
        def handler(request):
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=[{"id": 2}])
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={"link": '<https://api.github.com/items?page=2>; rel="next"'},
            )

        async with GitHubClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await client.paginate("/items"), [{"id": 1}, {"id": 2}])
            with self.assertRaises(GitHubError):
                await client.paginate("/items", max_pages=1)

    async def test_duplicate_search_never_claims_semantic_absence(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200, json={"items": [], "total_count": 0, "incomplete_results": False}
            )

        async with GitHubClient(transport=httpx.MockTransport(handler)) as client:
            result = await client.duplicates(
                "Owner/Tool", "websocket misses reconnection events"
            )
        self.assertEqual(result["conclusion"], "NO_KEYWORD_MATCH")
        self.assertEqual(len(requests), 2)
        self.assertIn("repo:owner/tool is:issue", requests[0].url.params["q"])
        self.assertIn("cannot establish", result["limitation"])

    async def test_incomplete_search_cannot_be_success(self):
        async with GitHubClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"items": [], "incomplete_results": True}
                )
            )
        ) as client:
            with self.assertRaises(GitHubError):
                await client.search_repositories("hyperliquid")


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_deduplicates_case_and_preserves_approval(self):
        store = MemoryStore()
        existing = normalize_repository(repo())
        existing["status"] = "APPROVED"
        store.put("repository", "upstream/tool", existing)

        class Client:
            async def search_repositories(self, query, limit):
                return [repo("Upstream/Tool" if query == "a" else "upstream/tool")]

        with tempfile.TemporaryDirectory() as temp:
            result = await discover(
                Client(),
                store,
                {
                    "search": {
                        "limit": 2,
                        "categories": [
                            {"name": "A", "query": "a", "weight": 1},
                            {"name": "B", "query": "b", "weight": 1},
                        ],
                    }
                },
                Path(temp),
            )
            self.assertEqual(result["discovered"], 1)
            self.assertEqual(
                len(json.loads((Path(temp) / "raw_candidates.json").read_text(encoding="utf-8"))), 1
            )
        saved = store.get("repository", "upstream/tool")
        self.assertEqual(saved["status"], "APPROVED")
        self.assertEqual(saved["categories"], ["A", "B"])

    async def test_license_inactivity_and_small_active_repository(self):
        store = MemoryStore()
        for record in [
            repo("upstream/small"),
            repo("upstream/unlicensed", license=None),
            repo(
                "upstream/old",
                pushed_at=(
                    datetime.now(timezone.utc) - timedelta(days=200)
                ).isoformat(),
            ),
            repo("yutianjiang39/fork", fork=True),
        ]:
            normalized = normalize_repository(record)
            store.put("repository", normalized["repository"], normalized)
        with tempfile.TemporaryDirectory() as temp:
            summary = screen(store, {}, Path(temp))
        self.assertEqual(summary["shortlisted"], 1)
        self.assertEqual(
            store.get("repository", "upstream/small")["status"], "SCREENED"
        )
        self.assertEqual(
            store.get("repository", "upstream/unlicensed")["status"], "NEEDS_DATA"
        )
        self.assertEqual(store.get("repository", "upstream/old")["status"], "EXCLUDED")
        self.assertEqual(
            store.get("repository", "yutianjiang39/fork")["status"], "EXCLUDED"
        )

    async def test_missing_maintainer_data_is_unknown_not_rejection(self):
        store = MemoryStore()
        record = normalize_repository(repo())
        store.put("repository", record["repository"], record)
        with tempfile.TemporaryDirectory() as temp:
            score(store, {}, Path(temp))
        result = store.get("repository", "upstream/tool")
        self.assertEqual(result["status"], "EVIDENCE_REQUIRED")
        self.assertIsNone(result["score"]["total"])
        self.assertFalse(result["score"]["complete"])
        self.assertIsNone(result["score"]["dimensions"]["maintainer_openness"]["value"])
        self.assertGreater(result["score"]["range"][1], result["score"]["range"][0])

    async def test_custom_top_level_username_is_excluded_as_own_repository(self):
        store = MemoryStore()
        record = normalize_repository(repo("custom-user/tool"))
        store.put("repository", record["repository"], record)
        with tempfile.TemporaryDirectory() as temp:
            screen(store, {"username": "Custom-User"}, Path(temp))
        saved = store.get("repository", "custom-user/tool")
        self.assertIn("OWN_REPOSITORY", saved["screening"]["reasons"])

    async def test_maintainer_trust_falls_back_to_tracking_configuration(self):
        from unittest.mock import patch

        store = MemoryStore()
        record = normalize_repository(repo())
        record["status"] = "SCREENED"
        store.put("repository", record["repository"], record)
        observed = {}

        async def sample(client, record, settings):
            observed.update(settings)
            return {
                "status": "UNKNOWN",
                "openness_score": None,
                "evidence": [],
                "metrics": {},
            }

        with (
            tempfile.TemporaryDirectory() as temp,
            patch("repo_scout.discovery._maintainer_sample", side_effect=sample),
        ):
            await analyze_maintainers(
                None,
                store,
                {
                    "tracking": {
                        "trusted_maintainers": {"upstream/tool": ["verified-person"]}
                    }
                },
                Path(temp),
            )
        self.assertEqual(
            observed["trusted_maintainers"], {"upstream/tool": ["verified-person"]}
        )

    async def test_identity_is_not_guessed_from_membership(self):
        self.assertFalse(
            is_maintainer(
                {"user": {"login": "person"}, "author_association": "MEMBER"}, "org"
            )
        )
        self.assertFalse(
            is_maintainer(
                {"user": {"login": "person"}, "author_association": "COLLABORATOR"},
                "org",
            )
        )
        self.assertTrue(
            is_maintainer(
                {"user": {"login": "person"}, "author_association": "COLLABORATOR"},
                "org",
                {"person"},
            )
        )

    async def test_insufficient_external_prs_stay_unknown(self):
        store = MemoryStore()
        record = normalize_repository(repo())
        record["status"] = "SCREENED"
        store.put("repository", record["repository"], record)

        def handler(request):
            path = request.url.path
            if path.endswith("/releases/latest"):
                return httpx.Response(404)
            if path.endswith("/pulls") or path.endswith("/issues"):
                return httpx.Response(200, json=[])
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as temp:
            async with GitHubClient(transport=httpx.MockTransport(handler)) as client:
                result = await analyze_maintainers(client, store, {}, Path(temp))
        self.assertEqual(result["analyzed"], 1)
        analysis = store.get("repository", "upstream/tool")["maintainer"]
        self.assertEqual(analysis["status"], "UNKNOWN")
        self.assertIsNone(analysis["openness_score"])


if __name__ == "__main__":
    unittest.main()
