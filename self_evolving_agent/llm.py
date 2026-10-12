"""Minimal openai-compatible LLM client with token/cost accounting."""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _read_with_deadline(response, deadline_s: float) -> bytes:
    """Read a response body with an overall deadline.

    The socket timeout only bounds the gap between bytes; a server that keeps
    the connection alive with whitespace while it queues the request would
    otherwise hold the call open indefinitely.
    """
    if not hasattr(response, "read1"):
        return response.read()
    started, chunks = time.monotonic(), []
    while True:
        chunk = response.read1(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        if time.monotonic() - started > deadline_s:
            raise socket.timeout(f"response body exceeded {deadline_s:.0f}s overall deadline")


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class LLMResponse:
    content: str
    raw_response: dict[str, Any]
    usage: LLMUsage
    model: str
    request_max_completion_tokens: int | None = None
    request_timeout_s: int | None = None
    request_attempts: tuple[dict[str, Any], ...] = ()
    requested_model: str | None = None
    deployment: str | None = None
    reasoning_effort: str | None = None
    request_latency_ms: int | None = None


class LLMRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str,
        retryable: bool,
        request_attempts: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.request_attempts = tuple(request_attempts or [])


class LLMClient:
    """Tiny client for OpenAI-compatible chat completions endpoints.

    It intentionally depends only on the Python standard library so the `evo`
    environment stays light. The request/response format follows the common
    `/chat/completions` schema used by OpenAI-compatible providers.
    """

    def __init__(self, config: dict[str, Any]):
        llm = config.get("llm", {}) or {}
        self.enabled = bool(llm.get("enabled", False))
        self.provider = str(llm.get("provider", "openai-compatible"))
        self.base_url = str(llm.get("base_url", "")).rstrip("/")
        self.api_key_env = str(llm.get("api_key_env") or "OPENAI_API_KEY")
        configured_api_key = llm.get("api_key")
        self._api_key_from_config = bool(configured_api_key)
        self.api_key_source = (
            "configured llm.api_key"
            if self._api_key_from_config
            else f"environment variable {self.api_key_env}"
        )
        self.api_key = str(configured_api_key or os.getenv(self.api_key_env, ""))
        self.model = str(llm.get("model", ""))
        self.requested_model = str(
            llm.get("requested_model") or llm.get("model_family") or self.model
        )
        self.deployment = str(
            llm.get("deployment") or llm.get("deployment_id") or self.model
        )
        configured_reasoning_effort = llm.get("reasoning_effort")
        self.reasoning_effort = (
            None
            if configured_reasoning_effort in {None, ""}
            else str(configured_reasoning_effort)
        )
        self.reasoning_effort_field = str(
            llm.get("reasoning_effort_field") or "reasoning_effort"
        )
        self.timeout_s = int(llm.get("timeout_s", 120))
        self.timeout_retries = max(0, int(llm.get("timeout_retries", 1)))
        self.timeout_retry_multiplier = max(
            1.0, float(llm.get("timeout_retry_multiplier", 2.0))
        )
        self.timeout_max_s = max(
            self.timeout_s, int(llm.get("timeout_max_s", max(self.timeout_s, 600)))
        )
        self.retry_backoff_s = max(0.0, float(llm.get("retry_backoff_s", 0.0)))
        temperature = llm.get("temperature")
        self.temperature = None if temperature is None else float(temperature)
        self.max_tokens = llm.get("max_tokens")
        self.max_completion_tokens = llm.get("max_completion_tokens")
        configured_cap = llm.get("max_completion_tokens_cap")
        if configured_cap is None and self.max_completion_tokens is not None:
            configured_cap = max(int(self.max_completion_tokens), 8192)
        self.max_completion_tokens_cap = (
            None if configured_cap is None else max(1, int(configured_cap))
        )
        self.length_retries = max(0, int(llm.get("length_retries", 1)))
        self.seed = llm.get("seed")
        self.thinking = llm.get("thinking")
        self.response_format = llm.get("response_format")
        self.completion_token_field = llm.get("completion_token_field", "max_completion_tokens")
        if self.completion_token_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("unsupported completion token field")
        self.prompt_token_price = float(llm.get("prompt_token_price", 0.0))
        self.completion_token_price = float(llm.get("completion_token_price", 0.0))

    def is_configured(self) -> bool:
        return self.configuration_error() is None

    def configuration_error(self) -> str | None:
        if not self.enabled:
            return "llm.enabled is false"
        if not self.base_url:
            return "llm.base_url is empty"
        if not self.model:
            return "llm.model is empty"
        if not self.api_key:
            if self._api_key_from_config:
                return "configured llm.api_key is empty"
            exported = self.api_key_env in os.environ
            return (
                f"environment variable {self.api_key_env} is "
                f"{'empty' if exported else 'not exported'}"
            )
        try:
            self.api_key.encode("latin-1")
        except UnicodeEncodeError:
            return (
                f"{self.api_key_source} contains characters that cannot be "
                "sent in HTTP headers; re-export an ASCII API key"
            )
        return None

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_completion_tokens: int | None = None,
        timeout_s: int | None = None,
    ) -> LLMResponse:
        configuration_error = self.configuration_error()
        if configuration_error:
            raise RuntimeError(
                "LLM client is not configured. "
                f"{configuration_error}."
            )

        request_max_completion_tokens = (
            int(max_completion_tokens)
            if max_completion_tokens is not None
            else (
                int(self.max_completion_tokens)
                if self.max_completion_tokens is not None
                else None
            )
        )
        request_timeout_s = int(timeout_s) if timeout_s is not None else self.timeout_s
        payload: dict[str, Any] = {
            "model": self.deployment,
            "messages": messages,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if request_max_completion_tokens is not None:
            payload[self.completion_token_field] = request_max_completion_tokens
        elif self.max_tokens is not None:
            payload["max_tokens"] = int(self.max_tokens)
        if self.seed is not None:
            payload["seed"] = int(self.seed)
        if self.reasoning_effort is not None:
            payload[self.reasoning_effort_field] = self.reasoning_effort
        if self.thinking is not None:
            payload["thinking"] = self.thinking
        if self.response_format is not None:
            payload["response_format"] = self.response_format

        data = json.dumps(payload).encode("utf-8")
        request = Request(
            url=f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                # Some providers expect Authorization, some expect api-key.
                "Authorization": f"Bearer {self.api_key}",
                "api-key": self.api_key,
            },
            method="POST",
        )
        request_attempts: list[dict[str, Any]] = []
        raw: dict[str, Any] | None = None
        current_timeout_s = request_timeout_s
        request_started = time.perf_counter()
        for attempt_index in range(self.timeout_retries + 1):
            attempt_number = attempt_index + 1
            try:
                with urlopen(request, timeout=current_timeout_s) as response:
                    raw = json.loads(_read_with_deadline(response, 2 * current_timeout_s).decode("utf-8"))
                request_attempts.append(
                    {
                        "attempt": attempt_number,
                        "timeout_s": current_timeout_s,
                        "status": "success",
                    }
                )
                break
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                request_attempts.append(
                    {
                        "attempt": attempt_number,
                        "timeout_s": current_timeout_s,
                        "status": "http_error",
                        "http_status": exc.code,
                    }
                )
                # Rate limits and server errors are transient; other HTTP
                # errors (bad request, auth) are never retried.
                if (exc.code == 429 or exc.code >= 500) and attempt_index < self.timeout_retries:
                    if self.retry_backoff_s:
                        time.sleep(self.retry_backoff_s * attempt_number)
                    continue
                raise LLMRequestError(
                    f"LLM HTTP error {exc.code}: {body}",
                    category="http_error",
                    retryable=False,
                    request_attempts=request_attempts,
                ) from exc
            except (TimeoutError, socket.timeout) as exc:
                request_attempts.append(
                    {
                        "attempt": attempt_number,
                        "timeout_s": current_timeout_s,
                        "status": "timeout",
                    }
                )
                if attempt_index >= self.timeout_retries:
                    raise LLMRequestError(
                        f"LLM read timeout after {attempt_number} attempt(s): {exc}",
                        category="timeout",
                        retryable=True,
                        request_attempts=request_attempts,
                    ) from exc
            except URLError as exc:
                category = "timeout" if _is_timeout_error(exc) else "connection_error"
                request_attempts.append(
                    {
                        "attempt": attempt_number,
                        "timeout_s": current_timeout_s,
                        "status": category,
                    }
                )
                if attempt_index >= self.timeout_retries:
                    raise LLMRequestError(
                        f"LLM connection error: {exc}",
                        category=category,
                        retryable=True,
                        request_attempts=request_attempts,
                    ) from exc
            except (ConnectionError, ssl.SSLError, http.client.IncompleteRead) as exc:
                request_attempts.append(
                    {
                        "attempt": attempt_number,
                        "timeout_s": current_timeout_s,
                        "status": "connection_error",
                    }
                )
                if attempt_index >= self.timeout_retries:
                    raise LLMRequestError(
                        f"LLM connection error: {exc}",
                        category="connection_error",
                        retryable=True,
                        request_attempts=request_attempts,
                    ) from exc

            if self.retry_backoff_s:
                time.sleep(self.retry_backoff_s * attempt_number)
            current_timeout_s = min(
                self.timeout_max_s,
                max(
                    current_timeout_s + 1,
                    int(current_timeout_s * self.timeout_retry_multiplier),
                ),
            )

        if raw is None:
            raise LLMRequestError(
                "LLM request failed without a response.",
                category="unknown",
                retryable=False,
                request_attempts=request_attempts,
            )

        content = _extract_content(raw)
        usage = _extract_usage(raw, self.prompt_token_price, self.completion_token_price)
        return LLMResponse(
            content=content,
            raw_response=raw,
            usage=usage,
            model=self.model,
            request_max_completion_tokens=request_max_completion_tokens,
            request_timeout_s=request_timeout_s,
            request_attempts=tuple(request_attempts),
            requested_model=self.requested_model,
            deployment=self.deployment,
            reasoning_effort=self.reasoning_effort,
            request_latency_ms=max(
                1,
                int((time.perf_counter() - request_started) * 1000),
            ),
        )

    def next_completion_budget(self, current: int | None) -> int | None:
        if current is None:
            return None
        cap = self.max_completion_tokens_cap
        if cap is None or current >= cap:
            return None
        return min(cap, max(current + 1, current * 2))


def _extract_content(raw: dict[str, Any]) -> str:
    choices = raw.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(content)


def _extract_usage(
    raw: dict[str, Any],
    prompt_token_price: float,
    completion_token_price: float,
) -> LLMUsage:
    usage = raw.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    cost_usd = (
        prompt_tokens * prompt_token_price + completion_tokens * completion_token_price
    ) / 1000.0
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )


def _is_timeout_error(exc: URLError) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(reason or exc).lower()
