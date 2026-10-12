"""Evaluation scoring for trace records."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvaluationScore:
    task_success_rate: float
    patch_correctness: float
    security_validity: float
    test_pass_rate: float
    cost_adjusted_performance: float
    aggregate_score: float
    task_count: int
    success_count: int = 0
    environment_failure_rate: float = 0.0
    test_failure_rate: float = 0.0
    patch_failure_rate: float = 0.0
    outcome_counts: dict[str, int] = field(default_factory=dict)


def score_records(
    records: list[dict[str, Any]],
    aggregate_metric: str = "task_success_rate",
) -> EvaluationScore:
    if not records:
        return EvaluationScore(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)

    success_rate = _mean(1.0 if record["success"] else 0.0 for record in records)
    patch_correctness = _category_success(records, "software")
    security_validity = _category_success(records, "security")
    test_pass_rate = _mean(record["metrics"].get("test_pass", 0.0) for record in records)
    average_cost = _mean(float(record.get("cost", 0.0)) for record in records)
    cost_adjusted = max(0.0, success_rate - average_cost)
    outcome_counts = _outcome_counts(records)
    environment_failure_rate = outcome_counts.get("environment_failed", 0) / len(records)
    test_failure_rate = outcome_counts.get("test_failed", 0) / len(records)
    patch_failure_rate = outcome_counts.get("patch_failed", 0) / len(records)

    metrics = {
        "task_success_rate": success_rate,
        "patch_correctness": patch_correctness,
        "security_validity": security_validity,
        "test_pass_rate": test_pass_rate,
        "cost_adjusted_performance": cost_adjusted,
    }
    if aggregate_metric not in metrics:
        raise ValueError(f"Unknown aggregate metric: {aggregate_metric}")

    return EvaluationScore(
        task_success_rate=success_rate,
        patch_correctness=patch_correctness,
        security_validity=security_validity,
        test_pass_rate=test_pass_rate,
        cost_adjusted_performance=cost_adjusted,
        aggregate_score=metrics[aggregate_metric],
        task_count=len(records),
        success_count=outcome_counts.get("success", 0),
        environment_failure_rate=environment_failure_rate,
        test_failure_rate=test_failure_rate,
        patch_failure_rate=patch_failure_rate,
        outcome_counts=outcome_counts,
    )


def write_score(score: EvaluationScore, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(score), indent=2, sort_keys=True), encoding="utf-8")


def _category_success(records: list[dict[str, Any]], category: str) -> float:
    matching = [record for record in records if record["task"]["category"] == category]
    if not matching:
        return 0.0
    return _mean(1.0 if record["success"] else 0.0 for record in matching)


def _outcome_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "success": 0,
        "environment_failed": 0,
        "test_failed": 0,
        "patch_failed": 0,
    }
    for record in records:
        status = record.get("execution_status")
        if status is None:
            status = "success" if record["success"] else "patch_failed"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _mean(values: Any) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return sum(materialized) / len(materialized)
