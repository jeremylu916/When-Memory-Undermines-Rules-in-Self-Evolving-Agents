"""Structured evaluation results shared by M5.6 runners and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from self_evolving_agent.scoring import EvaluationScore


@dataclass
class EvolutionEvaluationResult:
    """One benchmark evaluation with compact audit metadata.

    M5.6 still accepts legacy callbacks returning raw records or an
    ``EvaluationScore``. Adapters can return this richer object when official
    evidence, cache state, and artifact provenance must survive the callback
    boundary.
    """

    score: EvaluationScore
    records: list[dict[str, Any]] = field(default_factory=list)
    diagnosis: dict[str, Any] = field(default_factory=dict)
    artifact_paths: dict[str, str] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    evaluation_id: str = ""
    cache_hit: bool = False
    task_ids: list[str] = field(default_factory=list)
    source: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


def default_evaluation_cost() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }


def normalize_evaluation_cost(value: Any) -> dict[str, Any]:
    cost = default_evaluation_cost()
    if not isinstance(value, dict):
        return cost
    cost["prompt_tokens"] = _int_or_zero(value.get("prompt_tokens"))
    cost["completion_tokens"] = _int_or_zero(value.get("completion_tokens"))
    cost["total_tokens"] = _int_or_zero(value.get("total_tokens"))
    if cost["total_tokens"] == 0:
        cost["total_tokens"] = cost["prompt_tokens"] + cost["completion_tokens"]
    cost["cost_usd"] = _float_or_zero(value.get("cost_usd"))
    return cost


def merge_evaluation_costs(*values: Any) -> dict[str, Any]:
    total = default_evaluation_cost()
    for value in values:
        cost = normalize_evaluation_cost(value)
        total["prompt_tokens"] += cost["prompt_tokens"]
        total["completion_tokens"] += cost["completion_tokens"]
        total["total_tokens"] += cost["total_tokens"]
        total["cost_usd"] = round(total["cost_usd"] + cost["cost_usd"], 10)
    return total


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
