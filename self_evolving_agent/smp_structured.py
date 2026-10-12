"""Dedicated strict structured-output client for OpenMemGov controller calls."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any, Callable, Mapping

from self_evolving_agent.smp_contracts import canonical_hash
from self_evolving_agent.smp_protocol import StructuredExtractionError


DEEPSEEK_DEPLOYMENT = "DeepSeek-V4-Pro"
DEEPSEEK_ENDPOINT = "https://yuexing-4772-resource.services.ai.azure.com/openai/v1"
STRUCTURED_CLIENT_TYPE = "openai_chat_completions"
SOLVER_CLIENT_TYPE = "minisweagent_litellm_chat_completions"
FROZEN_OPENAI_CLIENT_ARGS = {"timeout": 60.0, "max_retries": 0}
ALLOWED_OPENAI_CLIENT_ARGS = frozenset(FROZEN_OPENAI_CLIENT_ARGS)
RETRY_POLICY = {"maximum_transport_retries": 0, "backoff": "none", "semantic_retries": 0}
DEFAULT_MAX_COMPLETION_TOKENS = 3000


def conservative_request_cost_usd(
    request: Mapping[str, Any], pricing_contract: Mapping[str, Any], *, max_completion_tokens: int
) -> float:
    """Upper-bound a call by treating each serialized UTF-8 byte as one input token."""
    try:
        serialized = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        raw_input_rate = pricing_contract["input_per_million"]
        raw_cached_rate = pricing_contract.get("cached_input_per_million", raw_input_rate)
        raw_output_rate = pricing_contract["output_per_million"]
    except KeyError as exc:
        raise StructuredExtractionError("frozen_pricing_contract_missing") from exc
    raw_rates = (raw_input_rate, raw_cached_rate, raw_output_rate)
    if any(
        isinstance(rate, bool) or not isinstance(rate, (int, float))
        or not math.isfinite(float(rate)) or rate < 0
        for rate in raw_rates
    ) or isinstance(max_completion_tokens, bool) or not isinstance(max_completion_tokens, int) or max_completion_tokens <= 0:
        raise StructuredExtractionError("frozen_pricing_contract_invalid")
    input_rate = max(float(raw_input_rate), float(raw_cached_rate))
    output_rate = float(raw_output_rate)
    return (len(serialized) * input_rate + max_completion_tokens * output_rate) / 1_000_000


def conservative_solver_request_cost_usd(
    request: Mapping[str, Any], pricing_contract: Mapping[str, Any], *,
    max_completion_tokens: int, token_counter: Callable[..., int] | None = None,
) -> float:
    """Conservatively price a solver request with its model-bound tokenizer."""
    try:
        model = request["model"]
        messages = request["messages"]
        if not isinstance(model, str) or not model or not isinstance(messages, list):
            raise ValueError("invalid model-bound request")
        if token_counter is None:
            from litellm import token_counter as counter
        else:
            counter = token_counter
        input_tokens = counter(model=model, messages=messages, tools=request.get("tools"))
        input_rate = pricing_contract["input_per_million"]
        cached_rate = pricing_contract.get("cached_input_per_million", input_rate)
        output_rate = pricing_contract["output_per_million"]
    except Exception as exc:
        raise StructuredExtractionError("request_token_estimate_unavailable") from exc
    rates = (input_rate, cached_rate, output_rate)
    if (
        isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0
        or any(isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(float(rate)) or rate < 0 for rate in rates)
        or isinstance(max_completion_tokens, bool) or not isinstance(max_completion_tokens, int) or max_completion_tokens <= 0
    ):
        raise StructuredExtractionError("frozen_pricing_contract_invalid")
    # Add a tokenizer/framing margin and never assume cache savings.
    bounded_input_tokens = math.ceil(input_tokens * 1.05) + 256
    return (
        bounded_input_tokens * float(input_rate)
        + max_completion_tokens * float(output_rate)
    ) / 1_000_000
def _signature_field_schema(*, preconditions: bool) -> dict[str, Any]:
    known_value = (
        {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 120}, "minItems": 1, "maxItems": 12}
        if preconditions else {"type": "string", "minLength": 1, "maxLength": 240}
    )
    refs = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 96}, "minItems": 1, "maxItems": 24}
    base = {"type": "object", "additionalProperties": False, "required": ["value", "status", "evidence_refs"]}
    return {
        "anyOf": [
            {**base, "properties": {"value": known_value, "status": {"type": "string", "enum": [status]}, "evidence_refs": refs}}
            for status in ("observed", "inferred")
        ] + [
            {**base, "properties": {"value": {"type": "null"}, "status": {"type": "string", "enum": ["unknown"]}, "evidence_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 0}}}
        ]
    }
SIGNATURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ownership", "operation", "observed_violation", "required_relation", "preconditions", "intervention"],
    "properties": {
        name: _signature_field_schema(preconditions=name == "preconditions")
        for name in ("ownership", "operation", "observed_violation", "required_relation", "preconditions", "intervention")
    },
}
EXPOSURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mechanism_trigger", "action_guidance", "caution", "scope"],
    "properties": {
        name: {"type": "string", "minLength": 1, "maxLength": 500}
        for name in ("mechanism_trigger", "action_guidance", "caution", "scope")
    },
}
CANDIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["signature", "exposure"],
    "properties": {"signature": SIGNATURE_SCHEMA, "exposure": EXPOSURE_SCHEMA},
}


@dataclass(frozen=True)
class UsageAccounting:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost: float
    accounting_source: str
    exact: bool
    cost_source: str
    pricing_hash: str
    provider_native_usage: Mapping[str, Any]
    provider_usage_hash: str


@dataclass(frozen=True)
class StructuredCallResult:
    payload: Mapping[str, Any]
    raw_response_hash: str
    request_binding_hash: str
    provider_request_id_hash: str
    schema_hash: str
    model_hash: str
    config_hash: str
    prompt_hash: str
    usage: UsageAccounting
    retry_count: int
    retry_policy_hash: str


class OpenAIChatCompletionsProvider:
    """Minimal direct OpenAI-compatible Chat Completions transport."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        environment: Mapping[str, str] | None = None,
        credential_env: str = "OPENAI_API_KEY",
        **client_kwargs: Any,
    ):
        if set(client_kwargs) - ALLOWED_OPENAI_CLIENT_ARGS:
            raise StructuredExtractionError("structured_client_argument_forbidden")
        if any(client_kwargs.get(name, value) != value for name, value in FROZEN_OPENAI_CLIENT_ARGS.items()):
            raise StructuredExtractionError("structured_client_argument_mismatch")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - deployment preflight
                raise StructuredExtractionError("openai_sdk_unavailable") from exc
            import os

            values = os.environ if environment is None else environment
            if credential_env not in values:
                raise StructuredExtractionError("structured_api_credential_missing")
            api_key = values[credential_env]
            frozen = {"base_url": DEEPSEEK_ENDPOINT, "api_key": api_key, **FROZEN_OPENAI_CLIENT_ARGS}
            client = OpenAI(**frozen)
        self.client = client

    def __call__(self, request: Mapping[str, Any]) -> Any:
        forbidden = {"tools", "tool_choice", "api_base", "drop_params", "reasoning", "verbosity", "input", "text"}
        if forbidden & set(request):
            raise StructuredExtractionError("structured_request_parameter_forbidden")
        try:
            return self.client.chat.completions.create(**dict(request))
        except (TimeoutError, ConnectionError) as exc:
            raise StructuredExtractionError("provider_transport_failure", provider_error_class=type(exc).__name__) from exc
        except Exception as exc:
            raise StructuredExtractionError("provider_request_failure", provider_error_class=type(exc).__name__) from exc


class StrictStructuredClient:
    def __init__(
        self,
        *,
        provider: Callable[[Mapping[str, Any]], Any],
        model: str,
        model_config: Mapping[str, Any],
        prompt: str,
        schema_name: str,
        schema: Mapping[str, Any],
        retry_policy: Mapping[str, Any] = RETRY_POLICY,
        require_usage: bool = True,
        tokenizer_estimator: Callable[[str, str], tuple[int, int]] | None = None,
        pricing_contract: Mapping[str, float] | None = None,
        aggregate_cost_cap: float = 3.0,
        response_format_mode: str = "json_schema",
        max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    ):
        if not model or not prompt or schema.get("additionalProperties") is not False:
            raise ValueError("strict structured client binding is incomplete")
        self.provider = provider
        self.model = model
        self.model_config = dict(model_config)
        self.prompt = prompt
        self.schema_name = schema_name
        self.schema = dict(schema)
        self.retry_policy = dict(retry_policy)
        self.require_usage = require_usage
        self.tokenizer_estimator = tokenizer_estimator
        self.pricing_contract = dict(pricing_contract or {})
        required_pricing = ("maximum_call_cost", "input_per_million", "output_per_million")
        if any(
            isinstance(self.pricing_contract.get(name), bool)
            or not isinstance(self.pricing_contract.get(name), (int, float))
            or not math.isfinite(float(self.pricing_contract[name]))
            or self.pricing_contract[name] < 0
            for name in required_pricing
        ) or (
            "cached_input_per_million" in self.pricing_contract
            and (
                isinstance(self.pricing_contract["cached_input_per_million"], bool)
                or not isinstance(self.pricing_contract["cached_input_per_million"], (int, float))
                or not math.isfinite(float(self.pricing_contract["cached_input_per_million"]))
                or self.pricing_contract["cached_input_per_million"] < 0
            )
        ):
            raise ValueError("invalid frozen pricing contract")
        if (
            isinstance(aggregate_cost_cap, bool) or not isinstance(aggregate_cost_cap, (int, float))
            or not math.isfinite(float(aggregate_cost_cap)) or aggregate_cost_cap < 0
        ):
            raise ValueError("invalid aggregate cost cap")
        self.aggregate_cost_cap = float(aggregate_cost_cap)
        if response_format_mode not in {"json_schema", "json_object"}:
            raise ValueError("unsupported frozen structured capability mode")
        self.response_format_mode = response_format_mode
        if isinstance(max_completion_tokens, bool) or not isinstance(max_completion_tokens, int) or max_completion_tokens <= 0:
            raise ValueError("invalid frozen completion token bound")
        maximum_call_cost = self.pricing_contract.get("maximum_call_cost")
        output_rate = self.pricing_contract.get("output_per_million")
        if (
            isinstance(maximum_call_cost, (int, float)) and not isinstance(maximum_call_cost, bool)
            and math.isfinite(float(maximum_call_cost))
            and isinstance(output_rate, (int, float)) and not isinstance(output_rate, bool)
            and math.isfinite(float(output_rate))
            and max_completion_tokens * float(output_rate) / 1_000_000 > float(maximum_call_cost) + 1e-12
        ):
            raise ValueError("completion token bound exceeds frozen call maximum")
        self.max_completion_tokens = max_completion_tokens
        self.cumulative_cost = 0.0
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.usage_records: list[UsageAccounting] = []

    @property
    def binding_config(self) -> dict[str, Any]:
        return {"client_params": dict(self.model_config), "response_format_mode": self.response_format_mode, "max_completion_tokens": self.max_completion_tokens}

    def __call__(self, payload: Mapping[str, Any]) -> StructuredCallResult:
        try:
            return self._call(payload)
        except StructuredExtractionError as exc:
            defaults = {
                "schema_hash": canonical_hash(self.schema),
                "model_hash": canonical_hash({"model": self.model}),
                "config_hash": canonical_hash(self.binding_config),
                "prompt_hash": canonical_hash({"prompt": self.prompt}),
                "retry_count": 0,
                "retry_policy_hash": canonical_hash(self.retry_policy),
            }
            for name, value in defaults.items():
                exc.details.setdefault(name, value)
            raise

    def _call(self, payload: Mapping[str, Any]) -> StructuredCallResult:
        request = self.build_request(payload)
        forbidden = {"tools", "tool_choice", "api_base", "drop_params", "reasoning", "verbosity", "input", "text"}
        if forbidden & set(request):
            raise StructuredExtractionError("structured_request_parameter_forbidden")
        request_hash = canonical_hash(request)
        maximum_call_cost = self.pricing_contract.get("maximum_call_cost")
        if (
            isinstance(maximum_call_cost, bool) or not isinstance(maximum_call_cost, (int, float))
            or not math.isfinite(float(maximum_call_cost)) or maximum_call_cost < 0
        ):
            raise StructuredExtractionError("frozen_pricing_contract_missing")
        worst_case_cost = conservative_request_cost_usd(
            request, self.pricing_contract, max_completion_tokens=self.max_completion_tokens
        )
        if worst_case_cost > float(maximum_call_cost) + 1e-12:
            raise StructuredExtractionError("structured_call_cost_cap_would_be_exceeded", worst_case_cost_usd=worst_case_cost)
        if self.cumulative_cost + worst_case_cost > self.aggregate_cost_cap + 1e-12:
            raise StructuredExtractionError("aggregate_cost_cap_would_be_exceeded")
        self.calls += 1
        try:
            response = self.provider(request)
        except StructuredExtractionError as exc:
            exc.details.setdefault("request_binding_hash", request_hash)
            raise
        except (TimeoutError, ConnectionError) as exc:
            raise StructuredExtractionError(
                "provider_transport_failure",
                provider_error_class=type(exc).__name__,
                request_binding_hash=request_hash,
            ) from exc
        except Exception as exc:
            raise StructuredExtractionError(
                "provider_request_failure",
                provider_error_class=type(exc).__name__,
                request_binding_hash=request_hash,
            ) from exc
        normalized = _plain(response)
        if not isinstance(normalized, Mapping):
            raise StructuredExtractionError("provider_response_not_object", request_binding_hash=request_hash)
        response_hash = canonical_hash(normalized)
        usage = normalize_provider_usage(
            normalized,
            require_usage=self.require_usage,
            estimator=self.tokenizer_estimator,
            request_text=json.dumps(request, sort_keys=True),
            output_text=_best_effort_output_text(normalized),
            pricing_contract=self.pricing_contract,
        )
        self.cumulative_cost += usage.cost
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.usage_records.append(usage)
        if self.cumulative_cost > self.aggregate_cost_cap:
            raise StructuredExtractionError(
                "aggregate_cost_cap_exceeded", response_hash=response_hash,
                request_binding_hash=request_hash, provider_usage_hash=usage.provider_usage_hash,
            )
        try:
            text, request_id = _one_output_text(normalized)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise StructuredExtractionError("malformed_structured_json") from exc
            if not isinstance(parsed, Mapping):
                raise StructuredExtractionError("structured_payload_not_object")
            _validate_schema(parsed, self.schema)
            return StructuredCallResult(
                payload=dict(parsed), raw_response_hash=response_hash, request_binding_hash=request_hash,
                provider_request_id_hash=canonical_hash({"provider_request_id": request_id}) if request_id else canonical_hash({"provider_request_id": None}),
                schema_hash=canonical_hash(self.schema), model_hash=canonical_hash({"model": self.model}),
                config_hash=canonical_hash(self.binding_config), prompt_hash=canonical_hash({"prompt": self.prompt}),
                usage=usage, retry_count=0, retry_policy_hash=canonical_hash(self.retry_policy),
            )
        except StructuredExtractionError as exc:
            exc.details.setdefault("response_hash", response_hash)
            exc.details.setdefault("request_binding_hash", request_hash)
            native_usage = normalized.get("usage")
            if isinstance(native_usage, Mapping):
                exc.details.setdefault("provider_usage_hash", canonical_hash(native_usage))
                exc.details.setdefault("provider_native_usage", _safe_native_usage(native_usage))
            exc.details.setdefault("usage_accounting", asdict(usage))
            raise

    def build_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": json.dumps(payload, sort_keys=True, ensure_ascii=True)},
            ],
            "response_format": (
                {
                    "type": "json_schema",
                    "json_schema": {"name": self.schema_name, "strict": True, "schema": self.schema},
                }
                if self.response_format_mode == "json_schema"
                else {"type": "json_object"}
            ),
            "max_completion_tokens": self.max_completion_tokens,
            **self.model_config,
        }


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    return value


def _best_effort_output_text(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    if isinstance(choices, list):
        return "\n".join(
            str(choice.get("message", {}).get("content", ""))
            for choice in choices
            if isinstance(choice, Mapping) and isinstance(choice.get("message"), Mapping)
        )
    pieces: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
    return "\n".join(pieces) if pieces else json.dumps(response, sort_keys=True, ensure_ascii=True)


def _one_output_text(response: Mapping[str, Any]) -> tuple[str, str]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise StructuredExtractionError("ambiguous_output_messages")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, Mapping) else None
    if not isinstance(message, Mapping):
        raise StructuredExtractionError("malformed_output_content")
    if message.get("role") != "assistant":
        raise StructuredExtractionError("unexpected_output_role")
    if message.get("tool_calls") or message.get("function_call"):
        raise StructuredExtractionError("unexpected_provider_tool_call")
    if message.get("refusal"):
        raise StructuredExtractionError("provider_refusal")
    content = message.get("content")
    if not isinstance(content, str):
        raise StructuredExtractionError("malformed_output_content")
    return content, str(response.get("id", ""))


def normalize_provider_usage(
    response: Mapping[str, Any],
    *,
    require_usage: bool,
    estimator: Callable[[str, str], tuple[int, int]] | None,
    request_text: str,
    output_text: str,
    pricing_contract: Mapping[str, float] | None = None,
) -> UsageAccounting:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        if require_usage or estimator is None:
            raise StructuredExtractionError("provider_usage_missing")
        input_tokens, output_tokens = estimator(request_text, output_text)
        native = {"estimated_input_tokens": input_tokens, "estimated_output_tokens": output_tokens}
        return UsageAccounting(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=0,
            reasoning_tokens=0,
            total_tokens=input_tokens + output_tokens,
            cost=0.0,
            accounting_source="tokenizer_estimate",
            exact=False,
            cost_source="unavailable_estimate",
            pricing_hash=canonical_hash(pricing_contract or {}),
            provider_native_usage=native,
            provider_usage_hash=canonical_hash(native),
        )
    input_tokens = _nonnegative_int(usage.get("input_tokens", usage.get("prompt_tokens")), "input_tokens")
    output_tokens = _nonnegative_int(usage.get("output_tokens", usage.get("completion_tokens")), "output_tokens")
    input_details = usage.get("input_tokens_details", usage.get("prompt_tokens_details", {}))
    output_details = usage.get("output_tokens_details", usage.get("completion_tokens_details", {}))
    cached = _optional_nonnegative_int(input_details.get("cached_tokens", 0) if isinstance(input_details, Mapping) else 0)
    reasoning = _optional_nonnegative_int(output_details.get("reasoning_tokens", 0) if isinstance(output_details, Mapping) else 0)
    total = _optional_nonnegative_int(usage.get("total_tokens", input_tokens + output_tokens))
    if total != input_tokens + output_tokens or cached > input_tokens or reasoning > output_tokens:
        raise StructuredExtractionError("provider_usage_inconsistent")
    cost = response.get("cost", response.get("extra", {}).get("cost") if isinstance(response.get("extra"), Mapping) else None)
    cost_source = "provider_response"
    if cost is None:
        prices = dict(pricing_contract or {})
        if not all(name in prices for name in ("input_per_million", "output_per_million")):
            raise StructuredExtractionError("provider_cost_missing")
        if cached and "cached_input_per_million" not in prices:
            raise StructuredExtractionError("cached_input_price_missing")
        cached_rate = prices.get("cached_input_per_million", prices["input_per_million"])
        rates = (prices["input_per_million"], cached_rate, prices["output_per_million"])
        if any(isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(float(rate)) or rate < 0 for rate in rates):
            raise StructuredExtractionError("frozen_pricing_contract_invalid")
        cost = (
            (input_tokens - cached) * prices["input_per_million"]
            + cached * cached_rate
            + output_tokens * prices["output_per_million"]
        ) / 1_000_000
        cost_source = "frozen_pricing_contract"
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(float(cost)) or cost < 0:
        raise StructuredExtractionError("provider_cost_invalid")
    return UsageAccounting(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=total,
        cost=float(cost),
        accounting_source="provider_native",
        exact=True,
        cost_source=cost_source,
        pricing_hash=canonical_hash(pricing_contract or {}),
        provider_native_usage=_safe_native_usage(usage),
        provider_usage_hash=canonical_hash(usage),
    )


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StructuredExtractionError("provider_usage_invalid", field=name)
    return value


def _optional_nonnegative_int(value: Any) -> int:
    return _nonnegative_int(value, "usage_detail")


def _safe_native_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "input_tokens_details",
        "output_tokens_details",
        "prompt_tokens_details",
        "completion_tokens_details",
    }
    return {name: usage[name] for name in allowed if name in usage}


def aggregate_usage(rows: list[UsageAccounting]) -> dict[str, Any]:
    if not rows:
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0.0, "exact": True}
    exact_values = {row.exact for row in rows}
    if len(exact_values) != 1:
        raise StructuredExtractionError("mixed_exact_and_estimated_accounting")
    return {
        "calls": len(rows),
        "input_tokens": sum(row.input_tokens for row in rows),
        "output_tokens": sum(row.output_tokens for row in rows),
        "cached_input_tokens": sum(row.cached_input_tokens for row in rows),
        "reasoning_tokens": sum(row.reasoning_tokens for row in rows),
        "total_tokens": sum(row.total_tokens for row in rows),
        "cost": sum(row.cost for row in rows),
        "exact": rows[0].exact,
        "accounting_source": rows[0].accounting_source,
    }


def arm_separated_accounting(*, solver: list[UsageAccounting], controller: list[UsageAccounting], extractor: list[UsageAccounting], estimated_memory_visible_tokens: int, tokenizer: str, memory_block_hash: str) -> dict[str, Any]:
    return {
        "solver": aggregate_usage(solver),
        "online_controller": aggregate_usage(controller),
        "post_official_extractor": aggregate_usage(extractor),
        "estimated_memory_visible_tokens": {
            "value": estimated_memory_visible_tokens,
            "exact": False,
            "tokenizer": tokenizer,
            "memory_block_hash": memory_block_hash,
        },
        "total_calls": len(solver) + len(controller) + len(extractor),
        "total_provider_cost": sum(row.cost for row in [*solver, *controller, *extractor]),
    }


def _validate_schema(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                _validate_schema(value, option, path=path)
                return
            except StructuredExtractionError as exc:
                errors.append(exc.error_code)
        raise StructuredExtractionError("structured_schema_mismatch", field=path)
    kind = schema.get("type")
    if kind == "null":
        if value is not None:
            raise StructuredExtractionError("structured_schema_mismatch", field=path)
        return
    if kind == "object":
        if not isinstance(value, Mapping):
            raise StructuredExtractionError("structured_schema_mismatch", field=path)
        required = set(schema.get("required", ()))
        properties = schema.get("properties", {})
        if not required.issubset(value) or (schema.get("additionalProperties") is False and set(value) - set(properties)):
            raise StructuredExtractionError("structured_schema_mismatch", field=path)
        for name, child in value.items():
            if name in properties:
                _validate_schema(child, properties[name], path=f"{path}.{name}")
        return
    if kind == "array":
        if (
            not isinstance(value, list)
            or len(value) > int(schema.get("maxItems", 10**9))
            or len(value) < int(schema.get("minItems", 0))
        ):
            raise StructuredExtractionError("structured_schema_mismatch", field=path)
        for index, child in enumerate(value):
            _validate_schema(child, schema["items"], path=f"{path}[{index}]")
        return
    if kind == "string":
        if not isinstance(value, str) or len(value) > int(schema.get("maxLength", 10**9)) or len(value) < int(schema.get("minLength", 0)):
            raise StructuredExtractionError("structured_schema_mismatch", field=path)
        if "enum" in schema and value not in schema["enum"]:
            raise StructuredExtractionError("structured_schema_mismatch", field=path)
        return
    raise StructuredExtractionError("unsupported_local_schema", field=path)


def safe_rejected_payload(payload: Any) -> dict[str, Any]:
    """Allowlist only bounded known candidate fields; hash all rejected input."""

    result: dict[str, Any] = {"payload_hash": canonical_hash(payload)}
    if not isinstance(payload, Mapping):
        return result
    safe: dict[str, Any] = {}
    exposure = payload.get("exposure")
    if isinstance(exposure, Mapping):
        kept = {
            name + "_hash": canonical_hash(value)
            for name, value in exposure.items()
            if name in EXPOSURE_SCHEMA["properties"] and isinstance(value, str) and len(value) <= 500
        }
        if kept:
            safe["exposure"] = kept
    signature = payload.get("signature")
    if isinstance(signature, Mapping):
        kept_signature = {}
        for name in SIGNATURE_SCHEMA["properties"]:
            field = signature.get(name)
            if not isinstance(field, Mapping):
                continue
            kept_signature[name] = {
                "value_hash": canonical_hash(field.get("value")),
                "status": field.get("status") if field.get("status") in {"observed", "inferred", "unknown"} else "redacted",
                "evidence_ref_hashes": [canonical_hash(ref) for ref in field.get("evidence_refs", ()) if isinstance(ref, str)][:24],
            }
        if kept_signature:
            safe["signature"] = kept_signature
    if safe:
        result["safe_fields"] = safe
    return result
