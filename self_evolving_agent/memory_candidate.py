"""M6 Memory-2 candidate schema for scoped prompt-evolution memory.

Memory candidates are sanitized learning proposals. They sit between raw
root-cause artifacts and the later scoped operation engine, so this module is
pure, deterministic, and intentionally does not implement extraction or
retrieval.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


MEMORY_CANDIDATE_SCHEMA_VERSION = "m6.2.memory_candidate.v1"

MEMORY_SCOPES = ("task", "repo", "benchmark", "global")
MEMORY_OPERATIONS = (
    "add",
    "reinforce",
    "merge",
    "contradict",
    "narrow_scope",
    "promote_scope",
    "suppress",
)
MEMORY_STATUSES = (
    "candidate",
    "tentative",
    "active",
    "suppressed",
    "contradicted",
)

EVIDENCE_SOURCE_STRENGTHS = {
    "official_harness": 1.0,
    "root_cause_event": 0.8,
    "materializer_trace": 0.7,
    "builder_rejection": 0.7,
    "safety_rejection": 0.8,
    "promotion_rejection": 0.6,
    "promotion_decision": 0.8,
    "optimizer_history_only": 0.3,
}

PROMPT_VISIBILITY_TARGETS = ("optimizer", "patch_generator", "reporter")
DEFAULT_PROMPT_VISIBILITY = {
    "optimizer": False,
    "patch_generator": False,
    "reporter": True,
}

OFFICIAL_FEEDBACK_ALLOWED_FIELDS = (
    "benchmark_id",
    "split_role",
    "total_tasks",
    "resolved_count",
    "unresolved_count",
    "fail_to_pass_failed_count",
    "pass_to_pass_regression_count",
    "empty_patch_count",
    "harness_error_count",
    "root_cause_histogram",
    "root_cause_histograms",
    "patch_generation_stats",
    "prompt_update_feedback_summary",
    "prompt_update_rejection_reasons",
    "rejected_update_fingerprints",
    "do_not_repeat_summary",
)

FORBIDDEN_PROMPT_FIELD_MARKERS = (
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


class MemoryCandidateValidationError(ValueError):
    """Raised when a memory candidate violates the frozen Memory-2a schema."""


def build_memory_candidate(
    *,
    run_id: str,
    round_index: int,
    task_ids: list[str] | tuple[str, ...],
    repo_ids: list[str] | tuple[str, ...],
    benchmark_id: str,
    source_events: list[str] | tuple[str, ...],
    evidence: dict[str, Any],
    hypothesis: dict[str, Any],
    decision: dict[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build and validate a sanitized memory candidate payload."""

    evidence_block, blocked_fields = _normalize_evidence(evidence)
    hypothesis_block = _normalize_hypothesis(hypothesis)
    decision_block = _normalize_decision(decision)
    decision_block["blocked_prompt_fields"] = sorted(
        {
            *decision_block.get("blocked_prompt_fields", []),
            *blocked_fields,
        }
    )

    candidate = {
        "schema_version": MEMORY_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": "",
        "created_at": created_at or _utc_now(),
        "run_id": str(run_id or ""),
        "round_index": int(round_index),
        "task_ids": _string_list(task_ids),
        "repo_ids": _string_list(repo_ids),
        "benchmark_id": str(benchmark_id or ""),
        "source_events": _string_list(source_events),
        "evidence": evidence_block,
        "hypothesis": hypothesis_block,
        "decision": decision_block,
    }
    candidate["candidate_id"] = memory_candidate_id(candidate)
    validate_memory_candidate(candidate)
    return candidate


def validate_memory_candidate(candidate: dict[str, Any]) -> None:
    """Validate a Memory-2a candidate payload."""

    if not isinstance(candidate, dict):
        raise MemoryCandidateValidationError("candidate must be a mapping")

    for key in (
        "schema_version",
        "candidate_id",
        "created_at",
        "run_id",
        "round_index",
        "task_ids",
        "repo_ids",
        "benchmark_id",
        "source_events",
        "evidence",
        "hypothesis",
        "decision",
    ):
        if key not in candidate:
            raise MemoryCandidateValidationError(f"missing top-level field: {key}")

    if candidate["schema_version"] != MEMORY_CANDIDATE_SCHEMA_VERSION:
        raise MemoryCandidateValidationError("unsupported candidate schema_version")
    if not isinstance(candidate["candidate_id"], str) or not candidate["candidate_id"]:
        raise MemoryCandidateValidationError("candidate_id must be a non-empty string")
    if candidate["candidate_id"] != memory_candidate_id(candidate):
        raise MemoryCandidateValidationError("candidate_id does not match payload fingerprint")
    if not isinstance(candidate["created_at"], str) or not candidate["created_at"]:
        raise MemoryCandidateValidationError("created_at must be a non-empty string")
    if not isinstance(candidate["run_id"], str):
        raise MemoryCandidateValidationError("run_id must be a string")
    if not isinstance(candidate["round_index"], int) or candidate["round_index"] < 0:
        raise MemoryCandidateValidationError("round_index must be a non-negative integer")
    if not isinstance(candidate["benchmark_id"], str):
        raise MemoryCandidateValidationError("benchmark_id must be a string")
    _validate_string_list(candidate["task_ids"], "task_ids")
    _validate_string_list(candidate["repo_ids"], "repo_ids")
    _validate_string_list(candidate["source_events"], "source_events")

    _validate_evidence(candidate["evidence"])
    _validate_hypothesis(candidate["hypothesis"])
    _validate_decision(candidate["decision"])


def candidate_fingerprint(candidate: dict[str, Any]) -> str:
    """Return a deterministic fingerprint, stable across mapping key order."""

    payload = _jsonable(candidate)
    if isinstance(payload, dict):
        payload.pop("candidate_id", None)
        payload.pop("created_at", None)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def memory_candidate_id(candidate: dict[str, Any]) -> str:
    """Return the deterministic candidate id for a candidate-like payload."""

    return f"memcand_{candidate_fingerprint(candidate)[:20]}"


def sanitize_official_feedback(feedback: dict[str, Any]) -> dict[str, Any]:
    """Return optimizer-safe official feedback fields only."""

    sanitized, _blocked = _sanitize_official_feedback_with_blocked(
        feedback,
        "official_feedback",
    )
    return sanitized


def sanitize_candidate_for_prompt(
    candidate: dict[str, Any],
    *,
    target: str = "optimizer",
) -> dict[str, Any]:
    """Return a prompt-visible candidate copy for the requested target.

    If the decision block does not allow the target, an empty mapping is
    returned. The stored candidate is not mutated.
    """

    validate_memory_candidate(candidate)
    if target not in PROMPT_VISIBILITY_TARGETS:
        raise MemoryCandidateValidationError(f"unknown prompt visibility target: {target}")

    visibility = candidate["decision"]["allowed_prompt_visibility"]
    if not visibility[target]:
        return {}

    sanitized = _jsonable(deepcopy(candidate))
    official, official_blocked = _sanitize_official_feedback_with_blocked(
        sanitized["evidence"].get("official_feedback_summary", {}),
        "evidence.official_feedback_summary",
    )
    sanitized["evidence"]["official_feedback_summary"] = official
    payload, blocked = _sanitize_payload_with_blocked(sanitized, "")
    sanitized = payload if isinstance(payload, dict) else {}

    blocked_fields = sorted(
        {
            *candidate["decision"].get("blocked_prompt_fields", []),
            *official_blocked,
            *blocked,
        }
    )
    sanitized["decision"]["blocked_prompt_fields"] = blocked_fields
    for field_path in blocked_fields:
        _remove_field_path(sanitized, field_path)
    return sanitized


def _normalize_evidence(evidence: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    evidence = evidence if isinstance(evidence, dict) else {}
    source_type = str(evidence.get("source_type") or "")
    evidence_strength = evidence.get("evidence_strength")
    if evidence_strength is None and source_type in EVIDENCE_SOURCE_STRENGTHS:
        evidence_strength = EVIDENCE_SOURCE_STRENGTHS[source_type]

    official, blocked_fields = _sanitize_official_feedback_with_blocked(
        evidence.get("official_feedback_summary", {}),
        "evidence.official_feedback_summary",
    )
    materializer_summary, materializer_blocked = _sanitize_payload_with_blocked(
        evidence.get("materializer_summary", {}),
        "evidence.materializer_summary",
    )
    prompt_feedback, prompt_blocked = _sanitize_payload_with_blocked(
        evidence.get("prompt_update_feedback", {}),
        "evidence.prompt_update_feedback",
    )
    cost_summary, cost_blocked = _sanitize_payload_with_blocked(
        evidence.get("cost_summary", {}),
        "evidence.cost_summary",
    )

    return (
        {
            "source_type": source_type,
            "evidence_strength": evidence_strength,
            "official_feedback_summary": official,
            "root_cause_events": _jsonable(_as_list(evidence.get("root_cause_events"))),
            "materializer_summary": materializer_summary,
            "prompt_update_feedback": prompt_feedback,
            "cost_summary": cost_summary,
            "contamination_risk": _jsonable(evidence.get("contamination_risk", "none")),
        },
        sorted({*blocked_fields, *materializer_blocked, *prompt_blocked, *cost_blocked}),
    )


def _normalize_hypothesis(hypothesis: dict[str, Any]) -> dict[str, Any]:
    hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
    return {
        "normalized_lesson": str(hypothesis.get("normalized_lesson") or ""),
        "root_cause": str(hypothesis.get("root_cause") or ""),
        "failure_type": str(hypothesis.get("failure_type") or ""),
        "prompt_module": str(hypothesis.get("prompt_module") or ""),
        "expected_effect": str(hypothesis.get("expected_effect") or ""),
        "scope_hypothesis": str(hypothesis.get("scope_hypothesis") or ""),
        "confidence": hypothesis.get("confidence"),
    }


def _normalize_decision(decision: dict[str, Any]) -> dict[str, Any]:
    decision = decision if isinstance(decision, dict) else {}
    visibility = decision.get("allowed_prompt_visibility")
    if isinstance(visibility, dict):
        visibility = {str(key): value for key, value in visibility.items()}
    return {
        "operation": str(decision.get("operation") or ""),
        "target_scope": str(decision.get("target_scope") or ""),
        "target_scope_id": str(decision.get("target_scope_id") or ""),
        "status": str(decision.get("status") or ""),
        "rationale": str(decision.get("rationale") or ""),
        "allowed_prompt_visibility": visibility,
        "blocked_prompt_fields": _string_list(decision.get("blocked_prompt_fields", [])),
        "evidence_requirements_met": decision.get("evidence_requirements_met"),
    }


def _validate_evidence(evidence: Any) -> None:
    if not isinstance(evidence, dict):
        raise MemoryCandidateValidationError("evidence must be a mapping")
    for key in (
        "source_type",
        "evidence_strength",
        "official_feedback_summary",
        "root_cause_events",
        "materializer_summary",
        "prompt_update_feedback",
        "cost_summary",
        "contamination_risk",
    ):
        if key not in evidence:
            raise MemoryCandidateValidationError(f"missing evidence field: {key}")
    source_type = evidence["source_type"]
    if source_type not in EVIDENCE_SOURCE_STRENGTHS:
        raise MemoryCandidateValidationError(f"invalid evidence source_type: {source_type}")
    try:
        strength = float(evidence["evidence_strength"])
    except (TypeError, ValueError) as exc:
        raise MemoryCandidateValidationError("evidence_strength must be numeric") from exc
    expected = EVIDENCE_SOURCE_STRENGTHS[source_type]
    if abs(strength - expected) > 1e-9:
        raise MemoryCandidateValidationError(
            f"evidence_strength for {source_type} must be {expected}"
        )
    if not isinstance(evidence["official_feedback_summary"], dict):
        raise MemoryCandidateValidationError("official_feedback_summary must be a mapping")
    _assert_no_forbidden_fields(evidence["official_feedback_summary"], "official_feedback_summary")
    if not isinstance(evidence["root_cause_events"], list):
        raise MemoryCandidateValidationError("root_cause_events must be a list")
    for key in ("materializer_summary", "prompt_update_feedback", "cost_summary"):
        if not isinstance(evidence[key], dict):
            raise MemoryCandidateValidationError(f"{key} must be a mapping")


def _validate_hypothesis(hypothesis: Any) -> None:
    if not isinstance(hypothesis, dict):
        raise MemoryCandidateValidationError("hypothesis must be a mapping")
    for key in (
        "normalized_lesson",
        "root_cause",
        "failure_type",
        "prompt_module",
        "expected_effect",
        "scope_hypothesis",
        "confidence",
    ):
        if key not in hypothesis:
            raise MemoryCandidateValidationError(f"missing hypothesis field: {key}")
    for key in (
        "normalized_lesson",
        "root_cause",
        "failure_type",
        "prompt_module",
        "expected_effect",
    ):
        if not isinstance(hypothesis[key], str):
            raise MemoryCandidateValidationError(f"{key} must be a string")
    if not hypothesis["normalized_lesson"]:
        raise MemoryCandidateValidationError("normalized_lesson must be non-empty")
    if hypothesis["scope_hypothesis"] not in MEMORY_SCOPES:
        raise MemoryCandidateValidationError(
            f"invalid scope_hypothesis: {hypothesis['scope_hypothesis']}"
        )
    _validate_confidence(hypothesis["confidence"], "hypothesis.confidence")


def _validate_decision(decision: Any) -> None:
    if not isinstance(decision, dict):
        raise MemoryCandidateValidationError("decision must be a mapping")
    for key in (
        "operation",
        "target_scope",
        "target_scope_id",
        "status",
        "rationale",
        "allowed_prompt_visibility",
        "blocked_prompt_fields",
        "evidence_requirements_met",
    ):
        if key not in decision:
            raise MemoryCandidateValidationError(f"missing decision field: {key}")
    if decision["operation"] not in MEMORY_OPERATIONS:
        raise MemoryCandidateValidationError(f"invalid operation: {decision['operation']}")
    if decision["target_scope"] not in MEMORY_SCOPES:
        raise MemoryCandidateValidationError(f"invalid target_scope: {decision['target_scope']}")
    if decision["status"] not in MEMORY_STATUSES:
        raise MemoryCandidateValidationError(f"invalid status: {decision['status']}")
    if not isinstance(decision["target_scope_id"], str):
        raise MemoryCandidateValidationError("target_scope_id must be a string")
    if not isinstance(decision["rationale"], str):
        raise MemoryCandidateValidationError("rationale must be a string")
    visibility = decision["allowed_prompt_visibility"]
    if not isinstance(visibility, dict):
        raise MemoryCandidateValidationError("allowed_prompt_visibility must be a mapping")
    if set(visibility) != set(PROMPT_VISIBILITY_TARGETS):
        raise MemoryCandidateValidationError(
            "allowed_prompt_visibility must explicitly include optimizer, "
            "patch_generator, and reporter"
        )
    for target, allowed in visibility.items():
        if not isinstance(allowed, bool):
            raise MemoryCandidateValidationError(
                f"allowed_prompt_visibility.{target} must be a bool"
            )
    _validate_string_list(decision["blocked_prompt_fields"], "blocked_prompt_fields")
    if not isinstance(decision["evidence_requirements_met"], bool):
        raise MemoryCandidateValidationError("evidence_requirements_met must be a bool")


def _sanitize_official_feedback_with_blocked(
    feedback: Any,
    prefix: str,
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(feedback, dict):
        return {}, []

    allowed = {_normalize_key(key) for key in OFFICIAL_FEEDBACK_ALLOWED_FIELDS}
    sanitized: dict[str, Any] = {}
    blocked: list[str] = []
    for key, value in sorted(feedback.items(), key=lambda pair: str(pair[0])):
        key_text = str(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        if _is_forbidden_key(key_text):
            blocked.append(path)
            continue
        if _normalize_key(key_text) not in allowed:
            continue
        safe_value, nested_blocked = _sanitize_payload_with_blocked(value, path)
        sanitized[key_text] = safe_value
        blocked.extend(nested_blocked)
    return sanitized, sorted(set(blocked))


def _sanitize_payload_with_blocked(value: Any, prefix: str) -> tuple[Any, list[str]]:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        blocked: list[str] = []
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if _is_forbidden_key(key_text):
                blocked.append(path)
                continue
            safe_item, nested_blocked = _sanitize_payload_with_blocked(item, path)
            sanitized[key_text] = safe_item
            blocked.extend(nested_blocked)
        return sanitized, sorted(set(blocked))
    if isinstance(value, (list, tuple)):
        sanitized_items: list[Any] = []
        blocked: list[str] = []
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            safe_item, nested_blocked = _sanitize_payload_with_blocked(item, path)
            sanitized_items.append(safe_item)
            blocked.extend(nested_blocked)
        return sanitized_items, sorted(set(blocked))
    if isinstance(value, str):
        return _redact_text(value), []
    return _jsonable(value), []


def _assert_no_forbidden_fields(value: Any, prefix: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _is_forbidden_key(str(key)):
                raise MemoryCandidateValidationError(f"forbidden prompt field: {path}")
            _assert_no_forbidden_fields(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_forbidden_fields(item, f"{prefix}[{index}]")


def _remove_field_path(payload: dict[str, Any], field_path: str) -> None:
    parts = [part for part in str(field_path).split(".") if part]
    if not parts:
        return
    cursor: Any = payload
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return
        cursor = cursor[part]
    if isinstance(cursor, dict):
        cursor.pop(parts[-1], None)


def _is_forbidden_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(marker in normalized for marker in FORBIDDEN_PROMPT_FIELD_MARKERS)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _validate_string_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list):
        raise MemoryCandidateValidationError(f"{field_name} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise MemoryCandidateValidationError(f"{field_name} must contain only strings")


def _validate_confidence(value: Any, field_name: str) -> None:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise MemoryCandidateValidationError(f"{field_name} must be numeric") from exc
    if confidence < 0.0 or confidence > 1.0:
        raise MemoryCandidateValidationError(f"{field_name} must be between 0 and 1")


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in _as_list(value) if str(item)]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


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


def _redact_text(text: str) -> str:
    redacted = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
