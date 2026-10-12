"""M6 Memory-4 pure scoped operation engine.

The engine consumes sanitized Memory-3 candidates and optional active-memory
snapshots, then proposes auditable scoped memory operations. It is intentionally
side-effect free: no persistent store, retrieval, optimizer integration, or
prompt assembly lives here.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable

from self_evolving_agent.memory_candidate import (
    MEMORY_SCOPES,
    candidate_fingerprint,
    validate_memory_candidate,
)


SCOPED_MEMORY_OPERATION_SCHEMA_VERSION = "m6.4.scoped_memory_operation.v1"
ACTIVE_MEMORY_ITEM_SCHEMA_VERSION = "m6.4.active_memory_item.v1"

SCOPED_MEMORY_OPERATIONS = (
    "add",
    "reinforce",
    "merge",
    "contradict",
    "narrow_scope",
    "promote_scope",
    "suppress",
    "pin",
)

ACTIVE_MEMORY_STATUSES = (
    "tentative",
    "active",
    "suppressed",
    "contradicted",
    "pinned",
)

_SCOPE_RANK = {"task": 0, "repo": 1, "benchmark": 2, "global": 3}
_WEAK_ROOT_CAUSES = {
    "unknown",
    "unknown_failure",
    "unknown_apply_failure",
    "api_error",
    "infrastructure_failure",
}
_NEGATIVE_SOURCE_TYPES = {"promotion_rejection", "safety_rejection"}
_FORBIDDEN_PROMPT_FIELD_MARKERS = (
    "gold_patch",
    "gold_diff",
    "reference_patch",
    "test_patch",
    "hidden_test_source",
    "hidden_source",
    "hidden_assertion",
    "assertion_text",
    "hidden_expected_value",
    "hidden_actual_value",
    "expected_output",
    "actual_output",
    "private_traceback",
    "raw_traceback",
    "traceback",
    "official_stdout",
    "official_stderr",
    "raw_stdout",
    "raw_stderr",
    "stdout",
    "stderr",
    "repair_suggestion",
    "suggested_fix",
    "suggested_patch",
    "concrete_repair_suggestion",
    "task_id",
    "task_ids",
    "repo_path",
    "target_files",
    "line_number",
    "line_numbers",
)
_SECRET_PATTERNS = (
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|bearer|token|password|secret)"
            r"(\s*[:=]\s*)([^\s,'\"]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"), "gh_[REDACTED]"),
)


class MemoryOperationValidationError(ValueError):
    """Raised when a scoped memory operation or snapshot is malformed."""


# A promotion gate decides whether a count-based `promote_scope` proposal is
# allowed to climb to a broader scope. It receives the candidate summary and the
# proposed (scope, scope_id), and returns either a bool or an auditable mapping
# with an "allow" key. See `memory_transfer_gate` for the transfer-validated
# implementation. None means "no gate" (legacy count-based behavior).
PromotionGate = Callable[[dict[str, Any], str, str], "bool | dict[str, Any]"]


def build_scoped_memory_operations(
    candidates: list[dict[str, Any]],
    existing_items: list[dict[str, Any]] | None = None,
    *,
    created_at: str | None = None,
    promotion_gate: PromotionGate | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic scoped operation proposals for a candidate batch."""

    working_items = [_jsonable(deepcopy(item)) for item in existing_items or []]
    for item in working_items:
        validate_active_memory_item(item)

    operations: list[dict[str, Any]] = []
    for candidate in candidates or []:
        operation = build_scoped_memory_operation(
            candidate,
            existing_items=working_items,
            created_at=created_at,
            promotion_gate=promotion_gate,
        )
        operations.append(operation)
        after_state = operation.get("after_state")
        if isinstance(after_state, dict) and after_state.get("memory_id"):
            working_items.append(after_state)
    return operations


def build_scoped_memory_operation(
    candidate: dict[str, Any],
    existing_items: list[dict[str, Any]] | None = None,
    *,
    created_at: str | None = None,
    promotion_gate: PromotionGate | None = None,
) -> dict[str, Any]:
    """Build one scoped memory operation proposal from one Memory-3 candidate."""

    validate_memory_candidate(candidate)
    items = [_jsonable(deepcopy(item)) for item in existing_items or []]
    for item in items:
        validate_active_memory_item(item)

    now = created_at or _utc_now()
    summary = _candidate_summary(candidate)
    transfer_gate_audit: dict[str, Any] | None = None
    compatible = _compatible_items(summary, items)
    same_scope = [
        item
        for item in compatible
        if item.get("scope") == summary["base_scope"]
        and item.get("scope_id") == summary["base_scope_id"]
    ]
    conflicts = _conflicting_items(summary, items)
    broad_conflicts = [
        item
        for item in conflicts
        if _SCOPE_RANK.get(str(item.get("scope")), -1)
        > _SCOPE_RANK.get(summary["base_scope"], -1)
    ]

    if summary["negative"] and broad_conflicts:
        operation_type = "narrow_scope"
        targets = broad_conflicts
        target_scope = summary["base_scope"]
        target_scope_id = summary["base_scope_id"]
        after_state = _item_from_candidate(
            candidate,
            summary,
            scope=target_scope,
            scope_id=target_scope_id,
            status="tentative",
            support_items=[],
            contradiction_count=1,
        )
        rationale = (
            "Narrow broad memory because narrower counterevidence contradicts "
            "the existing scoped lesson."
        )
    elif summary["strong_negative"]:
        operation_type = "suppress"
        targets = conflicts[:1]
        target_scope = summary["base_scope"]
        target_scope_id = summary["base_scope_id"]
        after_state = _item_from_candidate(
            candidate,
            summary,
            scope=target_scope,
            scope_id=target_scope_id,
            status="suppressed",
            support_items=targets,
            contradiction_count=_contradiction_count(targets) + 1,
        )
        rationale = (
            "Strong negative evidence should suppress unsafe or rejected scoped "
            "memory rather than reinforce it."
        )
    elif summary["negative"] and conflicts:
        operation_type = "contradict"
        targets = conflicts[:1]
        target_scope = str(targets[0].get("scope") or summary["base_scope"])
        target_scope_id = str(targets[0].get("scope_id") or summary["base_scope_id"])
        after_state = _merge_item_with_candidate(
            targets[0],
            candidate,
            summary,
            status="contradicted",
            contradiction_delta=1,
        )
        rationale = (
            "Candidate evidence contradicts an existing memory item and should "
            "be recorded before further reinforcement."
        )
    elif _pin_eligible(summary, conflicts):
        operation_type = "pin"
        targets = same_scope[:1]
        target_scope = summary["base_scope"]
        target_scope_id = summary["base_scope_id"]
        after_state = _item_from_candidate(
            candidate,
            summary,
            scope=target_scope,
            scope_id=target_scope_id,
            status="pinned",
            support_items=targets,
        )
        rationale = (
            "High-confidence benchmark/global invariant with official support "
            "can be proposed for pinning."
        )
    elif len(same_scope) > 1:
        operation_type = "merge"
        targets = same_scope
        target_scope = summary["base_scope"]
        target_scope_id = summary["base_scope_id"]
        after_state = _item_from_candidate(
            candidate,
            summary,
            scope=target_scope,
            scope_id=target_scope_id,
            status=_merged_status(targets),
            support_items=targets,
            contradiction_count=_contradiction_count(targets),
        )
        rationale = (
            "Multiple compatible memory snapshots at the same scope should be "
            "merged into one auditable memory item."
        )
    else:
        promoted = _promotion_target(summary, compatible)
        if promoted is not None and promotion_gate is not None:
            gate_result = promotion_gate(summary, promoted[0], promoted[1])
            transfer_gate_audit = _normalize_gate_audit(gate_result)
            if not transfer_gate_audit.get("allow", True):
                # Transfer gate rejected the broader scope; fall through to
                # reinforce/add at the current scope. The lesson is kept, not
                # deleted, mirroring scope demotion for prompt updates.
                promoted = None
        if promoted is not None:
            operation_type = "promote_scope"
            targets = compatible
            target_scope, target_scope_id = promoted
            after_state = _item_from_candidate(
                candidate,
                summary,
                scope=target_scope,
                scope_id=target_scope_id,
                status="active",
                support_items=targets,
            )
            rationale = (
                "Repeated compatible evidence supports promotion to a broader "
                "benchmark-aware scope."
            )
        elif len(same_scope) == 1:
            operation_type = "reinforce"
            targets = same_scope
            target_scope = summary["base_scope"]
            target_scope_id = summary["base_scope_id"]
            after_state = _merge_item_with_candidate(
                same_scope[0],
                candidate,
                summary,
                status=_reinforced_status(same_scope[0]),
            )
            rationale = (
                "Compatible evidence at the same scope reinforces an existing "
                "memory item."
            )
        else:
            operation_type = "add"
            targets = []
            target_scope = summary["base_scope"]
            target_scope_id = summary["base_scope_id"]
            after_state = _item_from_candidate(
                candidate,
                summary,
                scope=target_scope,
                scope_id=target_scope_id,
                status="tentative",
                support_items=[],
            )
            rationale = (
                "No compatible memory item exists, so the candidate becomes "
                "new scoped memory evidence."
            )

    operation = {
        "schema_version": SCOPED_MEMORY_OPERATION_SCHEMA_VERSION,
        "operation_id": "",
        "operation": operation_type,
        "target_scope": target_scope,
        "target_scope_id": target_scope_id,
        "candidate_id": summary["candidate_id"],
        "target_memory_ids": _string_list(
            [item.get("memory_id") for item in targets if isinstance(item, dict)]
        ),
        "source_events": summary["source_events"],
        "evidence_summary": _evidence_summary(summary),
        "scope_decision": _scope_decision(
            summary,
            operation=operation_type,
            target_scope=target_scope,
            target_scope_id=target_scope_id,
            compatible_items=compatible,
            conflicting_items=conflicts,
        ),
        "before_state": {"items": [_state_view(item) for item in targets]},
        "after_state": after_state,
        "confidence": _operation_confidence(operation_type, summary, targets),
        "rationale": rationale,
        "safety_flags": _safety_flags(summary),
        "created_at": now,
    }
    if transfer_gate_audit is not None:
        operation["scope_decision"]["transfer_gate"] = transfer_gate_audit
    operation["operation_id"] = scoped_memory_operation_id(operation)
    validate_scoped_memory_operation(operation)
    return operation


def _normalize_gate_audit(result: "bool | dict[str, Any]") -> dict[str, Any]:
    if isinstance(result, dict):
        audit = _jsonable(result)
        audit.setdefault("allow", True)
        return audit
    return {"allow": bool(result)}


def validate_scoped_memory_operation(operation: dict[str, Any]) -> None:
    """Validate a Memory-4 scoped operation proposal."""

    if not isinstance(operation, dict):
        raise MemoryOperationValidationError("operation must be a mapping")
    for key in (
        "schema_version",
        "operation_id",
        "operation",
        "target_scope",
        "target_scope_id",
        "candidate_id",
        "target_memory_ids",
        "source_events",
        "evidence_summary",
        "scope_decision",
        "before_state",
        "after_state",
        "confidence",
        "rationale",
        "safety_flags",
        "created_at",
    ):
        if key not in operation:
            raise MemoryOperationValidationError(f"missing operation field: {key}")
    if operation["schema_version"] != SCOPED_MEMORY_OPERATION_SCHEMA_VERSION:
        raise MemoryOperationValidationError("unsupported operation schema_version")
    if operation["operation"] not in SCOPED_MEMORY_OPERATIONS:
        raise MemoryOperationValidationError("invalid scoped operation")
    if operation["target_scope"] not in MEMORY_SCOPES:
        raise MemoryOperationValidationError("invalid target_scope")
    if not isinstance(operation["target_scope_id"], str):
        raise MemoryOperationValidationError("target_scope_id must be a string")
    _validate_string_list(operation["target_memory_ids"], "target_memory_ids")
    _validate_string_list(operation["source_events"], "source_events")
    _validate_string_list(operation["safety_flags"], "safety_flags")
    for key in ("candidate_id", "operation_id", "rationale", "created_at"):
        if not isinstance(operation[key], str) or not operation[key]:
            raise MemoryOperationValidationError(f"{key} must be a non-empty string")
    for key in ("evidence_summary", "scope_decision", "before_state", "after_state"):
        if not isinstance(operation[key], dict):
            raise MemoryOperationValidationError(f"{key} must be a mapping")
    _validate_confidence(operation["confidence"], "confidence")
    validate_active_memory_item(operation["after_state"])
    if operation["operation_id"] != scoped_memory_operation_id(operation):
        raise MemoryOperationValidationError("operation_id does not match payload")


def validate_active_memory_item(item: dict[str, Any]) -> None:
    """Validate an active-memory snapshot used by the pure operation engine."""

    if not isinstance(item, dict):
        raise MemoryOperationValidationError("active memory item must be a mapping")
    for key in (
        "schema_version",
        "memory_id",
        "scope",
        "scope_id",
        "benchmark_id",
        "root_cause",
        "failure_type",
        "prompt_module",
        "normalized_lesson",
        "expected_effect",
        "status",
        "confidence",
        "support_count",
        "contradiction_count",
        "source_types",
        "source_events",
        "candidate_ids",
        "prompt_injection_view",
        "audit_view",
    ):
        if key not in item:
            raise MemoryOperationValidationError(f"missing active memory field: {key}")
    if item["schema_version"] != ACTIVE_MEMORY_ITEM_SCHEMA_VERSION:
        raise MemoryOperationValidationError("unsupported active memory schema_version")
    if item["scope"] not in MEMORY_SCOPES:
        raise MemoryOperationValidationError("invalid active memory scope")
    if item["status"] not in ACTIVE_MEMORY_STATUSES:
        raise MemoryOperationValidationError("invalid active memory status")
    for key in (
        "memory_id",
        "scope_id",
        "benchmark_id",
        "root_cause",
        "failure_type",
        "prompt_module",
        "normalized_lesson",
        "expected_effect",
    ):
        if not isinstance(item[key], str):
            raise MemoryOperationValidationError(f"{key} must be a string")
    for key in ("source_types", "source_events", "candidate_ids"):
        _validate_string_list(item[key], key)
    for key in ("prompt_injection_view", "audit_view"):
        if not isinstance(item[key], dict):
            raise MemoryOperationValidationError(f"{key} must be a mapping")
    if int(item["support_count"]) < 0 or int(item["contradiction_count"]) < 0:
        raise MemoryOperationValidationError("counts must be non-negative")
    _validate_confidence(item["confidence"], "active memory confidence")
    if item["memory_id"] != active_memory_item_id(item):
        raise MemoryOperationValidationError("memory_id does not match payload")
    _assert_prompt_view_safe(item["prompt_injection_view"])


def scoped_memory_operation_id(operation: dict[str, Any]) -> str:
    """Return a deterministic id for an operation-like payload."""

    payload = _jsonable(deepcopy(operation))
    if isinstance(payload, dict):
        payload.pop("operation_id", None)
        payload.pop("created_at", None)
    return f"memop_{_stable_hash(payload)[:20]}"


def active_memory_item_id(item: dict[str, Any]) -> str:
    """Return a deterministic id for an active memory item-like payload."""

    payload = {
        "schema_version": ACTIVE_MEMORY_ITEM_SCHEMA_VERSION,
        "scope": item.get("scope"),
        "scope_id": item.get("scope_id"),
        "benchmark_id": item.get("benchmark_id"),
        "root_cause": item.get("root_cause"),
        "failure_type": item.get("failure_type"),
        "prompt_module": item.get("prompt_module"),
        "lesson_fingerprint": _lesson_fingerprint(
            str(item.get("normalized_lesson") or "")
        ),
    }
    return f"mem_{_stable_hash(payload)[:20]}"


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    evidence = candidate.get("evidence", {})
    hypothesis = candidate.get("hypothesis", {})
    decision = candidate.get("decision", {})
    official = evidence.get("official_feedback_summary", {})
    prompt_feedback = evidence.get("prompt_update_feedback", {})
    root_events = [
        item for item in evidence.get("root_cause_events", []) if isinstance(item, dict)
    ]
    base_scope, base_scope_id = _candidate_base_scope(candidate)
    root_cause = str(hypothesis.get("root_cause") or "")
    failure_type = str(hypothesis.get("failure_type") or "")
    source_type = str(evidence.get("source_type") or "")
    confidence = _float_or_default(hypothesis.get("confidence"), 0.0)
    pass_to_pass = _int_or_zero(official.get("pass_to_pass_regression_count"))
    fail_to_pass = _int_or_zero(official.get("fail_to_pass_failed_count"))
    weak = _weak_candidate(root_cause, failure_type, root_events)
    negative = (
        source_type in _NEGATIVE_SOURCE_TYPES
        or str(decision.get("operation") or "") == "suppress"
        or pass_to_pass > 0
        or failure_type in {"pass_to_pass_regression", "prompt_update_rejected"}
        or root_cause in {"pass_to_pass_regression", "regression_introduced"}
    )
    reason_strength = str(prompt_feedback.get("reason_strength") or "")
    strong_negative = negative and (
        pass_to_pass > 0
        or source_type == "safety_rejection"
        or reason_strength == "strong"
        or str(decision.get("status") or "") == "active"
    )
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "candidate_fingerprint": f"sha256:{candidate_fingerprint(candidate)[:20]}",
        "source_events": _string_list(candidate.get("source_events", [])),
        "task_ids": _string_list(candidate.get("task_ids", [])),
        "repo_ids": _string_list(candidate.get("repo_ids", [])),
        "benchmark_id": str(candidate.get("benchmark_id") or ""),
        "source_type": source_type,
        "evidence_strength": _float_or_default(evidence.get("evidence_strength"), 0.0),
        "root_cause": root_cause,
        "failure_type": failure_type,
        "prompt_module": str(hypothesis.get("prompt_module") or ""),
        "normalized_lesson": str(hypothesis.get("normalized_lesson") or ""),
        "expected_effect": str(hypothesis.get("expected_effect") or ""),
        "confidence": confidence,
        "base_scope": base_scope,
        "base_scope_id": base_scope_id,
        "requested_operation": str(decision.get("operation") or ""),
        "requested_status": str(decision.get("status") or ""),
        "requested_scope": str(decision.get("target_scope") or ""),
        "requested_scope_id": str(decision.get("target_scope_id") or ""),
        "blocked_prompt_fields": _string_list(decision.get("blocked_prompt_fields", [])),
        "contamination_risk": str(evidence.get("contamination_risk") or ""),
        "pass_to_pass_regression_count": pass_to_pass,
        "fail_to_pass_failed_count": fail_to_pass,
        "prompt_feedback_status": str(prompt_feedback.get("status") or ""),
        "reason_strength": reason_strength,
        "negative": negative,
        "strong_negative": strong_negative,
        "weak": weak,
        "official_support": source_type == "official_harness"
        or bool(official.get("resolved_count"))
        or bool(prompt_feedback.get("official_result_feedback")),
        "root_cause_events": root_events,
    }


def _candidate_base_scope(candidate: dict[str, Any]) -> tuple[str, str]:
    decision = candidate.get("decision", {})
    requested_scope = str(decision.get("target_scope") or "")
    requested_scope_id = str(decision.get("target_scope_id") or "")
    if requested_scope in MEMORY_SCOPES and requested_scope_id:
        return requested_scope, requested_scope_id
    task_ids = _string_list(candidate.get("task_ids", []))
    repo_ids = _string_list(candidate.get("repo_ids", []))
    benchmark_id = str(candidate.get("benchmark_id") or "")
    if len(task_ids) == 1:
        return "task", task_ids[0]
    if len(repo_ids) == 1:
        return "repo", repo_ids[0]
    if benchmark_id:
        return "benchmark", benchmark_id
    return "global", "global"


def _weak_candidate(
    root_cause: str,
    failure_type: str,
    root_events: list[dict[str, Any]],
) -> bool:
    if root_cause in _WEAK_ROOT_CAUSES or failure_type == "infrastructure_failure":
        return True
    for event in root_events:
        envelopes = event.get("evidence_envelopes")
        if not isinstance(envelopes, dict):
            continue
        apply_failure = envelopes.get("apply_failure")
        if isinstance(apply_failure, dict) and apply_failure.get("is_weakly_classified"):
            return True
    return False


def _compatible_items(
    summary: dict[str, Any],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if _semantic_key(item) == _semantic_key(summary)
        and str(item.get("status") or "") not in {"suppressed", "contradicted"}
    ]


def _conflicting_items(
    summary: dict[str, Any],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not summary["negative"]:
        return []
    conflicts: list[dict[str, Any]] = []
    for item in items:
        if str(item.get("status") or "") in {"suppressed", "contradicted"}:
            continue
        same_module = str(item.get("prompt_module") or "") == summary["prompt_module"]
        same_lesson = _semantic_key(item) == _semantic_key(summary)
        pass_to_pass = summary["pass_to_pass_regression_count"] > 0
        if same_lesson or (same_module and pass_to_pass):
            conflicts.append(item)
    return conflicts


def _promotion_target(
    summary: dict[str, Any],
    compatible_items: list[dict[str, Any]],
) -> tuple[str, str] | None:
    if summary["negative"] or summary["weak"]:
        return None
    task_ids = set(summary["task_ids"])
    repo_ids = set(summary["repo_ids"])
    benchmark_ids = {summary["benchmark_id"]} if summary["benchmark_id"] else set()
    for item in compatible_items:
        task_ids.update(_item_task_ids(item))
        repo_ids.update(_item_repo_ids(item))
        if item.get("benchmark_id"):
            benchmark_ids.add(str(item.get("benchmark_id")))

    if len(benchmark_ids) >= 2:
        return ("global", "global")
    if len(repo_ids) >= 2 and summary["benchmark_id"]:
        return ("benchmark", summary["benchmark_id"])
    if (
        len(task_ids) >= 2
        and len(repo_ids) == 1
        and _SCOPE_RANK.get(summary["base_scope"], 0) < _SCOPE_RANK["repo"]
    ):
        return ("repo", sorted(repo_ids)[0])
    return None


def _pin_eligible(
    summary: dict[str, Any],
    conflicts: list[dict[str, Any]],
) -> bool:
    if conflicts or summary["negative"] or summary["weak"]:
        return False
    if summary["base_scope"] not in {"benchmark", "global"}:
        return False
    return summary["official_support"] and summary["confidence"] >= 0.95


def _item_from_candidate(
    candidate: dict[str, Any],
    summary: dict[str, Any],
    *,
    scope: str,
    scope_id: str,
    status: str,
    support_items: list[dict[str, Any]],
    contradiction_count: int | None = None,
) -> dict[str, Any]:
    support_count = 1 + sum(_int_or_zero(item.get("support_count")) for item in support_items)
    contradictions = (
        _contradiction_count(support_items)
        if contradiction_count is None
        else contradiction_count
    )
    confidence = _combined_confidence(summary, support_items, status=status)
    source_types = _dedupe(
        [summary["source_type"]]
        + [
            str(source)
            for item in support_items
            for source in _as_list(item.get("source_types"))
        ]
    )
    source_events = _dedupe(
        summary["source_events"]
        + [
            str(event)
            for item in support_items
            for event in _as_list(item.get("source_events"))
        ]
    )
    candidate_ids = _dedupe(
        [summary["candidate_id"]]
        + [
            str(candidate_id)
            for item in support_items
            for candidate_id in _as_list(item.get("candidate_ids"))
        ]
    )
    item = {
        "schema_version": ACTIVE_MEMORY_ITEM_SCHEMA_VERSION,
        "memory_id": "",
        "scope": scope,
        "scope_id": scope_id,
        "benchmark_id": summary["benchmark_id"],
        "root_cause": summary["root_cause"],
        "failure_type": summary["failure_type"],
        "prompt_module": summary["prompt_module"],
        "normalized_lesson": summary["normalized_lesson"],
        "expected_effect": summary["expected_effect"],
        "status": status,
        "confidence": confidence,
        "support_count": support_count,
        "contradiction_count": contradictions,
        "source_types": source_types,
        "source_events": source_events,
        "candidate_ids": candidate_ids,
        "prompt_injection_view": {},
        "audit_view": {
            "candidate_fingerprints": _dedupe(
                [summary["candidate_fingerprint"]]
                + [
                    str(value)
                    for item in support_items
                    for value in _as_list(
                        _as_dict(item.get("audit_view")).get("candidate_fingerprints")
                    )
                ]
            ),
            "task_ids": _dedupe(
                summary["task_ids"]
                + [
                    str(value)
                    for item in support_items
                    for value in _item_task_ids(item)
                ]
            ),
            "repo_ids": _dedupe(
                summary["repo_ids"]
                + [
                    str(value)
                    for item in support_items
                    for value in _item_repo_ids(item)
                ]
            ),
            "source_events": source_events,
            "evidence_summary": _evidence_summary(summary),
        },
    }
    item["memory_id"] = active_memory_item_id(item)
    item["prompt_injection_view"] = _prompt_injection_view(item, summary)
    validate_active_memory_item(item)
    return item


def _merge_item_with_candidate(
    item: dict[str, Any],
    candidate: dict[str, Any],
    summary: dict[str, Any],
    *,
    status: str,
    contradiction_delta: int = 0,
) -> dict[str, Any]:
    merged = _item_from_candidate(
        candidate,
        summary,
        scope=str(item.get("scope") or summary["base_scope"]),
        scope_id=str(item.get("scope_id") or summary["base_scope_id"]),
        status=status,
        support_items=[item],
        contradiction_count=_int_or_zero(item.get("contradiction_count"))
        + contradiction_delta,
    )
    if status == "reinforced":
        merged["status"] = _reinforced_status(item)
    return merged


def _evidence_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": summary["source_type"],
        "evidence_strength": summary["evidence_strength"],
        "root_cause": summary["root_cause"],
        "failure_type": summary["failure_type"],
        "prompt_module": summary["prompt_module"],
        "candidate_confidence": summary["confidence"],
        "pass_to_pass_regression_count": summary["pass_to_pass_regression_count"],
        "fail_to_pass_failed_count": summary["fail_to_pass_failed_count"],
        "prompt_feedback_status": summary["prompt_feedback_status"],
        "reason_strength": summary["reason_strength"],
        "weak_evidence": summary["weak"],
        "negative_evidence": summary["negative"],
        "contamination_risk": summary["contamination_risk"],
    }


def _scope_decision(
    summary: dict[str, Any],
    *,
    operation: str,
    target_scope: str,
    target_scope_id: str,
    compatible_items: list[dict[str, Any]],
    conflicting_items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "requested_scope": summary["requested_scope"],
        "requested_scope_id": summary["requested_scope_id"],
        "base_scope": summary["base_scope"],
        "base_scope_id": summary["base_scope_id"],
        "selected_scope": target_scope,
        "selected_scope_id": target_scope_id,
        "operation": operation,
        "compatible_memory_count": len(compatible_items),
        "conflicting_memory_count": len(conflicting_items),
        "promotion_blocked": bool(summary["weak"] or summary["negative"]),
        "evidence_priority": _evidence_priority(summary),
    }


def _prompt_injection_view(
    item: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    safe_scope_id = (
        item["scope_id"] if item["scope"] in {"benchmark", "global"} else ""
    )
    view = {
        "memory_id": item["memory_id"],
        "scope": item["scope"],
        "scope_id": safe_scope_id,
        "benchmark_id": item["benchmark_id"] if item["scope"] in {"benchmark", "global"} else "",
        "root_cause": item["root_cause"],
        "prompt_module": item["prompt_module"],
        "normalized_lesson": _redact_text(item["normalized_lesson"]),
        "expected_effect": _redact_text(item["expected_effect"]),
        "confidence": item["confidence"],
        "actionability": _actionability(item, summary),
        "status": item["status"],
        "support_count": item["support_count"],
        "source_types": item["source_types"],
    }
    _assert_prompt_view_safe(view)
    return view


def _state_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": str(item.get("memory_id") or ""),
        "scope": str(item.get("scope") or ""),
        "scope_id": str(item.get("scope_id") or ""),
        "benchmark_id": str(item.get("benchmark_id") or ""),
        "root_cause": str(item.get("root_cause") or ""),
        "prompt_module": str(item.get("prompt_module") or ""),
        "status": str(item.get("status") or ""),
        "confidence": _float_or_default(item.get("confidence"), 0.0),
        "support_count": _int_or_zero(item.get("support_count")),
        "contradiction_count": _int_or_zero(item.get("contradiction_count")),
    }


def _semantic_key(value: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(value.get("root_cause") or ""),
        str(value.get("prompt_module") or ""),
        _lesson_fingerprint(str(value.get("normalized_lesson") or "")),
    )


def _lesson_fingerprint(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest()[:20]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _combined_confidence(
    summary: dict[str, Any],
    support_items: list[dict[str, Any]],
    *,
    status: str,
) -> float:
    values = [summary["confidence"]] + [
        _float_or_default(item.get("confidence"), 0.0) for item in support_items
    ]
    confidence = max(values) if values else 0.0
    if status in {"active", "pinned"}:
        confidence = max(confidence, 0.85)
    if status == "pinned":
        confidence = max(confidence, 0.95)
    if summary["weak"]:
        confidence = min(confidence, 0.35)
    if support_items and status not in {"suppressed", "contradicted"}:
        confidence = min(0.99, max(confidence, max(values) + 0.05))
    return round(max(0.0, min(1.0, confidence)), 4)


def _operation_confidence(
    operation: str,
    summary: dict[str, Any],
    target_items: list[dict[str, Any]],
) -> float:
    values = [summary["confidence"]] + [
        _float_or_default(item.get("confidence"), 0.0) for item in target_items
    ]
    confidence = max(values) if values else summary["confidence"]
    if operation in {"promote_scope", "pin"}:
        confidence = max(confidence, 0.85)
    if operation == "pin":
        confidence = max(confidence, 0.95)
    if operation in {"suppress", "contradict", "narrow_scope"}:
        confidence = max(confidence, 0.8 if summary["strong_negative"] else 0.65)
    if summary["weak"]:
        confidence = min(confidence, 0.35)
    return round(max(0.0, min(1.0, confidence)), 4)


def _reinforced_status(item: dict[str, Any]) -> str:
    if str(item.get("status") or "") == "pinned":
        return "pinned"
    support = _int_or_zero(item.get("support_count")) + 1
    if support >= 2 and str(item.get("status") or "") != "suppressed":
        return "active"
    return str(item.get("status") or "tentative")


def _merged_status(items: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in items}
    if "pinned" in statuses:
        return "pinned"
    if "active" in statuses:
        return "active"
    if "suppressed" in statuses:
        return "suppressed"
    return "tentative"


def _contradiction_count(items: list[dict[str, Any]]) -> int:
    return sum(_int_or_zero(item.get("contradiction_count")) for item in items)


def _item_task_ids(item: dict[str, Any]) -> list[str]:
    audit = _as_dict(item.get("audit_view"))
    values = _string_list(audit.get("task_ids", []))
    if str(item.get("scope") or "") == "task":
        values.append(str(item.get("scope_id") or ""))
    return _dedupe(values)


def _item_repo_ids(item: dict[str, Any]) -> list[str]:
    audit = _as_dict(item.get("audit_view"))
    values = _string_list(audit.get("repo_ids", []))
    if str(item.get("scope") or "") == "repo":
        values.append(str(item.get("scope_id") or ""))
    return _dedupe(values)


def _evidence_priority(summary: dict[str, Any]) -> str:
    if summary["pass_to_pass_regression_count"] > 0:
        return "official_pass_to_pass_regression"
    if summary["fail_to_pass_failed_count"] > 0:
        return "official_fail_to_pass_result"
    if summary["source_type"] in {"promotion_decision", "promotion_rejection"}:
        return "promotion_decision"
    if summary["source_type"] in {"official_harness", "root_cause_event", "materializer_trace"}:
        return "root_cause_evidence"
    return "llm_self_report_or_optimizer_history"


def _safety_flags(summary: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if summary["weak"]:
        flags.append("weak_evidence_no_promotion")
    if summary["negative"]:
        flags.append("negative_evidence")
    if summary["pass_to_pass_regression_count"] > 0:
        flags.append("pass_to_pass_regression_priority")
    if "official" in summary["contamination_risk"].lower():
        flags.append("official_feedback_sanitized")
    if summary["blocked_prompt_fields"]:
        flags.append("candidate_had_blocked_prompt_fields")
    return _dedupe(flags)


def _actionability(item: dict[str, Any], summary: dict[str, Any]) -> str:
    if item["status"] in {"suppressed", "contradicted"}:
        return "negative"
    if summary["weak"]:
        return "low"
    if item["status"] in {"active", "pinned"}:
        return "high"
    return "medium"


def _assert_prompt_view_safe(view: Any, path: str = "") -> None:
    if isinstance(view, dict):
        for key, value in view.items():
            key_text = str(key)
            if _is_forbidden_key(key_text):
                raise MemoryOperationValidationError(
                    f"forbidden prompt view field: {path}.{key_text}".strip(".")
                )
            _assert_prompt_view_safe(value, f"{path}.{key_text}".strip("."))
    elif isinstance(view, list):
        for index, value in enumerate(view):
            _assert_prompt_view_safe(value, f"{path}[{index}]")
    elif isinstance(view, str):
        lowered = view.lower()
        if "traceback" in lowered or "gold_patch" in lowered or "test_patch" in lowered:
            raise MemoryOperationValidationError("forbidden prompt view content")


def _is_forbidden_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
    return any(marker in normalized for marker in _FORBIDDEN_PROMPT_FIELD_MARKERS)


def _redact_text(text: str) -> str:
    redacted = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    if "traceback" in redacted.lower():
        return "[redacted]"
    return redacted


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def _string_list(value: Any) -> list[str]:
    return _dedupe([str(item) for item in _as_list(value) if str(item)])


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _validate_string_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list):
        raise MemoryOperationValidationError(f"{field_name} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise MemoryOperationValidationError(f"{field_name} must contain strings")


def _validate_confidence(value: Any, field_name: str) -> None:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise MemoryOperationValidationError(f"{field_name} must be numeric") from exc
    if confidence < 0.0 or confidence > 1.0:
        raise MemoryOperationValidationError(f"{field_name} must be between 0 and 1")


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
