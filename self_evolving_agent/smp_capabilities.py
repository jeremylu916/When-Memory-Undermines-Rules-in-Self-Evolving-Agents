"""Append-only, cost-accounted capability evidence required before formal v4 calls."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from self_evolving_agent.smp_contracts import canonical_hash
from self_evolving_agent.smp_protocol import FrameworkInvariantError
from self_evolving_agent.smp_structured import DEEPSEEK_DEPLOYMENT, DEEPSEEK_ENDPOINT


@dataclass(frozen=True)
class CapabilityProbePlan:
    ordered_modes: tuple[str, ...] = ("tool_call", "json_schema", "json_object")
    maximum_calls: int = 3
    maximum_cost_usd: float = 0.30
    per_probe_maximum_cost_usd: tuple[float, ...] = (0.10, 0.10, 0.10)
    solver_payload_hash: str = ""
    solver_tool_schema_hash: str = ""
    structured_payload_hash: str = ""
    solver_acceptance_predicate_hash: str = ""
    structured_acceptance_predicate_hash: str = ""
    transport_retry_policy_hash: str = ""
    schema_version: str = "openmemgov.capability_probe_plan.v4"
    plan_hash: str = ""

    def __post_init__(self) -> None:
        hashes = (
            self.solver_payload_hash,
            self.solver_tool_schema_hash,
            self.structured_payload_hash,
            self.solver_acceptance_predicate_hash,
            self.structured_acceptance_predicate_hash,
            self.transport_retry_policy_hash,
        )
        if (
            self.schema_version != "openmemgov.capability_probe_plan.v4"
            or self.ordered_modes != ("tool_call", "json_schema", "json_object")
            or self.maximum_calls != 3
            or isinstance(self.maximum_cost_usd, bool)
            or not isinstance(self.maximum_cost_usd, (int, float))
            or not math.isfinite(float(self.maximum_cost_usd))
            or not 0 < self.maximum_cost_usd <= 3.0
            or len(self.per_probe_maximum_cost_usd) != self.maximum_calls
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value <= 0
                for value in self.per_probe_maximum_cost_usd
            )
            or sum(self.per_probe_maximum_cost_usd) > self.maximum_cost_usd + 1e-12
            or any(not value.startswith("sha256:") for value in hashes)
        ):
            raise FrameworkInvariantError("capability_probe_plan_invalid")
        expected = canonical_hash(self.identity_payload())
        if self.plan_hash and self.plan_hash != expected:
            raise FrameworkInvariantError("capability_probe_plan_hash_mismatch")
        object.__setattr__(self, "plan_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key != "plan_hash"}


def validate_solver_tool_probe_response(response: Mapping[str, Any], *, expected_command: str) -> dict[str, str]:
    """Validate one compatible bash call without returning or executing its command."""
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise FrameworkInvariantError("solver_capability_choice_invalid")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    calls = message.get("tool_calls") if isinstance(message, Mapping) else None
    if message.get("role") != "assistant" or message.get("content") not in {None, ""} or not isinstance(calls, list) or len(calls) != 1:
        raise FrameworkInvariantError("solver_capability_tool_call_invalid")
    call = calls[0]
    function = call.get("function") if isinstance(call, Mapping) else None
    if call.get("type") not in {None, "function"} or not isinstance(function, Mapping) or function.get("name") != "bash" or not isinstance(function.get("arguments"), str):
        raise FrameworkInvariantError("solver_capability_bash_call_invalid")
    try:
        arguments = json.loads(function["arguments"])
    except json.JSONDecodeError as exc:
        raise FrameworkInvariantError("solver_capability_arguments_invalid") from exc
    if not isinstance(arguments, Mapping) or set(arguments) != {"command"} or arguments.get("command") != expected_command:
        raise FrameworkInvariantError("solver_capability_arguments_invalid")
    return {
        "response_hash": canonical_hash(response),
        "tool_call_hash": canonical_hash({"name": "bash", "arguments": arguments}),
    }


def bind_solver_probe_request(
    *,
    bash_tool: Mapping[str, Any],
    expected_tool_schema_hash: str,
    messages: tuple[Mapping[str, Any], ...],
    max_completion_tokens: int,
) -> dict[str, Any]:
    if canonical_hash(bash_tool) != expected_tool_schema_hash:
        raise FrameworkInvariantError("solver_bash_tool_schema_mismatch")
    if not messages:
        raise FrameworkInvariantError("solver_capability_payload_invalid")
    if isinstance(max_completion_tokens, bool) or not isinstance(max_completion_tokens, int) or max_completion_tokens <= 0:
        raise FrameworkInvariantError("solver_capability_token_bound_invalid")
    return {
        "model": "openai/DeepSeek-V4-Pro",
        "messages": [dict(row) for row in messages],
        "tools": [dict(bash_tool)],
        "tool_choice": "required",
        "base_url": DEEPSEEK_ENDPOINT,
        "max_completion_tokens": max_completion_tokens,
    }


@dataclass(frozen=True)
class CapabilityProbeResult:
    probe_id: str
    plan_hash: str
    capability: str
    mode: str
    deployment: str
    endpoint: str
    passed: bool
    failure_code: str
    request_hash: str
    response_hash: str
    provider_request_id_hash: str
    provider_usage_hash: str
    normalized_usage: Mapping[str, Any]
    cost_reservation_id: str
    reconciled_cost_usd: float
    retry_count: int
    retry_policy_hash: str
    acceptance_predicate_hash: str
    schema_version: str = "openmemgov.capability_probe.v4"
    result_hash: str = ""

    def __post_init__(self) -> None:
        allowed_usage = {
            "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens",
            "cost", "accounting_source", "exact", "pricing_hash",
        }
        numeric_usage = {"input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens", "cost"}
        hashes = (
            self.plan_hash, self.request_hash, self.response_hash, self.provider_request_id_hash,
            self.provider_usage_hash, self.retry_policy_hash, self.acceptance_predicate_hash,
        )
        if (
            self.schema_version != "openmemgov.capability_probe.v4"
            or self.capability not in {"solver_tool_call", "structured_output"}
            or self.mode not in {"tool_call", "json_schema", "json_object"}
            or self.deployment != DEEPSEEK_DEPLOYMENT
            or self.endpoint != DEEPSEEK_ENDPOINT
            or self.passed == bool(self.failure_code)
            or not self.cost_reservation_id.startswith("capability:")
            or isinstance(self.reconciled_cost_usd, bool)
            or not isinstance(self.reconciled_cost_usd, (int, float))
            or not math.isfinite(float(self.reconciled_cost_usd))
            or self.reconciled_cost_usd < 0
            or any(not value.startswith("sha256:") for value in hashes)
            or not isinstance(self.normalized_usage, Mapping)
            or not set(self.normalized_usage).issubset(allowed_usage)
            or not {"input_tokens", "output_tokens", "total_tokens", "cost", "accounting_source", "exact", "pricing_hash"}.issubset(self.normalized_usage)
            or any(
                isinstance(self.normalized_usage[name], bool)
                or not isinstance(self.normalized_usage[name], (int, float))
                or not math.isfinite(float(self.normalized_usage[name]))
                or self.normalized_usage[name] < 0
                for name in numeric_usage if name in self.normalized_usage
            )
            or not str(self.normalized_usage.get("pricing_hash", "")).startswith("sha256:")
        ):
            raise FrameworkInvariantError("capability_probe_invalid")
        expected = canonical_hash(self.identity_payload())
        if self.result_hash and self.result_hash != expected:
            raise FrameworkInvariantError("capability_probe_hash_mismatch")
        object.__setattr__(self, "result_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key != "result_hash"}


@dataclass(frozen=True)
class CapabilityRegistry:
    plan: CapabilityProbePlan
    results: tuple[CapabilityProbeResult, ...]
    selected_structured_mode: str
    cost_ledger_hash: str
    schema_version: str = "openmemgov.capability_registry.v4"
    registry_hash: str = ""

    def __post_init__(self) -> None:
        ids = [row.probe_id for row in self.results]
        modes = tuple(row.mode for row in self.results)
        if (
            self.schema_version != "openmemgov.capability_registry.v4"
            or not self.cost_ledger_hash.startswith("sha256:")
            or len(ids) != len(set(ids))
            or len(self.results) > self.plan.maximum_calls
            or any(row.plan_hash != self.plan.plan_hash for row in self.results)
            or modes != self.plan.ordered_modes[: len(modes)]
            or any(row.reconciled_cost_usd > self.plan.per_probe_maximum_cost_usd[index] + 1e-12 for index, row in enumerate(self.results))
            or self.selected_structured_mode not in {"", "json_schema", "json_object"}
        ):
            raise FrameworkInvariantError("capability_registry_invalid")
        expected = canonical_hash(self.identity_payload())
        if self.registry_hash and self.registry_hash != expected:
            raise FrameworkInvariantError("capability_registry_hash_mismatch")
        object.__setattr__(self, "registry_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan": {**self.plan.identity_payload(), "plan_hash": self.plan.plan_hash},
            "results": [{**row.identity_payload(), "result_hash": row.result_hash} for row in self.results],
            "selected_structured_mode": self.selected_structured_mode,
            "cost_ledger_hash": self.cost_ledger_hash,
        }

    def require_gate3(self) -> None:
        solver = any(row.passed and row.capability == "solver_tool_call" and row.mode == "tool_call" for row in self.results)
        selected = self.selected_structured_mode
        structured = any(row.passed and row.capability == "structured_output" and row.mode == selected for row in self.results)
        if not solver or selected not in {"json_schema", "json_object"} or not structured:
            raise FrameworkInvariantError("gate3_capability_evidence_missing")
        if selected == "json_object" and not any(row.mode == "json_schema" and not row.passed for row in self.results):
            raise FrameworkInvariantError("json_object_fallback_without_json_schema_failure")

    def validate_cost_ledger(self, snapshot: Mapping[str, Any]) -> None:
        if snapshot.get("ledger_hash") != self.cost_ledger_hash:
            raise FrameworkInvariantError("capability_cost_ledger_provenance_mismatch")
        reservations = snapshot.get("reservations")
        if not isinstance(reservations, Mapping):
            raise FrameworkInvariantError("capability_cost_ledger_invalid")
        if set(reservations) != {result.cost_reservation_id for result in self.results}:
            raise FrameworkInvariantError("capability_cost_reservation_set_mismatch")
        for result in self.results:
            row = reservations.get(result.cost_reservation_id)
            if (
                not isinstance(row, Mapping)
                or row.get("state") != "reconciled"
                or float(row.get("actual_cost_usd", -1)) != result.reconciled_cost_usd
                or row.get("stage") != "capability_probe"
            ):
                raise FrameworkInvariantError("capability_cost_reservation_mismatch")
        if any(row.get("state") == "reserved" for row in reservations.values() if isinstance(row, Mapping)):
            raise FrameworkInvariantError("preflight_capability_cost_reservation_open")


def load_capability_registry(path: str | Path) -> CapabilityRegistry:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        plan = capability_probe_plan_from_mapping(value["plan"])
        results = tuple(CapabilityProbeResult(**row) for row in value["results"])
        return CapabilityRegistry(
            plan=plan, results=results,
            selected_structured_mode=str(value.get("selected_structured_mode", "")),
            cost_ledger_hash=str(value["cost_ledger_hash"]),
            schema_version=str(value.get("schema_version", "")),
            registry_hash=str(value.get("registry_hash", "")),
        )
    except FrameworkInvariantError:
        raise
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FrameworkInvariantError("capability_registry_invalid") from exc


def capability_probe_plan_from_mapping(value: Mapping[str, Any]) -> CapabilityProbePlan:
    """Load the frozen JSON representation without accepting schema extensions."""
    expected = set(CapabilityProbePlan.__dataclass_fields__)
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FrameworkInvariantError("capability_probe_plan_invalid")
    payload = dict(value)
    for name in ("ordered_modes", "per_probe_maximum_cost_usd"):
        if not isinstance(payload[name], list):
            raise FrameworkInvariantError("capability_probe_plan_invalid")
        payload[name] = tuple(payload[name])
    try:
        return CapabilityProbePlan(**payload)
    except (TypeError, ValueError) as exc:
        raise FrameworkInvariantError("capability_probe_plan_invalid") from exc


def write_capability_registry_exclusive(path: str | Path, registry: CapabilityRegistry) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**registry.identity_payload(), "registry_hash": registry.registry_hash}
    encoded = (json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n").encode()
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        existing = load_capability_registry(target)
        if existing.registry_hash == registry.registry_hash:
            return target
        raise FrameworkInvariantError("capability_registry_append_conflict") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return target


def write_probe_plan_exclusive(path: str | Path, plan: CapabilityProbePlan) -> Path:
    payload = {**plan.identity_payload(), "plan_hash": plan.plan_hash}
    return _write_exclusive_json(Path(path), payload, conflict_code="capability_probe_plan_conflict")


def write_probe_result_exclusive(root: str | Path, result: CapabilityProbeResult) -> Path:
    target = Path(root) / "probe-results" / f"{result.mode}.json"
    payload = {**result.identity_payload(), "result_hash": result.result_hash}
    return _write_exclusive_json(target, payload, conflict_code="capability_probe_result_conflict")


def next_probe_mode(root: str | Path, plan: CapabilityProbePlan) -> str | None:
    result_root = Path(root) / "probe-results"
    completed = []
    for mode in plan.ordered_modes:
        target = result_root / f"{mode}.json"
        if not target.is_file():
            return mode
        row = json.loads(target.read_text(encoding="utf-8"))
        result = CapabilityProbeResult(**row)
        if result.plan_hash != plan.plan_hash or result.mode != mode:
            raise FrameworkInvariantError("capability_probe_resume_mismatch")
        completed.append(result)
        if mode == "tool_call" and not result.passed:
            return None
        if mode == "json_schema" and result.passed:
            return None
    return None


def _write_exclusive_json(path: Path, payload: Mapping[str, Any], *, conflict_code: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_payload = json.loads(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    encoded = (json.dumps(normalized_payload, sort_keys=True, ensure_ascii=True) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as load_exc:
            raise FrameworkInvariantError(conflict_code) from load_exc
        if existing == normalized_payload:
            return path
        raise FrameworkInvariantError(conflict_code) from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return path
