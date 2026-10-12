"""Round-level feedback summaries for failed prompt-evolution candidates.

The optimizer needs more than aggregate train diagnosis when a candidate is
rejected. This module turns heldout current-vs-candidate evidence into a compact
negative feedback object that can be stored in the evolution ledger and later
fed into optimizer memory.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


ROUND_FAILURE_FEEDBACK_SCHEMA_VERSION = "m7.failure_feedback.v1"
TASK_TRANSITION_LIMIT = 10


def build_round_failure_feedback(
    *,
    promotion_decision: Any,
    aggregate_metric: str,
    current_score: Any | None,
    candidate_score: Any | None,
    current_records: list[dict[str, Any]] | None = None,
    candidate_records: list[dict[str, Any]] | None = None,
    current_diagnosis: dict[str, Any] | None = None,
    candidate_diagnosis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build optimizer-facing feedback from a heldout promotion comparison."""

    decision = _decision_value(promotion_decision)
    metric_delta = _score_delta(current_score, candidate_score)
    outcome_delta = _outcome_delta(current_score, candidate_score)
    transitions = _task_transitions(
        current_records or [],
        candidate_records or [],
        current_diagnosis=current_diagnosis or {},
        candidate_diagnosis=candidate_diagnosis or {},
    )
    regressed = [
        item
        for item in transitions
        if item.get("transition_kind") in {"success_to_failure", "failure_worsened"}
    ][:TASK_TRANSITION_LIMIT]
    changed_failures = [
        item
        for item in transitions
        if item.get("transition_kind") == "failure_shape_changed"
    ][:TASK_TRANSITION_LIMIT]

    return {
        "schema_version": ROUND_FAILURE_FEEDBACK_SCHEMA_VERSION,
        "promotion": {
            "decision": decision,
            "reason": str(getattr(promotion_decision, "reason", "") or ""),
            "metric": str(
                getattr(promotion_decision, "metric", "") or aggregate_metric
            ),
            "margin": _float_or_none(getattr(promotion_decision, "margin", None)),
            "current_score": _float_or_none(
                getattr(promotion_decision, "current_score", None)
            ),
            "candidate_score": _float_or_none(
                getattr(promotion_decision, "candidate_score", None)
            ),
        },
        "metric_delta": metric_delta,
        "failure_delta": outcome_delta,
        "regressed_tasks": regressed,
        "changed_failure_shapes": changed_failures,
        "do_not_repeat": _do_not_repeat(
            decision=decision,
            metric_delta=metric_delta,
            outcome_delta=outcome_delta,
            regressed_tasks=regressed,
            changed_failures=changed_failures,
        ),
        "strategy_options": [
            "inject_failure_semantics",
            "multi_sample_and_rerank",
            "localization_deepen",
            "suppress_prompt_delta",
            "preserve_current",
        ],
    }


def _decision_value(decision: Any) -> str:
    accepted = getattr(decision, "accepted", None)
    if accepted is True:
        return "promoted"
    if accepted is False:
        reason = str(getattr(decision, "reason", "") or "")
        if "skipped dev evaluation" in reason:
            return "not_evaluated"
        return "rejected"
    if isinstance(decision, str):
        return decision
    return "unknown"


def _score_delta(
    current_score: Any | None,
    candidate_score: Any | None,
) -> dict[str, float]:
    current = _score_mapping(current_score)
    candidate = _score_mapping(candidate_score)
    fields = (
        "task_success_rate",
        "patch_correctness",
        "security_validity",
        "test_pass_rate",
        "cost_adjusted_performance",
        "aggregate_score",
    )
    return {
        field: round(
            float(candidate.get(field, 0.0)) - float(current.get(field, 0.0)),
            6,
        )
        for field in fields
        if field in current or field in candidate
    }


def _outcome_delta(
    current_score: Any | None,
    candidate_score: Any | None,
) -> dict[str, int]:
    current = _score_mapping(current_score).get("outcome_counts", {}) or {}
    candidate = _score_mapping(candidate_score).get("outcome_counts", {}) or {}
    if not isinstance(current, dict):
        current = {}
    if not isinstance(candidate, dict):
        candidate = {}
    keys = sorted(set(current) | set(candidate))
    return {
        str(key): _int_or_zero(candidate.get(key)) - _int_or_zero(current.get(key))
        for key in keys
    }


def _task_transitions(
    current_records: list[dict[str, Any]],
    candidate_records: list[dict[str, Any]],
    *,
    current_diagnosis: dict[str, Any],
    candidate_diagnosis: dict[str, Any],
) -> list[dict[str, str]]:
    current_by_id = {_record_task_id(record): record for record in current_records}
    candidate_by_id = {_record_task_id(record): record for record in candidate_records}
    current_sig = _diagnosis_signatures(current_diagnosis)
    candidate_sig = _diagnosis_signatures(candidate_diagnosis)
    transitions = []
    for task_id in sorted(set(current_by_id) & set(candidate_by_id)):
        before_record = current_by_id[task_id]
        after_record = candidate_by_id[task_id]
        before_success = bool(before_record.get("success", False))
        after_success = bool(after_record.get("success", False))
        before = current_sig.get(task_id) or _record_signature(before_record)
        after = candidate_sig.get(task_id) or _record_signature(after_record)
        kind = _transition_kind(before_success, after_success, before, after)
        if kind == "unchanged":
            continue
        transitions.append(
            {
                "task_id": task_id,
                "before": before,
                "after": after,
                "transition_kind": kind,
            }
        )
    transitions.sort(key=lambda item: (item["transition_kind"], item["task_id"]))
    return transitions[:TASK_TRANSITION_LIMIT]


def _transition_kind(
    before_success: bool,
    after_success: bool,
    before: str,
    after: str,
) -> str:
    if before_success and not after_success:
        return "success_to_failure"
    if not before_success and after_success:
        return "failure_to_success"
    if before_success and after_success:
        return "unchanged"
    if before == after:
        return "unchanged"
    if "length_exhausted" in after and "length_exhausted" not in before:
        return "failure_worsened"
    return "failure_shape_changed"


def _diagnosis_signatures(diagnosis: dict[str, Any]) -> dict[str, str]:
    signatures = {}
    for entry in diagnosis.get("per_task_diagnosis", []) or []:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id") or "")
        if not task_id:
            continue
        signatures[task_id] = _analysis_signature(entry)
    for entry in diagnosis.get("failure_analyses", []) or []:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id") or "")
        if task_id and task_id not in signatures:
            signatures[task_id] = _analysis_signature(entry)
    return signatures


def _analysis_signature(entry: dict[str, Any]) -> str:
    label = str(entry.get("primary_label") or entry.get("category") or "failure")
    subcategories = [
        str(item)
        for item in entry.get("subcategories", []) or []
        if str(item or "").strip()
    ]
    empty_patch_cause = str(entry.get("empty_patch_cause") or "").strip()
    if empty_patch_cause:
        return f"empty_patch:{empty_patch_cause}"
    if subcategories:
        return f"{label}:{'/'.join(subcategories[:3])}"
    return label


def _record_signature(record: dict[str, Any]) -> str:
    if bool(record.get("success", False)):
        return "success"
    status = str(record.get("execution_status") or "failure")
    for call in record.get("llm_calls", []) or []:
        if not isinstance(call, dict):
            continue
        apply_failure = call.get("apply_failure")
        if isinstance(apply_failure, dict):
            known_reason = str(apply_failure.get("known_reason") or "").strip()
            if known_reason:
                return f"{status}:{known_reason}"
    return status


def _do_not_repeat(
    *,
    decision: str,
    metric_delta: dict[str, float],
    outcome_delta: dict[str, int],
    regressed_tasks: list[dict[str, str]],
    changed_failures: list[dict[str, str]],
) -> list[str]:
    cautions = []
    if decision == "rejected":
        cautions.append(
            "Do not repeat rejected prompt wording unless it changes the executable repair process or adds concrete failure semantics."
        )
    if metric_delta.get("test_pass_rate", 0.0) < 0:
        cautions.append(
            "Do not trade lower test_pass_rate for unchanged task_success_rate."
        )
    if outcome_delta.get("patch_failed", 0) > 0:
        cautions.append(
            "Do not repeat updates that increase patch_failed without increasing success_count."
        )
    if any(
        "length_exhausted" in item.get("after", "")
        for item in regressed_tasks + changed_failures
    ):
        cautions.append(
            "Do not add correctness wording that pushes previously non-empty failures into length_exhausted empty patches."
        )
    return cautions[:4]


def _score_mapping(score: Any | None) -> dict[str, Any]:
    if score is None:
        return {}
    if isinstance(score, dict):
        return score
    if is_dataclass(score):
        return asdict(score)
    mapping = {}
    for key in (
        "task_success_rate",
        "patch_correctness",
        "security_validity",
        "test_pass_rate",
        "cost_adjusted_performance",
        "aggregate_score",
        "success_count",
        "outcome_counts",
    ):
        if hasattr(score, key):
            mapping[key] = getattr(score, key)
    return mapping


def _record_task_id(record: dict[str, Any]) -> str:
    task = record.get("task")
    if isinstance(task, dict):
        return str(task.get("id") or task.get("instance_id") or "unknown")
    return str(record.get("task_id") or record.get("instance_id") or "unknown")


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
