"""Project-side mini-SWE adapter for one-call SMP memory exposure."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from minisweagent.agents.default import DefaultAgent
from minisweagent.models.utils.actions_toolcall import BASH_TOOL

from self_evolving_agent.smp_contracts import (
    EvidenceEvent,
    EvidenceSource,
    EvidenceType,
    LeakageClass,
    TriggerCategory,
)
from self_evolving_agent.smp_runtime import SMPRuntime
from self_evolving_agent.smp_trace_adapter import MiniTraceAdapter
from self_evolving_agent.smp_structured import UsageAccounting, aggregate_usage, conservative_solver_request_cost_usd, normalize_provider_usage
from self_evolving_agent.smp_protocol import FrameworkInvariantError, StructuredExtractionError


PostActionAdapter = Callable[[dict[str, Any], list[dict[str, Any]], SMPRuntime], Iterable[tuple[EvidenceEvent, TriggerCategory | None, str]]]


def _response_visible_text(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    if isinstance(response.get("content"), str):
        return response["content"]
    native = response.get("extra", {}).get("response", {}) if isinstance(response.get("extra"), dict) else {}
    parts = []
    if isinstance(native, dict):
        for item in native.get("output", ()):
            if isinstance(item, dict) and item.get("type") == "message":
                for part in item.get("content", ()):
                    if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        parts.append(part["text"])
    return "\n".join(parts)


class _SMPModelProxy:
    def __init__(
        self, model: Any, runtime: SMPRuntime, accounting_token_counter: Callable[[str], int] | None = None,
        *, solver_pricing: Mapping[str, Any] | None = None, max_completion_tokens: int | None = None,
        cost_limit: float | None = None,
        request_token_counter: Callable[..., int] | None = None,
        usage_persist: Callable[[Sequence[UsageAccounting]], None] | None = None,
    ):
        self._model = model
        self._runtime = runtime
        self._query_index = 0
        self._accounting_token_counter = accounting_token_counter
        self._request_token_counter = request_token_counter
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.usage_records: list[UsageAccounting] = []
        self._solver_pricing = dict(solver_pricing) if solver_pricing is not None else None
        self._max_completion_tokens = max_completion_tokens
        self._cost_limit = cost_limit
        self._usage_persist = usage_persist

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        if self._runtime.state.solver_done:
            raise RuntimeError("completed SMP task cannot issue another model query")
        try:
            pending = self._runtime.state.pending_ephemeral_memory
            query_messages = self._runtime.attach_pending_to_query(
                messages,
                query_index=self._query_index,
                memory_message=(
                    self._model.format_message(role="system", content=pending.block)
                    if pending is not None
                    else None
                ),
            )
            if self._solver_pricing is not None:
                if self._max_completion_tokens is None or self._cost_limit is None:
                    raise FrameworkInvariantError("solver_cost_guard_binding_missing")
                config = getattr(self._model, "config", {})
                model_name = getattr(config, "model_name", None) or (config.get("model_name") if isinstance(config, Mapping) else None)
                model_kwargs = getattr(config, "model_kwargs", None) or (config.get("model_kwargs", {}) if isinstance(config, Mapping) else {})
                request = {
                    "model": model_name,
                    "messages": query_messages,
                    "tools": [BASH_TOOL],
                    **dict(model_kwargs or {}),
                    **kwargs,
                    "max_completion_tokens": self._max_completion_tokens,
                }
                try:
                    worst_case = conservative_solver_request_cost_usd(
                        request, self._solver_pricing, max_completion_tokens=self._max_completion_tokens,
                        token_counter=self._request_token_counter,
                    )
                except StructuredExtractionError as exc:
                    raise FrameworkInvariantError("solver_pricing_contract_invalid", provider_error_code=exc.error_code) from exc
                cumulative_actual = sum(row.cost for row in self.usage_records)
                effective_limit = min(float(self._cost_limit), float(self._solver_pricing["maximum_task_cost"]))
                if cumulative_actual + worst_case > effective_limit + 1e-12:
                    raise FrameworkInvariantError(
                        "solver_cost_cap_would_be_exceeded",
                        cumulative_actual_cost_usd=cumulative_actual,
                        next_call_worst_case_cost_usd=worst_case,
                    )
            self._query_index += 1
            response = self._model.query(query_messages, **kwargs)
            request_text = json.dumps(query_messages, sort_keys=True, ensure_ascii=True)
            output_text = _response_visible_text(response)
            extra = response.get("extra", {}) if isinstance(response, dict) else {}
            native = extra.get("response", response) if isinstance(extra, dict) else response
            native = dict(native) if isinstance(native, dict) else {}
            if isinstance(extra, dict) and isinstance(extra.get("cost"), (int, float)):
                native["cost"] = float(extra["cost"])
            estimator = None
            if self._accounting_token_counter is not None:
                estimator = lambda request, output: (
                    self._accounting_token_counter(request), self._accounting_token_counter(output)
                )
            try:
                usage = normalize_provider_usage(
                    native,
                    require_usage=not self._runtime.test_mode,
                    estimator=estimator,
                    request_text=request_text,
                    output_text=output_text,
                )
            except StructuredExtractionError as exc:
                raise FrameworkInvariantError(
                    "solver_provider_usage_invalid", provider_error_code=exc.error_code,
                    uncertain_paid_response=True,
                ) from exc
            # Durability ordering is part of the paid-call protocol: normalize,
            # atomically persist, then expose the record in memory or return.
            if self._usage_persist is not None:
                self._usage_persist(tuple((*self.usage_records, usage)))
            self.usage_records.append(usage)
            self.calls += 1
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            return response
        finally:
            self._runtime.clear_pending()


class SMPMiniAgent(DefaultAgent):
    """DefaultAgent with a host-only SMP controller and unchanged canonical history."""

    def __init__(self, model: Any, env: Any, *, runtime: SMPRuntime, post_action_adapter: PostActionAdapter | None = None, accounting_token_counter: Callable[[str], int] | None = None, request_token_counter: Callable[..., int] | None = None, solver_pricing: Mapping[str, Any] | None = None, solver_max_completion_tokens: int | None = None, solver_cost_limit: float | None = None, usage_persist: Callable[[Sequence[UsageAccounting]], None] | None = None, **kwargs: Any):
        self.smp_runtime = runtime
        self._post_action_adapter = post_action_adapter or MiniTraceAdapter()
        self._underlying_model = model
        self._smp_model_proxy = _SMPModelProxy(
            model, runtime, accounting_token_counter, solver_pricing=solver_pricing,
            max_completion_tokens=solver_max_completion_tokens, cost_limit=solver_cost_limit,
            request_token_counter=request_token_counter,
            usage_persist=usage_persist,
        )
        super().__init__(model=self._smp_model_proxy, env=env, **kwargs)

    def smp_accounting(self) -> dict[str, Any]:
        summary = aggregate_usage(self._smp_model_proxy.usage_records)
        summary["provider_usage_hashes"] = [row.provider_usage_hash for row in self._smp_model_proxy.usage_records]
        summary["provider_native_usage"] = [dict(row.provider_native_usage) for row in self._smp_model_proxy.usage_records]
        summary["cost_sources"] = [row.cost_source for row in self._smp_model_proxy.usage_records]
        return summary

    def run(self, task: str = "", **kwargs: Any) -> dict:
        result: dict[str, Any] | None = None
        failure: BaseException | None = None
        try:
            self.smp_runtime.add_initial_evidence(task)
            self.smp_runtime.consider_trigger(TriggerCategory.TASK_START)
            result = super().run(task, **kwargs)
            return result
        except BaseException as exc:
            failure = exc
            raise
        finally:
            self.smp_runtime.clear_pending()
            if not self.smp_runtime.state.solver_done:
                termination = EvidenceEvent(
                    task_id=self.smp_runtime.state.task_id,
                    event_index=len(self.smp_runtime.state.evidence),
                    event_type=EvidenceType.SOLVER_TERMINATION,
                    source=EvidenceSource.PUBLIC_CONTEXT,
                    observable=True,
                    leakage_class=LeakageClass.PUBLIC_RUNTIME,
                    payload={
                        "exit_status": (result or {}).get("exit_status", type(failure).__name__ if failure else "unknown")
                    },
                )
                self.smp_runtime.terminate(event=termination)

    def execute_actions(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        observations = super().execute_actions(message)
        try:
            adapted = self._post_action_adapter(message, observations, self.smp_runtime)
            for event, trigger, failure_state_hash in adapted:
                self.smp_runtime.record_evidence(event)
                if trigger is not None:
                    self.smp_runtime.consider_trigger(trigger, failure_state_hash=failure_state_hash)
        except FrameworkInvariantError:
            raise
        except Exception as exc:
            if not self.smp_runtime.test_mode:
                raise FrameworkInvariantError(
                    "unexpected_trace_adapter_failure",
                    adapter_error_class=type(exc).__name__,
                ) from exc
            self.smp_runtime.record_adapter_failure(error_type=type(exc).__name__)
            return observations
        return observations
