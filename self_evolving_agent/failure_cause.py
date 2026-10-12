"""M6.1 root-cause event schema for benchmark-aware memory.

This module normalizes trace, materializer, and official harness evidence into
one compact event per task. It is intentionally pure and IO-free so later M6
layers can reuse it from evaluators, memory, reports, and optimizer inputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any


FAILURE_CAUSE_EVENT_SCHEMA_VERSION = "m6.1.failure_cause_event.v1"

EMPTY_PATCH_REASONS = (
    "api_error",
    "length_exhausted",
    "structured_edit_anchor_mismatch",
    "structured_edit_parse_failed",
    "git_apply_failure",
    "no_patch_intent",
    "validation_rejected",
    "unknown",
    "not_empty_patch",
)

FAILURE_TYPES = (
    "success",
    "empty_patch",
    "materialization_failed",
    "generation_failed",
    "fail_to_pass_failure",
    "pass_to_pass_regression",
    "infrastructure_failure",
    "validation_rejected",
    "test_failure",
    "unknown_failure",
)

FAILURE_PHASES = (
    "success",
    "generation",
    "materialization",
    "validation",
    "official_harness",
    "infrastructure",
    "unknown",
)

_API_FAILURE_TYPES = {
    "connection_error",
    "http_error",
    "llm_error",
    "timeout",
    "unknown",
}

_APPLY_FAILURE_TYPES = {
    "structured_edit_anchor_mismatch",
    "structured_edit_parse_failed",
    "git_apply_failure",
    "validation_rejected",
    "unknown_apply_failure",
}

_APPLY_FAILURE_KNOWN_REASONS = {
    "structured_edit_anchor_mismatch",
    "structured_edit_parse_failed",
    "git_apply_failure",
    "validation_rejected",
    "unknown",
}

_ANCHOR_MISMATCH_MARKERS = (
    "target text not found",
    "line range guard did not match",
    "exact find",
    "find text is empty",
    "anchor mismatch",
    "could not find",
)

_PARSE_FAILURE_MARKERS = (
    "no structured edits",
    "malformed json",
    "invalid json",
    "parse failed",
)

_NO_PATCH_INTENT_MARKERS = (
    "no patch",
    "no changes needed",
    "nothing to change",
    "already fixed",
    "cannot reproduce",
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


@dataclass(frozen=True)
class FailureCauseEvent:
    schema_version: str = FAILURE_CAUSE_EVENT_SCHEMA_VERSION
    task_id: str = ""
    repo: str = ""
    benchmark: str = ""
    phase: str = "unknown"
    failure_type: str = "unknown_failure"
    empty_patch_reason: str = "not_empty_patch"
    llm_finish_reason: str = ""
    structured_edit_count: int = 0
    structured_edit_files: tuple[str, ...] = field(default_factory=tuple)
    structured_edit_apply_error: str = ""
    patch_chars_before_materialization: int = 0
    patch_chars_after_materialization: int = 0
    retry_count: int = 0
    tokens_per_task: int = 0
    official_fail_to_pass_failures: tuple[str, ...] = field(default_factory=tuple)
    official_pass_to_pass_failures: tuple[str, ...] = field(default_factory=tuple)
    evidence_sources: tuple[str, ...] = field(default_factory=tuple)
    evidence_envelopes: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "structured_edit_files",
            "official_fail_to_pass_failures",
            "official_pass_to_pass_failures",
            "evidence_sources",
        ):
            payload[key] = list(payload[key])
        return payload


def build_failure_cause_event(record: dict[str, Any]) -> dict[str, Any]:
    """Build one normalized root-cause event from a trace or critic record."""

    if not isinstance(record, dict):
        record = {}
    task = _as_dict(record.get("task"))
    official = _official_harness(record)
    llm_calls = [item for item in _as_list(record.get("llm_calls")) if isinstance(item, dict)]
    tool_calls = [item for item in _as_list(record.get("tool_calls")) if isinstance(item, dict)]
    apply_failure = _latest_apply_failure_envelope(record, llm_calls)

    f2p_failures = _official_test_failures(official, "FAIL_TO_PASS")
    p2p_failures = _official_test_failures(official, "PASS_TO_PASS")
    apply_error = _latest_structured_edit_apply_error(llm_calls, tool_calls)
    patch_before = _patch_chars_before_materialization(record, llm_calls, apply_failure)
    patch_after = _patch_chars_after_materialization(record, apply_failure)
    empty_patch = _is_empty_patch(record, official, patch_before, patch_after)
    empty_reason = _infer_empty_patch_reason(
        record=record,
        empty_patch=empty_patch,
        llm_calls=llm_calls,
        apply_error=apply_error,
        apply_failure=apply_failure,
    )
    failure_type = _infer_failure_type(
        record=record,
        empty_patch=empty_patch,
        empty_patch_reason=empty_reason,
        apply_error=apply_error,
        f2p_failures=f2p_failures,
        p2p_failures=p2p_failures,
    )
    phase = _infer_phase(
        record=record,
        failure_type=failure_type,
        empty_patch_reason=empty_reason,
        apply_error=apply_error,
        f2p_failures=f2p_failures,
        p2p_failures=p2p_failures,
    )

    event = FailureCauseEvent(
        task_id=str(task.get("id") or record.get("task_id") or ""),
        repo=_repo_identifier(task, record),
        benchmark=str(task.get("benchmark") or record.get("benchmark") or ""),
        phase=phase,
        failure_type=failure_type,
        empty_patch_reason=empty_reason,
        llm_finish_reason=_latest_text_field(llm_calls, "finish_reason"),
        structured_edit_count=_structured_edit_count(llm_calls, tool_calls),
        structured_edit_files=tuple(_structured_edit_files(llm_calls, tool_calls)),
        structured_edit_apply_error=_redact_text(apply_error),
        patch_chars_before_materialization=patch_before,
        patch_chars_after_materialization=patch_after,
        retry_count=_retry_count(llm_calls),
        tokens_per_task=_tokens_per_task(record, llm_calls),
        official_fail_to_pass_failures=tuple(f2p_failures),
        official_pass_to_pass_failures=tuple(p2p_failures),
        evidence_sources=tuple(
            _evidence_sources(record, official, llm_calls, tool_calls, apply_failure)
        ),
        evidence_envelopes=(
            {"apply_failure": apply_failure} if apply_failure else {}
        ),
        confidence=_confidence(
            record=record,
            official=official,
            failure_type=failure_type,
            empty_patch_reason=empty_reason,
            apply_error=apply_error,
            llm_calls=llm_calls,
        ),
    )
    return event.to_dict()


def build_failure_cause_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build normalized root-cause events for a sequence of task records."""

    return [build_failure_cause_event(record) for record in records or []]


def _official_harness(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("official_harness")
    if not isinstance(value, dict):
        value = record.get("harness")
    return value if isinstance(value, dict) else {}


def _official_test_failures(official: dict[str, Any], bucket_name: str) -> list[str]:
    tests_status = official.get("tests_status")
    if not isinstance(tests_status, dict):
        return []
    bucket = tests_status.get(bucket_name)
    if not isinstance(bucket, dict):
        return []
    failures = bucket.get("failure")
    if failures is None:
        failures = bucket.get("failures")
    return [str(item) for item in _as_list(failures) if str(item)]


def _is_empty_patch(
    record: dict[str, Any],
    official: dict[str, Any],
    patch_before: int,
    patch_after: int,
) -> bool:
    if official.get("empty_patch") is True:
        return True
    if official.get("patch_is_None") is True or official.get("patch_exists") is False:
        return True
    if official.get("patch_successfully_applied") is True:
        return False
    # Structured-edit JSON may have non-zero content before materialization, but
    # it is still an empty patch if no patch/file edit survived materialization.
    if patch_after > 0:
        return False
    if _as_list(record.get("file_edits")):
        return False
    return not bool(record.get("success", False))


def _infer_empty_patch_reason(
    *,
    record: dict[str, Any],
    empty_patch: bool,
    llm_calls: list[dict[str, Any]],
    apply_error: str,
    apply_failure: dict[str, Any],
) -> str:
    if not empty_patch:
        return "not_empty_patch"

    known_reason = str(apply_failure.get("known_reason") or "").strip()
    if known_reason in _APPLY_FAILURE_KNOWN_REASONS:
        return known_reason

    failure_types = {
        str(call.get("failure_type") or "").strip().lower()
        for call in llm_calls
        if call.get("failure_type") is not None
    }
    finish_reasons = {
        str(call.get("finish_reason") or "").strip().lower()
        for call in llm_calls
        if call.get("finish_reason") is not None
    }
    if "length_exhausted" in failure_types or "length" in finish_reasons:
        return "length_exhausted"
    if failure_types & _API_FAILURE_TYPES:
        return "api_error"

    apply_text = apply_error.lower()
    if any(marker in apply_text for marker in _ANCHOR_MISMATCH_MARKERS):
        return "structured_edit_anchor_mismatch"
    if any(marker in apply_text for marker in _PARSE_FAILURE_MARKERS):
        return "structured_edit_parse_failed"

    if any(
        str(call.get("failure_type") or "").strip().lower()
        in {"invalid_output", "empty_content", "malformed_json"}
        for call in llm_calls
    ):
        return "structured_edit_parse_failed"
    if llm_calls and not any(
        int(call.get("structured_edit_count") or 0) > 0
        for call in llm_calls
        if isinstance(call, dict)
    ):
        preview = "\n".join(
            str(call.get("content_preview") or "") for call in llm_calls
        ).lower()
        if any(marker in preview for marker in _NO_PATCH_INTENT_MARKERS):
            return "no_patch_intent"

    steps_text = "\n".join(str(item).lower() for item in _as_list(record.get("intermediate_steps")))
    command_text = "\n".join(
        str(result.get("failure_type") or result.get("validation_status") or "")
        for result in _as_list(record.get("command_results"))
        if isinstance(result, dict)
    ).lower()
    test_text = "\n".join(
        str(result.get("failure_type") or result.get("validation_status") or "")
        for result in _as_list(record.get("test_results"))
        if isinstance(result, dict)
    ).lower()
    if "validation" in steps_text or "validation_patch_failure" in command_text + test_text:
        return "validation_rejected"

    return "unknown"


def _infer_failure_type(
    *,
    record: dict[str, Any],
    empty_patch: bool,
    empty_patch_reason: str,
    apply_error: str,
    f2p_failures: list[str],
    p2p_failures: list[str],
) -> str:
    if bool(record.get("success", False)):
        return "success"
    if empty_patch:
        return "empty_patch"
    if f2p_failures:
        return "fail_to_pass_failure"
    if p2p_failures:
        return "pass_to_pass_regression"
    if apply_error:
        return "materialization_failed"
    if empty_patch_reason in {"api_error", "length_exhausted", "structured_edit_parse_failed"}:
        return "generation_failed"
    status = str(record.get("execution_status") or "").lower()
    if status == "environment_failed":
        return "infrastructure_failure"
    if status == "test_failed":
        return "test_failure"
    if empty_patch_reason == "validation_rejected":
        return "validation_rejected"
    return "unknown_failure"


def _infer_phase(
    *,
    record: dict[str, Any],
    failure_type: str,
    empty_patch_reason: str,
    apply_error: str,
    f2p_failures: list[str],
    p2p_failures: list[str],
) -> str:
    if failure_type == "success":
        return "success"
    generation_reasons = {
        "api_error",
        "length_exhausted",
        "structured_edit_parse_failed",
        "no_patch_intent",
    }
    if empty_patch_reason in generation_reasons:
        return "generation"
    if (
        empty_patch_reason in {"structured_edit_anchor_mismatch", "git_apply_failure"}
        or apply_error
    ):
        return "materialization"
    if empty_patch_reason == "validation_rejected" or failure_type == "validation_rejected":
        return "validation"
    if failure_type == "empty_patch":
        return "unknown"
    if f2p_failures or p2p_failures:
        return "official_harness"
    if failure_type == "infrastructure_failure":
        return "infrastructure"
    status = str(record.get("execution_status") or "").lower()
    if status == "environment_failed":
        return "infrastructure"
    return "unknown"


def _latest_structured_edit_apply_error(
    llm_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> str:
    for call in reversed(llm_calls):
        error = call.get("structured_edit_apply_error")
        if error:
            return str(error)
    for call in reversed(tool_calls):
        if str(call.get("name") or "") != "structured_edit_materializer":
            continue
        result = call.get("result")
        if isinstance(result, dict) and result.get("error"):
            return str(result.get("error"))
    return ""


def _structured_edit_count(
    llm_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> int:
    counts: list[int] = []
    for call in llm_calls:
        if "structured_edit_count" in call:
            counts.append(_int_or_zero(call.get("structured_edit_count")))
    for call in tool_calls:
        if str(call.get("name") or "") != "structured_edit_materializer":
            continue
        arguments = call.get("arguments")
        if isinstance(arguments, dict) and "edit_count" in arguments:
            counts.append(_int_or_zero(arguments.get("edit_count")))
    return max(counts) if counts else 0


def _structured_edit_files(
    llm_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> list[str]:
    files: list[str] = []
    for call in llm_calls:
        for item in _as_list(call.get("structured_edit_files")):
            files.append(str(item))
    for call in tool_calls:
        if str(call.get("name") or "") != "structured_edit_materializer":
            continue
        arguments = call.get("arguments")
        if isinstance(arguments, dict):
            for item in _as_list(arguments.get("files")):
                files.append(str(item))
    return _dedupe_nonempty(files)[:20]


def _patch_chars_before_materialization(
    record: dict[str, Any],
    llm_calls: list[dict[str, Any]],
    apply_failure: dict[str, Any],
) -> int:
    candidates = [
        _int_or_zero(_as_dict(record.get("prediction")).get("model_patch_chars")),
        len(str(record.get("model_patch") or "")),
        _int_or_zero(apply_failure.get("patch_chars_before_materialization")),
    ]
    for call in llm_calls:
        candidates.append(_int_or_zero(call.get("extracted_patch_chars")))
        candidates.append(_int_or_zero(call.get("content_chars")))
    return max(candidates) if candidates else 0


def _patch_chars_after_materialization(
    record: dict[str, Any],
    apply_failure: dict[str, Any],
) -> int:
    candidates = [
        len(str(record.get("patch_diff") or "")),
        len(str(record.get("model_patch") or "")),
        _int_or_zero(_as_dict(record.get("prediction")).get("model_patch_chars")),
        _int_or_zero(apply_failure.get("patch_chars_after_materialization")),
    ]
    return max(candidates)


def _retry_count(llm_calls: list[dict[str, Any]]) -> int:
    llm_retry_count = max(0, len(llm_calls) - 1)
    transport_retries = 0
    for call in llm_calls:
        attempts = _as_list(call.get("request_attempts"))
        transport_retries += max(0, len(attempts) - 1)
    return llm_retry_count + transport_retries


def _tokens_per_task(record: dict[str, Any], llm_calls: list[dict[str, Any]]) -> int:
    total = sum(_int_or_zero(call.get("total_tokens")) for call in llm_calls)
    if total:
        return total
    cost = record.get("cost")
    if isinstance(cost, dict):
        return _int_or_zero(cost.get("total_tokens"))
    return 0


def _evidence_sources(
    record: dict[str, Any],
    official: dict[str, Any],
    llm_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    apply_failure: dict[str, Any],
) -> list[str]:
    sources: list[str] = []
    if official:
        sources.append("official_harness")
    if llm_calls:
        sources.append("llm_calls")
    if any(str(call.get("name") or "") == "structured_edit_materializer" for call in tool_calls):
        sources.append("materializer")
    if apply_failure:
        sources.append("materializer_trace")
    if isinstance(record.get("prediction"), dict) or record.get("model_patch"):
        sources.append("prediction")
    if str(record.get("patch_diff") or "").strip():
        sources.append("patch_diff")
    if _as_list(record.get("command_results")):
        sources.append("command_results")
    if _as_list(record.get("test_results")):
        sources.append("test_results")
    if isinstance(record.get("subprocess_logs"), dict):
        sources.append("subprocess_logs")
    return _dedupe_nonempty(sources)


def _confidence(
    *,
    record: dict[str, Any],
    official: dict[str, Any],
    failure_type: str,
    empty_patch_reason: str,
    apply_error: str,
    llm_calls: list[dict[str, Any]],
) -> float:
    if failure_type == "success":
        return 1.0
    if official and failure_type in {"fail_to_pass_failure", "pass_to_pass_regression"}:
        return 0.95
    if official and failure_type == "empty_patch":
        if empty_patch_reason in {"unknown", "not_empty_patch"}:
            return 0.75
        return 0.9
    if apply_error:
        return 0.85
    if llm_calls and empty_patch_reason != "unknown":
        return 0.8
    if _as_list(record.get("command_results")) or _as_list(record.get("test_results")):
        return 0.65
    return 0.4


def _repo_identifier(task: dict[str, Any], record: dict[str, Any]) -> str:
    metadata = _as_dict(task.get("metadata"))
    return str(
        task.get("repo_url")
        or task.get("repo_path")
        or metadata.get("repo")
        or metadata.get("repo_url")
        or record.get("repo")
        or ""
    )


def _latest_text_field(items: list[dict[str, Any]], key: str) -> str:
    for item in reversed(items):
        value = item.get(key)
        if value is not None:
            return str(value)
    return ""


def _latest_apply_failure_envelope(
    record: dict[str, Any],
    llm_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    direct = record.get("apply_failure")
    if isinstance(direct, dict):
        return _sanitize_apply_failure_envelope(direct)
    for call in reversed(llm_calls):
        value = call.get("apply_failure")
        if isinstance(value, dict):
            return _sanitize_apply_failure_envelope(value)
    return {}


def _sanitize_apply_failure_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    failure_type = str(envelope.get("type") or "").strip()
    if failure_type not in _APPLY_FAILURE_TYPES:
        failure_type = "unknown_apply_failure"
    known_reason = str(envelope.get("known_reason") or "").strip()
    if known_reason not in _APPLY_FAILURE_KNOWN_REASONS:
        known_reason = "unknown"
    is_weakly_classified = bool(envelope.get("is_weakly_classified"))
    if failure_type == "unknown_apply_failure" or known_reason == "unknown":
        is_weakly_classified = True
    return {
        "schema_version": str(envelope.get("schema_version") or ""),
        "phase": _bounded_text(envelope.get("phase"), max_chars=80),
        "type": failure_type,
        "known_failure_type": _bounded_text(
            envelope.get("known_failure_type") or "empty_patch",
            max_chars=80,
        ),
        "known_reason": known_reason,
        "is_weakly_classified": is_weakly_classified,
        "observable_signature": _bounded_string_list(
            envelope.get("observable_signature"),
            max_items=40,
            max_chars=200,
        ),
        "message_tail": _bounded_text(envelope.get("message_tail"), max_chars=1000),
        "target_files": _bounded_string_list(
            envelope.get("target_files"),
            max_items=20,
            max_chars=240,
        ),
        "structured_edit_count": _int_or_zero(envelope.get("structured_edit_count")),
        "structured_edit_extracted": bool(envelope.get("structured_edit_extracted")),
        "edit_intent_present": bool(envelope.get("edit_intent_present")),
        "retry_attempted": bool(envelope.get("retry_attempted")),
        "retry_failed": bool(envelope.get("retry_failed")),
        "patch_chars_before_materialization": _int_or_zero(
            envelope.get("patch_chars_before_materialization")
        ),
        "patch_chars_after_materialization": _int_or_zero(
            envelope.get("patch_chars_after_materialization")
        ),
    }


def _bounded_string_list(value: Any, *, max_items: int, max_chars: int) -> list[str]:
    return [
        _bounded_text(item, max_chars=max_chars)
        for item in _as_list(value)
        if _bounded_text(item, max_chars=max_chars)
    ][:max_items]


def _bounded_text(value: Any, *, max_chars: int) -> str:
    text = _redact_text(str(value or "").replace("\r", " ").replace("\n", " ")).strip()
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _dedupe_nonempty(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _redact_text(text: str) -> str:
    redacted = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
