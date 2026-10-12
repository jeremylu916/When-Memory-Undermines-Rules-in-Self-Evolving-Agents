"""Stdlib client for the AWS Bedrock Converse API with a Bedrock API key.

Mirrors the parts of ``LLMClient`` that ``ModelBackend`` uses: ``chat``
returns an ``LLMResponse`` whose ``raw_response`` carries OpenAI-style
``usage`` / ``choices`` fields, and ``configuration_error`` reports missing
settings. Authentication is ``Authorization: Bearer <key>`` read from an
environment variable; the key is never logged or stored by this module.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .llm import LLMRequestError, LLMResponse, LLMUsage

# Bedrock stop reasons mapped to OpenAI finish_reason names used downstream.
FINISH_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                  "content_filtered": "content_filter", "guardrail_intervened": "content_filter"}


def last_json_object(text: str) -> str | None:
    """The last top-level JSON object in free text (prose around it is dropped), or None."""
    decoder, index, last = json.JSONDecoder(), 0, None
    while (start := text.find("{", index)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            last = obj
        index = end
    return json.dumps(last) if last is not None else None


def strip_json_fence(text: str) -> str:
    """Remove one surrounding ```json fence; the JSON itself is left untouched."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        body = stripped[3:-3]
        if body.lower().startswith("json"):
            body = body[4:]
        return body.strip()
    return stripped


class BedrockClient:
    def __init__(self, config: dict[str, Any]):
        llm = config.get("llm", config)
        self.enabled = bool(llm.get("enabled", True))
        self.model = str(llm.get("model") or "")
        self.region = str(llm.get("region") or "us-east-1")
        self.api_key_env = str(llm.get("api_key_env") or "AWS_BEARER_TOKEN_BEDROCK")
        self.api_key = os.getenv(self.api_key_env, "")
        self.temperature = llm.get("temperature")
        self.additional_fields = llm.get("additional_model_request_fields") or {}
        self.extract_last_json = bool(llm.get("extract_last_json", False))
        self.max_tokens = int(llm.get("max_completion_tokens") or 1024)
        self.timeout_s = int(llm.get("timeout_s") or 120)
        self.retries = max(0, int(llm.get("timeout_retries", 0)))
        self.retry_backoff_s = max(0.0, float(llm.get("retry_backoff_s", 0.0)))
        self.prompt_token_price = float(llm.get("prompt_token_price", 0.0))
        self.completion_token_price = float(llm.get("completion_token_price", 0.0))
        self.strip_fence = bool(llm.get("strip_json_fence", True))
        # "tool": force one tool call with an open object schema, the Converse
        # equivalent of JSON mode (models that otherwise prepend prose).
        self.structured_output = str(llm.get("structured_output") or "text")
        # Optional JSON schema for the forced tool's input; the default accepts any
        # object. A schema requiring at least one property stops models that would
        # otherwise return an empty tool input.
        self.tool_schema = llm.get("tool_input_schema") or {"type": "object"}
        if self.structured_output not in {"text", "tool", "tool_wrapped"}:
            raise ValueError("structured_output must be text, tool, or tool_wrapped")
        if self.structured_output == "tool_wrapped":  # one required string field holding the JSON answer
            self.tool_schema = {"type": "object", "required": ["json"], "properties": {"json": {
                "type": "string", "description": "the complete JSON object requested by the instructions, serialized"}}}

    def configuration_error(self) -> str | None:
        if not self.enabled:
            return "llm.enabled is false"
        if not self.model:
            return "llm.model is empty"
        if not self.api_key:
            return f"environment variable {self.api_key_env} is not exported"
        return None

    def chat(self, messages: list[dict[str, str]], max_completion_tokens: int | None = None) -> LLMResponse:
        system = [{"text": m["content"]} for m in messages if m["role"] == "system"]
        turns = [{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages if m["role"] != "system"]
        body: dict[str, Any] = {"messages": turns,
                                "inferenceConfig": {"maxTokens": int(max_completion_tokens or self.max_tokens)}}
        if system:
            body["system"] = system
        if self.temperature is not None:
            body["inferenceConfig"]["temperature"] = self.temperature
        if self.additional_fields:  # e.g. reasoning effort for models without temperature
            body["additionalModelRequestFields"] = self.additional_fields
        if self.structured_output in {"tool", "tool_wrapped"}:
            body["toolConfig"] = {"tools": [{"toolSpec": {
                "name": "respond", "description": "Return the complete JSON object requested by the instructions.",
                "inputSchema": {"json": self.tool_schema}}}], "toolChoice": {"tool": {"name": "respond"}}}
        url = (f"https://bedrock-runtime.{self.region}.amazonaws.com/model/"
               f"{quote(self.model, safe='')}/converse")
        request = Request(url, data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json",
                                   "Authorization": f"Bearer {self.api_key}"})
        attempts: list[dict[str, Any]] = []
        raw = None
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_s) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                # v18: a reply with no content and no input tokens was never processed (seen on
                # GPT-6.1); retry it like a 503. Content-filter stops carry input tokens and are kept.
                if (not raw.get("output", {}).get("message", {}).get("content")
                        and int(raw.get("usage", {}).get("inputTokens", 0)) == 0):
                    attempts.append({"attempt": attempt + 1, "status": "empty_unprocessed"})
                    raw = None
                    if attempt >= self.retries:
                        raise LLMRequestError("Bedrock returned an empty, unprocessed reply", category="empty_reply",
                                              retryable=True, request_attempts=attempts)
                else:
                    attempts.append({"attempt": attempt + 1, "status": "success"})
                    break
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                attempts.append({"attempt": attempt + 1, "status": "http_error", "http_status": exc.code})
                transient = exc.code == 429 or exc.code >= 500
                if not transient or attempt >= self.retries:
                    raise LLMRequestError(f"Bedrock HTTP error {exc.code}: {detail}", category="http_error",
                                          retryable=transient, request_attempts=attempts) from exc
            except (TimeoutError, socket.timeout, URLError, ConnectionError, ssl.SSLError,
                    http.client.IncompleteRead) as exc:
                attempts.append({"attempt": attempt + 1, "status": "connection_error"})
                if attempt >= self.retries:
                    raise LLMRequestError(f"Bedrock connection error: {type(exc).__name__}",
                                          category="connection_error", retryable=True,
                                          request_attempts=attempts) from exc
            if self.retry_backoff_s:
                time.sleep(self.retry_backoff_s * (attempt + 1))
        if raw is None:
            raise LLMRequestError("Bedrock request failed without a response", category="unknown",
                                  retryable=False, request_attempts=attempts)
        blocks = raw.get("output", {}).get("message", {}).get("content", [])
        tool_inputs = [block["toolUse"]["input"] for block in blocks if "toolUse" in block]
        if self.structured_output == "tool" and tool_inputs:
            content = json.dumps(tool_inputs[0])
        elif self.structured_output == "tool_wrapped" and tool_inputs:
            inner = tool_inputs[0].get("json", "")
            content = strip_json_fence(inner) if isinstance(inner, str) else json.dumps(inner)
        else:
            text = "".join(block.get("text", "") for block in blocks if "text" in block)
            content = strip_json_fence(text) if self.strip_fence else text
            if self.extract_last_json:  # v15: models that write a sentence before the JSON answer
                content = last_json_object(text) or content
        usage_raw = raw.get("usage", {})
        prompt, completion = int(usage_raw.get("inputTokens", 0)), int(usage_raw.get("outputTokens", 0))
        cost = (prompt * self.prompt_token_price + completion * self.completion_token_price) / 1000
        openai_style = {"model": self.model,
                        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                  "total_tokens": prompt + completion},
                        "choices": [{"finish_reason": FINISH_REASONS.get(raw.get("stopReason"),
                                                                          raw.get("stopReason"))}],
                        "bedrock": {"stopReason": raw.get("stopReason"), "metrics": raw.get("metrics")}}
        return LLMResponse(content=content, raw_response=openai_style,
                           usage=LLMUsage(prompt, completion, prompt + completion, cost),
                           model=self.model, request_attempts=tuple(attempts))
