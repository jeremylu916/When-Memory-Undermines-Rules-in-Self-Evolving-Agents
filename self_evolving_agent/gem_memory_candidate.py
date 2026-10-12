"""GEM memory candidate schema for execution-gated admission.

This module is intentionally pure: it builds, validates, fingerprints, and
serializes candidate memory items before any oracle or admission side effects.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


GEM_MEMORY_CANDIDATE_SCHEMA_VERSION = "gem.memory_candidate.v1"

GEM_ADMISSION_STATES = ("pending", "admitted", "quarantined")

FORBIDDEN_PROMPT_MARKERS = (
    "gold_patch",
    "gold_diff",
    "reference_patch",
    "hidden_test",
    "hidden_assertion",
    "official_stdout",
    "official_stderr",
    "traceback",
)

FORBIDDEN_SECRET_KEYS = (
    "api_key",
    "authorization",
    "bearer",
    "token",
    "password",
    "secret",
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


class GemMemoryCandidateValidationError(ValueError):
    """Raised when a GEM memory candidate violates the v1 schema."""


def build_gem_memory_candidate(
    *,
    task_id: str,
    repo: str,
    benchmark_id: str,
    trajectory_id: str,
    pre_patch_ref: str,
    post_patch_ref: str,
    claim: str,
    failure_signature: dict[str, Any],
    intervention: dict[str, Any],
    applicability: Any,
    exclusion: Any,
    evidence_refs: list[str] | tuple[str, ...] | None = None,
    oracle_refs: list[str] | tuple[str, ...] | None = None,
    admission_state: str = "pending",
    provenance: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build and validate a sanitized GEM candidate payload."""

    candidate = {
        "schema_version": GEM_MEMORY_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": "",
        "created_at": created_at or _utc_now(),
        "task_id": str(task_id or ""),
        "repo": str(repo or ""),
        "benchmark_id": str(benchmark_id or ""),
        "trajectory_id": str(trajectory_id or ""),
        "pre_patch_ref": str(pre_patch_ref or ""),
        "post_patch_ref": str(post_patch_ref or ""),
        "claim": _redact_text(str(claim or "")),
        "failure_signature": _sanitize_payload(failure_signature),
        "intervention": _sanitize_payload(intervention),
        "applicability": _sanitize_payload(applicability),
        "exclusion": _sanitize_payload(exclusion),
        "evidence_refs": _string_list(evidence_refs or []),
        "oracle_refs": _string_list(oracle_refs or []),
        "admission_state": str(admission_state or "pending"),
        "provenance": _sanitize_payload(provenance or {}),
    }
    candidate["candidate_id"] = gem_memory_candidate_id(candidate)
    validate_gem_memory_candidate(candidate)
    return candidate


def validate_gem_memory_candidate(candidate: dict[str, Any]) -> None:
    """Validate a GEM candidate payload."""

    if not isinstance(candidate, dict):
        raise GemMemoryCandidateValidationError("candidate must be a mapping")

    for key in (
        "schema_version",
        "candidate_id",
        "created_at",
        "task_id",
        "repo",
        "benchmark_id",
        "trajectory_id",
        "pre_patch_ref",
        "post_patch_ref",
        "claim",
        "failure_signature",
        "intervention",
        "applicability",
        "exclusion",
        "evidence_refs",
        "oracle_refs",
        "admission_state",
        "provenance",
    ):
        if key not in candidate:
            raise GemMemoryCandidateValidationError(f"missing top-level field: {key}")

    if candidate["schema_version"] != GEM_MEMORY_CANDIDATE_SCHEMA_VERSION:
        raise GemMemoryCandidateValidationError("unsupported candidate schema_version")
    if not isinstance(candidate["candidate_id"], str) or not candidate["candidate_id"]:
        raise GemMemoryCandidateValidationError("candidate_id must be a non-empty string")
    if candidate["candidate_id"] != gem_memory_candidate_id(candidate):
        raise GemMemoryCandidateValidationError(
            "candidate_id does not match payload fingerprint"
        )
    for key in (
        "created_at",
        "task_id",
        "repo",
        "benchmark_id",
        "trajectory_id",
        "pre_patch_ref",
        "post_patch_ref",
        "claim",
    ):
        if not isinstance(candidate[key], str):
            raise GemMemoryCandidateValidationError(f"{key} must be a string")
    if not candidate["claim"].strip():
        raise GemMemoryCandidateValidationError("claim must be non-empty")
    if not candidate["task_id"]:
        raise GemMemoryCandidateValidationError("task_id must be non-empty")
    if not candidate["repo"]:
        raise GemMemoryCandidateValidationError("repo must be non-empty")
    if not candidate["pre_patch_ref"]:
        raise GemMemoryCandidateValidationError("pre_patch_ref must be non-empty")
    if not candidate["post_patch_ref"]:
        raise GemMemoryCandidateValidationError("post_patch_ref must be non-empty")

    _validate_failure_signature(candidate["failure_signature"])
    _validate_intervention(candidate["intervention"])
    _validate_string_list(candidate["evidence_refs"], "evidence_refs")
    _validate_string_list(candidate["oracle_refs"], "oracle_refs")
    if candidate["admission_state"] not in GEM_ADMISSION_STATES:
        raise GemMemoryCandidateValidationError("invalid admission_state")
    if not isinstance(candidate["provenance"], dict):
        raise GemMemoryCandidateValidationError("provenance must be a mapping")
    _assert_no_forbidden_payload(candidate, "")


def gem_candidate_fingerprint(candidate: dict[str, Any]) -> str:
    """Return the deterministic GEM candidate fingerprint.

    `admission_state` is lifecycle metadata and is excluded so admission can
    write back pending/admitted/quarantined without changing candidate identity.
    """

    payload = _jsonable(deepcopy(candidate))
    if isinstance(payload, dict):
        payload.pop("candidate_id", None)
        payload.pop("created_at", None)
        payload.pop("admission_state", None)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def gem_memory_candidate_id(candidate: dict[str, Any]) -> str:
    """Return the deterministic id for a GEM candidate-like payload."""

    return f"gemcand_{gem_candidate_fingerprint(candidate)[:20]}"


def read_gem_memory_candidates_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate GEM candidates from a JSONL file."""

    records: list[dict[str, Any]] = []
    source = Path(path)
    if not source.exists():
        return records
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GemMemoryCandidateValidationError(
                f"invalid JSON at {source}:{line_number}"
            ) from exc
        validate_gem_memory_candidate(record)
        records.append(record)
    return records


def write_gem_memory_candidates_jsonl(
    path: str | Path,
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> None:
    """Write validated GEM candidates to JSONL."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for candidate in candidates:
        validate_gem_memory_candidate(candidate)
        lines.append(
            json.dumps(
                _jsonable(candidate),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _validate_failure_signature(value: Any) -> None:
    if not isinstance(value, dict):
        raise GemMemoryCandidateValidationError("failure_signature must be a mapping")
    for key in ("type", "observable"):
        if key not in value:
            raise GemMemoryCandidateValidationError(
                f"failure_signature must include {key}"
            )
        if not _has_content(value[key]):
            raise GemMemoryCandidateValidationError(
                f"failure_signature.{key} must be non-empty"
            )


def _validate_intervention(value: Any) -> None:
    if not isinstance(value, dict):
        raise GemMemoryCandidateValidationError("intervention must be a mapping")
    for key in ("summary", "patch_hash"):
        if key not in value:
            raise GemMemoryCandidateValidationError(f"intervention must include {key}")
        if not _has_content(value[key]):
            raise GemMemoryCandidateValidationError(
                f"intervention.{key} must be non-empty"
            )


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key_text = str(key)
            _assert_safe_key(key_text)
            sanitized[key_text] = _sanitize_payload(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return _jsonable(value)


def _assert_no_forbidden_payload(value: Any, prefix: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            _assert_safe_key(key_text, path=path)
            _assert_no_forbidden_payload(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_forbidden_payload(item, f"{prefix}[{index}]")
    elif isinstance(value, str):
        _assert_safe_text(value, prefix)


def _assert_safe_key(key: str, *, path: str | None = None) -> None:
    normalized = _normalize_marker(key)
    markers = FORBIDDEN_PROMPT_MARKERS + FORBIDDEN_SECRET_KEYS
    if any(marker in normalized for marker in markers):
        raise GemMemoryCandidateValidationError(
            f"forbidden prompt field: {path or key}"
        )


def _assert_safe_text(text: str, path: str) -> None:
    normalized = _normalize_marker(text)
    for marker in FORBIDDEN_PROMPT_MARKERS:
        if marker in normalized:
            raise GemMemoryCandidateValidationError(
                f"forbidden prompt marker in {path or 'payload'}: {marker}"
            )


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return value is not None


def _validate_string_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list):
        raise GemMemoryCandidateValidationError(f"{field_name} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise GemMemoryCandidateValidationError(
            f"{field_name} must contain only strings"
        )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        value = [value]
    return [str(item) for item in value if str(item)]


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


def _normalize_marker(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
