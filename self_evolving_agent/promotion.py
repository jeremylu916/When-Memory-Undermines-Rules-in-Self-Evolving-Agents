"""Held-out promotion gate (Gate 2).

Promotion is decided on a held-out improvement that must exceed a noise margin.
Because the agent / critic / optimizer become LLM-driven in M4, a single score
is unreliable, so the gate accepts multiple samples per config and compares the
means with a margin:

    promote  iff  mean(candidate) > mean(current) + margin
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from self_evolving_agent.scoring import EvaluationScore

# Default minimum mean improvement required to promote (absorbs sampling noise).
DEFAULT_PROMOTION_MARGIN = 0.0


@dataclass(frozen=True)
class PromotionDecision:
    accepted: bool
    metric: str
    current_score: float
    candidate_score: float
    reason: str
    margin: float = 0.0
    current_samples: int = 1
    candidate_samples: int = 1
    regression_gate_passed: bool = True
    regression_reasons: tuple[str, ...] = ()
    regressed_tasks: tuple[dict[str, Any], ...] = ()
    regression_metrics: dict[str, int] | None = None


def decide_promotion(
    *,
    current: EvaluationScore | list[EvaluationScore],
    candidate: EvaluationScore | list[EvaluationScore],
    metric: str,
    margin: float = DEFAULT_PROMOTION_MARGIN,
    current_records: list[dict[str, Any]] | None = None,
    candidate_records: list[dict[str, Any]] | None = None,
) -> PromotionDecision:
    current_scores = _as_list(current)
    candidate_scores = _as_list(candidate)

    current_value = _mean_metric(current_scores, metric)
    candidate_value = _mean_metric(candidate_scores, metric)

    regression = evaluate_regression_gate(
        current_records or [],
        candidate_records or [],
    )
    accepted = candidate_value > current_value + margin and regression["passed"]
    if not regression["passed"]:
        reason = "candidate rejected by regression gate: " + "; ".join(
            regression["reasons"]
        )
    elif accepted:
        reason = (
            f"candidate mean improved held-out score by more than margin "
            f"({candidate_value:.3f} > {current_value:.3f} + {margin:.3f})"
        )
    else:
        reason = (
            f"candidate mean did not beat current by the margin "
            f"({candidate_value:.3f} <= {current_value:.3f} + {margin:.3f})"
        )

    return PromotionDecision(
        accepted=accepted,
        metric=metric,
        current_score=current_value,
        candidate_score=candidate_value,
        reason=reason,
        margin=margin,
        current_samples=len(current_scores),
        candidate_samples=len(candidate_scores),
        regression_gate_passed=bool(regression["passed"]),
        regression_reasons=tuple(regression["reasons"]),
        regressed_tasks=tuple(regression["regressed_tasks"]),
        regression_metrics=regression["metrics"],
    )


def evaluate_regression_gate(
    current_records: list[dict[str, Any]],
    candidate_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare official harness outcomes per task and enforce hard regressions."""

    current = {_task_key(item): _task_test_metrics(item) for item in current_records}
    candidate = {_task_key(item): _task_test_metrics(item) for item in candidate_records}
    task_ids = sorted(
        key for key in set(current) & set(candidate) if key[0]
    )
    regressed_tasks: list[dict[str, Any]] = []
    for task_id in task_ids:
        before = current[task_id]
        after = candidate[task_id]
        delta = {
            key: after[key] - before[key]
            for key in (
                "pass_to_pass_regression_count",
                "fail_to_pass_success_count",
                "resolved_count",
            )
        }
        if (
            delta["pass_to_pass_regression_count"] > 0
            or delta["fail_to_pass_success_count"] < 0
            or delta["resolved_count"] < 0
        ):
            regressed_tasks.append(
                {
                    "task_id": task_id[0],
                    "repeat_index": task_id[1],
                    "current": before,
                    "candidate": after,
                    "delta": delta,
                }
            )

    totals = {
        "current_pass_to_pass_regression_count": sum(
            item["pass_to_pass_regression_count"] for item in current.values()
        ),
        "candidate_pass_to_pass_regression_count": sum(
            item["pass_to_pass_regression_count"] for item in candidate.values()
        ),
        "current_fail_to_pass_success_count": sum(
            item["fail_to_pass_success_count"] for item in current.values()
        ),
        "candidate_fail_to_pass_success_count": sum(
            item["fail_to_pass_success_count"] for item in candidate.values()
        ),
        "current_resolved_count": sum(item["resolved_count"] for item in current.values()),
        "candidate_resolved_count": sum(
            item["resolved_count"] for item in candidate.values()
        ),
    }
    reasons: list[str] = []
    if (
        totals["candidate_pass_to_pass_regression_count"]
        > totals["current_pass_to_pass_regression_count"]
    ):
        reasons.append(
            "total PASS_TO_PASS regressions increased "
            f"({totals['current_pass_to_pass_regression_count']} -> "
            f"{totals['candidate_pass_to_pass_regression_count']})"
        )
    catastrophic = [
        item["task_id"]
        for item in regressed_tasks
        if item["delta"]["pass_to_pass_regression_count"] >= 5
    ]
    if catastrophic:
        reasons.append(
            "tasks introduced at least five PASS_TO_PASS regressions: "
            + ", ".join(catastrophic)
        )
    if (
        totals["candidate_fail_to_pass_success_count"]
        < totals["current_fail_to_pass_success_count"]
        and totals["candidate_resolved_count"]
        <= totals["current_resolved_count"]
    ):
        reasons.append(
            "FAIL_TO_PASS successes decreased without an official resolved improvement "
            f"({totals['current_fail_to_pass_success_count']} -> "
            f"{totals['candidate_fail_to_pass_success_count']})"
        )
    return {
        "passed": not reasons,
        "reasons": reasons,
        "regressed_tasks": regressed_tasks,
        "metrics": totals,
    }


def _task_key(record: dict[str, Any]) -> tuple[str, int]:
    task = record.get("task") or {}
    task_id = str(
        record.get("run_id")
        or record.get("task_id")
        or (task.get("id") if isinstance(task, dict) else "")
        or ""
    )
    return task_id, int(record.get("evaluation_repeat", 0) or 0)


def _task_test_metrics(record: dict[str, Any]) -> dict[str, int]:
    harness = record.get("official_harness") or record.get("harness") or {}
    if not isinstance(harness, dict):
        harness = {}
    tests = harness.get("tests_status") or {}
    fail_to_pass = tests.get("FAIL_TO_PASS") or {}
    pass_to_pass = tests.get("PASS_TO_PASS") or {}
    return {
        "pass_to_pass_regression_count": len(pass_to_pass.get("failure") or []),
        "fail_to_pass_success_count": len(fail_to_pass.get("success") or []),
        "resolved_count": int(bool(harness.get("resolved", record.get("success", False)))),
    }


def write_decision(decision: PromotionDecision, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(decision), indent=2, sort_keys=True), encoding="utf-8")


def _as_list(
    scores: EvaluationScore | list[EvaluationScore],
) -> list[EvaluationScore]:
    if isinstance(scores, list):
        if not scores:
            raise ValueError("Promotion requires at least one score sample")
        return scores
    return [scores]


def _mean_metric(scores: list[EvaluationScore], metric: str) -> float:
    values = [_metric_value(score, metric) for score in scores]
    return sum(values) / len(values)


def _metric_value(score: EvaluationScore, metric: str) -> float:
    if metric == "aggregate_score":
        return score.aggregate_score
    if not hasattr(score, metric):
        raise ValueError(f"Unknown promotion metric: {metric}")
    return float(getattr(score, metric))
