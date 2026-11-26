"""Independent, credential-safe HTTP adapters behind one small model interface."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import re
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from .secrets import secret
from .bundle import secret_reason, token_estimate


class ProviderError(RuntimeError):
    """Safe error message: never contains request headers, source or API body."""


class BudgetExceeded(ProviderError):
    pass


class RequestRejected(ProviderError):
    """The service explicitly denied the request, or it was not sent."""


@dataclass(frozen=True)
class ModelRequest:
    model: str
    system: str
    prompt: str
    max_output_tokens: int = 2048
    parameters: dict[str, Any] = field(default_factory=dict)
    json_output: bool = True

    def __post_init__(self) -> None:
        if not self.model or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", self.model):
            raise ValueError("A real model ID must be explicitly configured")
        if not 1 <= self.max_output_tokens <= 100_000:
            raise ValueError("max_output_tokens is out of bounds")
        if secret_reason(self.prompt) or secret_reason(self.system):
            raise ProviderError(
                "Possible credential detected in model context; request blocked"
            )


@dataclass
class ModelResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    usage_known: bool
    cost_usd: float | None = None
    request_id: str | None = None
    attempts: int = 1
    cached: bool = False
    usage_details: dict = field(default_factory=dict)


class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def estimate_tokens(self, content: str) -> int: ...
    async def health_check(self) -> dict: ...
    async def get_usage(self, response: ModelResponse) -> dict: ...


_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
_DEFAULT_ENVS = {
    "openai": "OPENAI_API_KEY",
    "openai_compatible": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _request_id(value: Any) -> str | None:
    return (
        str(value)
        if isinstance(value, (str, int))
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", str(value))
        else None
    )


def _retry_delay(header: str | None, default: float) -> float:
    if not header:
        return default
    try:
        return max(0.0, float(header))
    except ValueError:
        try:
            date = parsedate_to_datetime(header)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            return default


class HTTPProvider:
    provider_type = ""

    def __init__(self, config: dict, *, transport=None) -> None:
        self.config = dict(config)
        if "api_key" in config or "token" in config or "headers" in config:
            raise ValueError(
                "Credentials must use api_key_env; literal keys/headers are forbidden"
            )
        self.base_url = str(
            config.get("base_url", _BASE_URLS[self.provider_type])
        ).rstrip("/")
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Provider base_url must be an HTTPS origin/path without credentials/query"
            )
        self.api_key_env = str(
            config.get("api_key_env", _DEFAULT_ENVS[self.provider_type])
        )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must be an environment-variable name")
        self.timeout = float(config.get("timeout_seconds", 90))
        self.retries = int(config.get("retries", 2))
        self.retry_base = float(config.get("retry_base_seconds", 1))
        self.max_backoff = float(config.get("max_backoff_seconds", 30))
        if (
            not 0 <= self.retries <= 5
            or self.timeout <= 0
            or self.retry_base < 0
            or self.max_backoff < 0
        ):
            raise ValueError("Invalid provider timeout/retry configuration")
        self.transport = transport

    def public_config(self) -> dict:
        return {
            "type": self.provider_type,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_version": self.config.get("api_version"),
            "input_per_million": self.config.get("input_per_million"),
            "output_per_million": self.config.get("output_per_million"),
            "pricing": self.config.get("pricing", {}),
        }

    def _headers(self) -> dict:
        key = secret(self.api_key_env)
        if not key:
            raise RequestRejected(
                f"Missing API credential environment variable: {self.api_key_env}"
            )
        if self.provider_type == "anthropic":
            return {
                "x-api-key": key,
                "anthropic-version": self.config.get("api_version", "2023-06-01"),
            }
        if self.provider_type == "gemini":
            return {"x-goog-api-key": key}
        return {"Authorization": "Bearer " + key}

    def _build(self, request: ModelRequest) -> tuple[str, dict]:
        raise NotImplementedError

    def _parse(self, response: dict) -> ModelResponse:
        raise NotImplementedError

    def _parameters(self, request: ModelRequest, allowed: set[str]) -> dict:
        unexpected = set(request.parameters) - allowed
        if unexpected:
            raise ProviderError(
                "Unsupported or protected generation parameters: "
                + ", ".join(sorted(unexpected))
            )
        return dict(request.parameters)

    def _estimate_cost(self, response: ModelResponse, model: str = "") -> float | None:
        pricing = self.config.get("pricing", {}).get(model, self.config)
        prices = pricing.get("input_per_million"), pricing.get("output_per_million")
        if not response.usage_known or any(value is None for value in prices):
            return None
        if any(
            not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value)
            for value in prices
        ):
            raise ProviderError(
                "Configured token prices must be nonnegative finite numbers"
            )
        return (
            response.input_tokens * prices[0] + response.output_tokens * prices[1]
        ) / 1_000_000

    async def generate(self, request: ModelRequest) -> ModelResponse:
        endpoint, payload = self._build(request)
        headers = self._headers()
        # No redirects: credentials may only go to the configured API endpoint.
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.post(
                        self.base_url + endpoint, headers=headers, json=payload
                    )
                except httpx.TransportError:
                    # Processing may already have happened: never replay an ambiguous generation.
                    raise ProviderError(
                        "Model transport failed; outcome/charge unknown; no automatic replay"
                    ) from None
                if response.status_code == 429 and attempt < self.retries:
                    delay = _retry_delay(
                        response.headers.get("retry-after"),
                        self.retry_base * 2**attempt,
                    )
                    if delay > self.max_backoff:
                        raise RequestRejected(
                            "Provider requested a longer backoff; retry this run later"
                        )
                    await asyncio.sleep(delay)
                    continue
                if not 200 <= response.status_code < 300:
                    error = (
                        RequestRejected
                        if response.status_code in {400, 401, 403, 404, 422, 429}
                        else ProviderError
                    )
                    raise error(
                        f"Model API returned HTTP {response.status_code}; response body withheld"
                    )
                try:
                    result = self._parse(response.json())
                except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                    raise ProviderError(
                        "Malformed or incomplete model response"
                    ) from None
                if not isinstance(result.text, str) or not result.text.strip():
                    raise ProviderError(
                        "Model returned no text (blocked, refused, or incomplete)"
                    )
                if secret_reason(result.text):
                    raise ProviderError(
                        "Possible credential in model output; response withheld"
                    )
                result.attempts = attempt + 1
                result.usage_details["model_version"] = _request_id(
                    response.json().get("modelVersion") or response.json().get("model")
                )
                result.request_id = (
                    _request_id(response.headers.get("x-request-id"))
                    or result.request_id
                )
                result.cost_usd = self._estimate_cost(result, request.model)
                return result
        raise ProviderError("Model retry limit exceeded")

    async def estimate_tokens(self, content: str) -> int:
        # No paid counting call. One UTF-8 byte per token plus framing allowance
        # is deliberately more conservative than the chunk-sizing heuristic.
        return max(1, len(content.encode("utf-8"))) + 256

    async def get_usage(self, response: ModelResponse) -> dict:
        return {
            key: value for key, value in asdict(response).items() if key not in {"text"}
        }

    async def health_check(self) -> dict:
        headers = self._headers()
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            try:
                response = await client.get(self.base_url + "/models", headers=headers)
                return {
                    "healthy": response.is_success,
                    "http_status": response.status_code,
                    "provider": self.provider_type,
                    "generation_checked": False,
                }
            except httpx.TransportError:
                return {
                    "healthy": False,
                    "provider": self.provider_type,
                    "error": "transport_error",
                }


class OpenAIProvider(HTTPProvider):
    provider_type = "openai"

    def _build(self, request: ModelRequest) -> tuple[str, dict]:
        body = self._parameters(
            request, {"temperature", "top_p", "reasoning", "service_tier"}
        )
        body.update(
            {
                "model": request.model,
                "instructions": request.system,
                "input": request.prompt,
                "max_output_tokens": request.max_output_tokens,
                "store": False,
            }
        )
        if request.json_output:
            body["text"] = {"format": {"type": "json_object"}}
        return "/responses", body

    def _parse(self, response: dict) -> ModelResponse:
        if response.get("status", "completed") != "completed":
            raise ProviderError("OpenAI response is incomplete; no findings accepted")
        text = "".join(
            part.get("text", "")
            for item in response.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        usage = response.get("usage") or {}
        input_tokens, output_tokens = (
            _count(usage.get("input_tokens")),
            _count(usage.get("output_tokens")),
        )
        return ModelResponse(
            text,
            input_tokens,
            output_tokens,
            input_tokens is not None and output_tokens is not None,
            request_id=_request_id(response.get("id")),
        )


class OpenAICompatibleProvider(HTTPProvider):
    provider_type = "openai_compatible"

    def _build(self, request: ModelRequest) -> tuple[str, dict]:
        body = self._parameters(
            request, {"temperature", "top_p", "seed", "stop", "reasoning_effort"}
        )
        max_field = self.config.get("max_tokens_field", "max_tokens")
        if max_field not in {"max_tokens", "max_completion_tokens"}:
            raise ProviderError(
                "max_tokens_field must be max_tokens or max_completion_tokens"
            )
        body.update(
            {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.prompt},
                ],
                max_field: request.max_output_tokens,
            }
        )
        if request.json_output:
            body["response_format"] = {"type": "json_object"}
        return "/chat/completions", body

    def public_config(self) -> dict:
        return {
            **super().public_config(),
            "max_tokens_field": self.config.get("max_tokens_field", "max_tokens"),
        }

    def _parse(self, response: dict) -> ModelResponse:
        choice = response["choices"][0]
        if choice.get("finish_reason") not in {None, "stop"}:
            raise ProviderError(
                "Chat response is incomplete/refused; no findings accepted"
            )
        usage = response.get("usage") or {}
        input_tokens, output_tokens = (
            _count(usage.get("prompt_tokens")),
            _count(usage.get("completion_tokens")),
        )
        return ModelResponse(
            choice["message"]["content"],
            input_tokens,
            output_tokens,
            input_tokens is not None and output_tokens is not None,
            request_id=_request_id(response.get("id")),
        )


class AnthropicProvider(HTTPProvider):
    provider_type = "anthropic"

    def _build(self, request: ModelRequest) -> tuple[str, dict]:
        body = self._parameters(
            request, {"temperature", "top_p", "top_k", "thinking", "stop_sequences"}
        )
        body.update(
            {
                "model": request.model,
                "system": request.system,
                "messages": [{"role": "user", "content": request.prompt}],
                "max_tokens": request.max_output_tokens,
            }
        )
        # Provider-specific schema features vary by model/API version. JSON is
        # requested in the prompt and independently validated by our parser.
        return "/messages", body

    def _parse(self, response: dict) -> ModelResponse:
        if response.get("stop_reason") not in {None, "end_turn", "stop_sequence"}:
            raise ProviderError(
                "Anthropic response is incomplete/refused; no findings accepted"
            )
        text = "".join(
            item["text"]
            for item in response.get("content", [])
            if item.get("type") == "text"
        )
        usage = response.get("usage") or {}
        input_tokens, output_tokens = (
            _count(usage.get("input_tokens")),
            _count(usage.get("output_tokens")),
        )
        cached = _count(usage.get("cache_read_input_tokens")) or 0
        created = _count(usage.get("cache_creation_input_tokens")) or 0
        if input_tokens is not None:
            input_tokens += cached + created
        return ModelResponse(
            text,
            input_tokens,
            output_tokens,
            input_tokens is not None and output_tokens is not None,
            request_id=_request_id(response.get("id")),
            usage_details={
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": created,
            },
        )


class GeminiProvider(HTTPProvider):
    provider_type = "gemini"

    def _build(self, request: ModelRequest) -> tuple[str, dict]:
        model = request.model.removeprefix("models/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
            raise ProviderError("Gemini model ID is not a model resource name")
        generation = self._parameters(
            request,
            {"temperature", "topP", "topK", "seed", "stopSequences", "thinkingConfig"},
        )
        generation["maxOutputTokens"] = request.max_output_tokens
        if request.json_output:
            generation["responseMimeType"] = "application/json"
        return f"/models/{model}:generateContent", {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": generation,
        }

    def _parse(self, response: dict) -> ModelResponse:
        candidate = response.get("candidates", [])[0]
        if candidate.get("finishReason") not in {None, "STOP"}:
            raise ProviderError(
                "Gemini response is incomplete/refused; no findings accepted"
            )
        text = "".join(
            part.get("text", "")
            for part in candidate.get("content", {}).get("parts", [])
            if not part.get("thought")
        )
        usage = response.get("usageMetadata") or {}
        input_tokens, output_tokens = (
            _count(usage.get("promptTokenCount")),
            _count(usage.get("candidatesTokenCount")),
        )
        thoughts = _count(usage.get("thoughtsTokenCount")) or 0
        if output_tokens is not None:
            output_tokens += thoughts
        return ModelResponse(
            text,
            input_tokens,
            output_tokens,
            input_tokens is not None and output_tokens is not None,
            request_id=_request_id(response.get("responseId")),
            usage_details={"thoughts_tokens": thoughts},
        )


class ProviderRegistry:
    types = {
        "openai": OpenAIProvider,
        "openai_compatible": OpenAICompatibleProvider,
        "openai-compatible": OpenAICompatibleProvider,
        "anthropic": AnthropicProvider,
        "gemini": GeminiProvider,
    }

    def __init__(self, providers: dict, *, transport=None) -> None:
        self.providers = providers
        self.transport = transport
        self.instances: dict[str, HTTPProvider] = {}

    def get(self, name: str) -> HTTPProvider:
        if name not in self.providers:
            raise ProviderError(f"Provider {name!r} is not configured")
        if name not in self.instances:
            config = self.providers[name]
            provider_type = config.get("type")
            if provider_type not in self.types:
                raise ProviderError(f"Unsupported provider type: {provider_type}")
            self.instances[name] = self.types[provider_type](
                config, transport=self.transport
            )
        return self.instances[name]


class ModelRunner:
    """Run-scoped concurrency and conservative reservations; durable per-call cache.

    Requests with ambiguous transport outcomes are not replayed. Retry attempts
    reserve their full estimated input/output ceiling; reported usage is the
    known successful attempt and explicitly marked incomplete after retries.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        store,
        *,
        input_budget: int = 100_000,
        output_budget: int = 20_000,
        cost_budget_usd: float | None = None,
        concurrency: int = 2,
        run_id: str = "",
    ) -> None:
        if input_budget < 1 or output_budget < 1 or concurrency < 1:
            raise ValueError("Budgets and concurrency must be positive")
        if cost_budget_usd is not None and (
            cost_budget_usd <= 0 or not math.isfinite(cost_budget_usd)
        ):
            raise ValueError("Cost budget must be a positive finite number")
        self.registry, self.store, self.run_id = registry, store, run_id
        self.input_budget, self.output_budget, self.cost_budget = (
            input_budget,
            output_budget,
            cost_budget_usd,
        )
        self.semaphore = asyncio.Semaphore(concurrency)
        self.lock = asyncio.Lock()
        self.reserved_input = self.reserved_output = 0
        self.reserved_cost = 0.0
        self.usage: list[dict] = []
        self._key_locks: dict[str, asyncio.Lock] = {}

    async def run(
        self,
        provider_name: str,
        request: ModelRequest,
        *,
        prompt_version: str = "1",
        role: str = "unspecified",
    ) -> ModelResponse:
        provider = self.registry.get(provider_name)
        key = stable_hash(
            {
                "provider": provider.public_config(),
                "request": asdict(request),
                "prompt_version": prompt_version,
            }
        )
        # Identical concurrent work shares the same durable response.
        lock = self._key_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.store.cache_get("model_response", key)
            if cached is not None:
                result = ModelResponse(**cached)
                result.cached = True
                self.usage.append(
                    {
                        "cache_key": key,
                        "provider": provider_name,
                        "model": request.model,
                        "cached": True,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                        "source_input_tokens": result.input_tokens,
                        "source_output_tokens": result.output_tokens,
                        "source_cost_usd": result.cost_usd,
                        "source_usage_complete": result.usage_known
                        and result.attempts == 1,
                    }
                )
                return result
            input_estimate = await provider.estimate_tokens(
                request.system + "\n" + request.prompt
            )
            attempts = provider.retries + 1
            reserved_in, reserved_out = (
                input_estimate * attempts,
                request.max_output_tokens * attempts,
            )
            estimated_cost = provider._estimate_cost(
                ModelResponse("", reserved_in, reserved_out, True), request.model
            )
            async with self.lock:
                if (
                    self.reserved_input + reserved_in > self.input_budget
                    or self.reserved_output + reserved_out > self.output_budget
                ):
                    raise BudgetExceeded(
                        "Run token budget exhausted before making a model request"
                    )
                if self.cost_budget is not None:
                    if estimated_cost is None:
                        raise BudgetExceeded(
                            "Cost budget requires configured input/output prices for every provider"
                        )
                    if self.reserved_cost + estimated_cost > self.cost_budget:
                        raise BudgetExceeded(
                            "Run cost budget exhausted before making a model request"
                        )
                self.reserved_input += reserved_in
                self.reserved_output += reserved_out
                self.reserved_cost += estimated_cost or 0.0
            meta = {
                "prompt_version": prompt_version,
                "task_role": role,
                "provider": provider_name,
                "model": request.model,
            }
            with self.store.atomic():
                if self.store.cache_get("model_pending", key):
                    raise ProviderError(
                        "Prior request is in flight or its usage is unknown; reconcile before replay"
                    )
                self.store.cache_set(
                    "model_pending", key, {**meta, "status": "PENDING"}
                )
            try:
                async with self.semaphore:
                    result = await provider.generate(request)
            except ProviderError as exc:
                entry = {
                    **meta,
                    "cache_key": key,
                    "run_id": self.run_id,
                    "provider": provider_name,
                    "model": request.model,
                    "cached": False,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cost_usd": None,
                    "usage_known": False,
                    "usage_complete": False,
                    "status": "REJECTED"
                    if isinstance(exc, RequestRejected)
                    else "UNKNOWN_USAGE",
                    "reserved_input_tokens": reserved_in,
                    "reserved_output_tokens": reserved_out,
                }
                self.usage.append(entry)
                with self.store.atomic():
                    self.store.cache_set(
                        "model_pending",
                        key,
                        {} if isinstance(exc, RequestRejected) else entry,
                    )
                    self.store.put(
                        "model_usage",
                        stable_hash([self.run_id, key]),
                        entry,
                        reason="Model request failed; usage may be unknown",
                        actor="model-runner",
                    )
                raise
            entry = {
                **meta,
                "status": "COMPLETED",
                "model_version": result.usage_details.get("model_version"),
                "cache_key": key,
                "run_id": self.run_id,
                "provider": provider_name,
                "model": request.model,
                "cached": False,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "usage_known": result.usage_known,
                "attempts": result.attempts,
                "usage_complete": result.usage_known and result.attempts == 1,
                "estimated_input_tokens": input_estimate,
                "reserved_input_tokens": reserved_in,
                "reserved_output_tokens": reserved_out,
            }
            self.usage.append(entry)
            with self.store.atomic():
                self.store.cache_set("model_response", key, asdict(result))
                self.store.cache_set("model_pending", key, {})
                self.store.put(
                    "model_usage",
                    stable_hash([self.run_id, key]),
                    entry,
                    reason="Model request usage",
                    actor="model-runner",
                )
            return result
