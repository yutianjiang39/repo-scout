from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

from repo_scout.audit import audit_bundle, parse_findings
from repo_scout.bundle import chunk_source, prepare_bundle, secret_reason
from repo_scout.database import Store
from repo_scout.providers import (
    AnthropicProvider, BudgetExceeded, GeminiProvider, ModelRequest, ModelRunner,
    OpenAICompatibleProvider, OpenAIProvider, ProviderError, ProviderRegistry,
)


def raw_finding(**changes):
    return {"title": "Reconnect drops pending subscriptions", "category": "reliability",
            "severity": "medium", "confidence": "MEDIUM", "file_path": "client.py",
            "line_start": 1, "line_end": 2, "trigger_condition": "connection is lost",
            "expected_behavior": "resubscribe", "actual_behavior": "subscription map cleared",
            "impact": "market-data events stop", "proposed_fix": "preserve pending state",
            "evidence": ["source clears the pending map"], "test_plan": ["simulate reconnect"], **changes}


def openai_response(text='{"findings": []}', **changes):
    return {"id": "resp_test", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 120, "output_tokens": 60}, **changes}


def init_repo(path: Path):
    for args in (["init", "-q", str(path)], ["-C", str(path), "add", "."],
                 ["-C", str(path), "-c", "user.name=Local Test", "-c", "user.email=test@example.invalid",
                  "commit", "-qm", "Local test fixture"]):
        subprocess.run(["git", *args], check=True, capture_output=True)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.output = self.base / "review_bundle"

    def tearDown(self):
        self.temp.cleanup()

    def test_excludes_secrets_binaries_and_symlink_without_mutating_original(self):
        (self.repo / "src").mkdir()
        original = b"def reconnect():\n    return True\n"
        (self.repo / "src" / "client.py").write_bytes(original)
        (self.repo / ".env").write_text("TOKEN=private")
        (self.repo / "credential.py").write_text('api_key = "abcdEFGH1234567890abcdEFGH"\n')
        (self.repo / "blob.dat").write_bytes(b"\x00\x01")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "code.js").write_text("not sent")
        outside = self.base / "outside"
        outside.write_text("private information")
        (self.repo / "linked.txt").symlink_to(outside)
        result = prepare_bundle(self.repo, self.output, repository="upstream/client")
        self.assertEqual([item["path"] for item in result["files"]], ["src/client.py"])
        excluded = {item["path"]: item["reason"] for item in result["excluded"]}
        self.assertEqual(excluded["credential.py"], "possible_hardcoded_secret")
        self.assertEqual(excluded["linked.txt"], "symlink")
        self.assertEqual((self.repo / "src/client.py").read_bytes(), original)
        self.assertEqual((self.output / "files/src/client.py").read_bytes(), original)
        self.assertTrue((self.repo / ".env").exists())
        self.assertNotIn("abcdEFGH", (self.output / "manifest.json").read_text())

    def test_incremental_hash_and_secret_introduction_removes_old_copy(self):
        source = self.repo / "client.py"
        source.write_text("value = 1\n")
        first = prepare_bundle(self.repo, self.output, repository="upstream/client")
        copy = self.output / "files/client.py"
        mtime = copy.stat().st_mtime_ns
        second = prepare_bundle(self.repo, self.output, repository="upstream/client")
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(copy.stat().st_mtime_ns, mtime)
        source.write_text('password = "j1k2l3m4n5o6p7q8r9s0"\n')
        final = prepare_bundle(self.repo, self.output, repository="upstream/client")
        self.assertEqual(final["files"], [])
        self.assertFalse(copy.exists())
        self.assertTrue(source.exists())

    def test_output_cannot_modify_original_or_follow_output_symlinks(self):
        (self.repo / "client.py").write_text("value = 1\n")
        with self.assertRaises(ValueError):
            prepare_bundle(self.repo, self.repo / "bundle")
        self.output.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare_bundle(self.repo, self.output)

    def test_function_boundaries_and_original_line_ranges(self):
        content = "import os\n\n@decorator\ndef first():\n    return 1\n\ndef second():\n    return 2\n"
        chunks = chunk_source("upstream/client", "client.py", content, chunk_tokens=64)
        lines = content.splitlines(keepends=True)
        self.assertEqual("".join(chunk["content"] for chunk in chunks), content)
        for chunk in chunks:
            self.assertEqual(chunk["content"], "".join(lines[chunk["line_start"] - 1:chunk["line_end"]]))
            self.assertEqual(chunk["dependencies"], ["os"])
        first_fn = [chunk for chunk in chunks if "def first" in chunk["content"]][0]
        self.assertIn("@decorator", first_fn["content"])
        self.assertIn("return 1", first_fn["content"])

    def test_large_function_is_explicitly_partial(self):
        text = "def large():\n" + "    value += 1\n" * 100
        chunks = chunk_source("upstream/client", "client.py", text, chunk_tokens=64)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk["partial"] for chunk in chunks))
        self.assertEqual("".join(chunk["content"] for chunk in chunks), text)

    def test_known_token_patterns_never_return_secret_value(self):
        secret = "ghp_" + "a" * 36
        self.assertEqual(secret_reason(secret), "possible_hardcoded_secret")
        self.assertIsNone(secret_reason('API_KEY = os.environ["API_KEY"]'))


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        self.env = patch.dict(os.environ, {"MOCK_MODEL_KEY": "unit-test-key"})
        self.env.start()
        self.request = ModelRequest("mock-model", "Return JSON only", "audit source", max_output_tokens=100)

    def tearDown(self):
        self.env.stop()
        self.store.close()
        self.temp.cleanup()

    def config(self, **changes):
        return {"api_key_env": "MOCK_MODEL_KEY", "retry_base_seconds": 0, "retries": 2, **changes}

    async def test_openai_payload_and_usage(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=openai_response())
        provider = OpenAIProvider(self.config(pricing={"mock-model": {"input_per_million": 2, "output_per_million": 6}}),
                                  transport=httpx.MockTransport(handler))
        result = await provider.generate(self.request)
        body = json.loads(requests[0].content)
        self.assertEqual(requests[0].url.path, "/v1/responses")
        self.assertEqual(body["instructions"], self.request.system)
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 100)
        self.assertEqual((result.input_tokens, result.output_tokens), (120, 60))
        self.assertAlmostEqual(result.cost_usd, 0.0006)

    async def test_anthropic_has_independent_shape_and_cached_tokens(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "msg_test", "content": [{"type": "text", "text": "{}"}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                        "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30}})
        result = await AnthropicProvider(self.config(), transport=httpx.MockTransport(handler)).generate(self.request)
        body = json.loads(requests[0].content)
        self.assertEqual(requests[0].url.path, "/v1/messages")
        self.assertEqual(requests[0].headers["x-api-key"], "unit-test-key")
        self.assertEqual(body["system"], self.request.system)
        self.assertNotIn("input", body)
        self.assertEqual(result.input_tokens, 60)
        self.assertIsNone(result.cost_usd)

    async def test_gemini_shape_includes_thinking_usage_and_key_only_in_header(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 6, "thoughtsTokenCount": 9}})
        result = await GeminiProvider(self.config(), transport=httpx.MockTransport(handler)).generate(self.request)
        body = json.loads(requests[0].content)
        self.assertEqual(requests[0].url.path, "/v1beta/models/mock-model:generateContent")
        self.assertEqual(requests[0].url.query, b"")
        self.assertEqual(requests[0].headers["x-goog-api-key"], "unit-test-key")
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(result.output_tokens, 15)

    async def test_compatible_chat_endpoint_is_separate(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
        result = await OpenAICompatibleProvider(self.config(max_tokens_field="max_completion_tokens"),
                                                transport=httpx.MockTransport(handler)).generate(self.request)
        self.assertEqual(requests[0].url.path, "/v1/chat/completions")
        self.assertEqual(json.loads(requests[0].content)["max_completion_tokens"], 100)
        self.assertEqual(result.output_tokens, 4)

    async def test_rate_limit_retry_and_no_retry_for_authorization_failure(self):
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"retry-after": "0"}) if calls == 1 else httpx.Response(200, json=openai_response())
        result = await OpenAIProvider(self.config(), transport=httpx.MockTransport(handler)).generate(self.request)
        self.assertEqual(calls, 2)
        self.assertEqual(result.attempts, 2)
        calls = 0
        def denied(request):
            nonlocal calls
            calls += 1
            return httpx.Response(401, text="unit-test-key should not be logged")
        with self.assertRaises(ProviderError) as error:
            await OpenAIProvider(self.config(), transport=httpx.MockTransport(denied)).generate(self.request)
        self.assertEqual(calls, 1)
        self.assertNotIn("unit-test-key", str(error.exception))

    async def test_ambiguous_transport_is_not_replayed(self):
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("secret response", request=request)
        with self.assertRaises(ProviderError):
            await OpenAIProvider(self.config(), transport=httpx.MockTransport(handler)).generate(self.request)
        self.assertEqual(calls, 1)

    async def test_no_redirect_or_parameter_override(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "https://evil.invalid/steal"})
        provider = OpenAIProvider(self.config(), transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError):
            await provider.generate(self.request)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ProviderError):
            await provider.generate(ModelRequest("mock-model", "JSON", "test", parameters={"tools": []}))
        self.assertEqual(len(calls), 1)

    async def test_missing_credentials_and_unsafe_config_fail_before_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderError):
                await OpenAIProvider(self.config()).generate(self.request)
        with self.assertRaises(ValueError):
            OpenAIProvider(self.config(base_url="https://token@example.invalid/api?key=token"))
        with self.assertRaises(ValueError):
            OpenAIProvider(self.config(api_key="literal"))
        with self.assertRaises(ProviderError):
            ModelRequest("mock-model", "JSON", "ghp_" + "a" * 36)

    async def test_cache_full_parameters_version_and_concurrent_deduplication(self):
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json=openai_response())
        registry = ProviderRegistry({"p": {"type": "openai", **self.config()}}, transport=httpx.MockTransport(handler))
        runner = ModelRunner(registry, self.store, run_id="run1")
        results = await asyncio.gather(runner.run("p", self.request), runner.run("p", self.request))
        self.assertEqual(calls, 1)
        self.assertEqual(sum(result.cached for result in results), 1)
        runner2 = ModelRunner(registry, self.store, run_id="run2")
        self.assertTrue((await runner2.run("p", self.request)).cached)
        await runner2.run("p", self.request, prompt_version="2")
        await runner2.run("p", ModelRequest("mock-model", "Return JSON only", "audit source", 100, {"temperature": 0.2}))
        self.assertEqual(calls, 3)
        self.assertNotIn("unit-test-key", json.dumps(self.store.list("model_usage")))

    async def test_budget_blocks_unpriced_or_too_large_requests_before_network(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=openai_response())
        registry = ProviderRegistry({"p": {"type": "openai", **self.config()}}, transport=httpx.MockTransport(handler))
        with self.assertRaises(BudgetExceeded):
            await ModelRunner(registry, self.store, input_budget=1).run("p", self.request)
        with self.assertRaises(BudgetExceeded):
            await ModelRunner(registry, self.store, cost_budget_usd=1).run("p", self.request)
        self.assertEqual(calls, [])

    async def test_absent_usage_is_unknown_not_zero(self):
        provider = OpenAIProvider(self.config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=openai_response(usage=None))))
        response = await provider.generate(self.request)
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertFalse(response.usage_known)
        self.assertIsNone(response.cost_usd)

    async def test_failed_request_usage_persists_unknown_not_pass(self):
        registry = ProviderRegistry({"p": {"type": "openai", **self.config()}},
                                    transport=httpx.MockTransport(lambda r: httpx.Response(401)))
        runner = ModelRunner(registry, self.store, run_id="failed")
        with self.assertRaises(ProviderError):
            await runner.run("p", self.request)
        record = self.store.list("model_usage")[0]
        self.assertFalse(record["usage_complete"])
        self.assertIsNone(record["input_tokens"])
        self.assertEqual(record["status"], "FAILED_OR_AMBIGUOUS")


class AuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "client.py").write_bytes(b"def reconnect():\r\n    pending.clear()\r\n    return True\r\n")
        init_repo(self.repo)
        self.bundle = self.base / "bundle"
        self.manifest = prepare_bundle(self.repo, self.bundle, repository="upstream/client")
        self.store = Store(self.base / "db.sqlite")
        self.env = patch.dict(os.environ, {"MOCK_MODEL_KEY": "unit-test-key"})
        self.env.start()
        self.config = {"providers": {"test": {"type": "openai", "api_key_env": "MOCK_MODEL_KEY", "retries": 0}},
                       "roles": {"primary_auditor": {"provider": "test", "model": "mock-model"}}}

    def tearDown(self):
        self.env.stop()
        self.store.close()
        self.temp.cleanup()

    async def test_malicious_confirmed_claim_remains_candidate_and_nonexistent_path_rejected(self):
        payload = json.dumps({"findings": [raw_finding(status="CONFIRMED", evidence_status="CONFIRMED"),
                                          raw_finding(file_path="does_not_exist.py"), raw_finding(line_end=999)]})
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=openai_response(payload)))
        run = await audit_bundle(self.bundle, self.config, self.store, output=self.base / "reports", transport=transport)
        self.assertEqual(len(run["findings"]), 1)
        finding = run["findings"][0]
        self.assertEqual(finding["status"], "CANDIDATE")
        self.assertEqual(finding["evidence_status"], "INFERRED")
        self.assertEqual(finding["provenance"], "imported")
        self.assertEqual(len(run["verification"][0]["rejected_outputs"]), 2)
        self.assertIn("pending.clear()", finding["evidence"][0]["content"])
        self.assertTrue((self.base / "reports/audit_report.md").exists())

    async def test_max_five_findings_even_if_config_asks_for_more(self):
        self.config["audit"] = {"max_findings": 1000}
        payload = json.dumps({"findings": [raw_finding(title=f"Bug candidate number {i}") for i in range(8)]})
        run = await audit_bundle(self.bundle, self.config, self.store,
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200, json=openai_response(payload))))
        self.assertEqual(len(run["findings"]), 5)
        self.assertEqual(len(self.store.list("finding")), 5)

    async def test_resume_uses_cache_and_preserves_downstream_validation(self):
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=openai_response(json.dumps({"findings": [raw_finding()]})))
        transport = httpx.MockTransport(handler)
        run = await audit_bundle(self.bundle, self.config, self.store, transport=transport)
        finding = self.store.require("finding", run["findings"][0]["finding_id"])
        finding["status"] = "CONFIRMED"
        self.store.put("finding", finding["finding_id"], finding)
        rerun = await audit_bundle(self.bundle, self.config, self.store, transport=transport)
        self.assertEqual(calls, 1)
        self.assertTrue(rerun["usage"][0]["cached"])
        self.assertEqual(self.store.require("finding", finding["finding_id"])["status"], "CONFIRMED")

    async def test_changed_bundle_cannot_use_stale_locations(self):
        (self.bundle / "files/client.py").write_text("unreviewed source\n")
        with self.assertRaises(ValueError):
            await audit_bundle(self.bundle, self.config, self.store)

    async def test_partial_audit_does_not_claim_no_bugs(self):
        self.config["audit"] = {"input_token_budget": 1}
        run = await audit_bundle(self.bundle, self.config, self.store)
        self.assertFalse(run["complete"])
        self.assertEqual(run["status"], "PARTIAL")
        self.assertEqual(run["verification"][0]["status"], "BUDGET_DEFERRED")

    async def test_adversarial_role_can_dispute_but_never_confirm(self):
        self.config["roles"]["adversarial_reviewer"] = {"provider": "test", "model": "mock-model"}
        def handler(request):
            prompt = json.loads(request.content)["input"]
            payload = ({"verdict": "REFUTED", "reason": "Caller restores subscriptions", "evidence": []}
                       if "Try to disprove" in prompt else {"findings": [raw_finding()]})
            return httpx.Response(200, json=openai_response(json.dumps(payload)))
        run = await audit_bundle(self.bundle, self.config, self.store, transport=httpx.MockTransport(handler))
        self.assertEqual(run["findings"][0]["status"], "CANDIDATE")
        self.assertEqual(run["findings"][0]["recommendation"], "DISCARD_OR_REINVESTIGATE")
        self.assertEqual(len(run["findings"][0]["assessments"]), 2)


if __name__ == "__main__":
    unittest.main()
