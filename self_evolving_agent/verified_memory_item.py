"""Sidecar verified-memory items for GEM.

Raw GEM candidates are proposals. This module derives a narrower, oracle-
constrained memory view only after Exec admission has executable fail-before /
pass-after evidence.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


VERIFIED_MEMORY_ITEM_SCHEMA_VERSION = "gem.verified_memory_item.v1"

CLAIM_SCOPES = ("single_failure_reproduction", "repo_pattern", "broader")
RETRIEVAL_POLICIES = (
    "not_retrievable_until_scope_review",
    "narrow_only",
    "blocked",
)

FORBIDDEN_TEXT_MARKERS = (
    "gold_patch",
    "gold diff",
    "reference_patch",
    "reference patch",
    "hidden_test",
    "hidden test",
    "hidden_assertion",
    "official_stdout",
    "official stderr",
    "official_stderr",
    "official stdout",
    "hidden expected",
    "hidden_actual",
)

SECRET_PATTERNS = (
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


class VerifiedMemoryItemValidationError(ValueError):
    """Raised when a verified-memory item is malformed or unsafe."""


def build_verified_memory_item(
    *,
    candidate: dict[str, Any],
    admission_event: dict[str, Any],
    oracle_runs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    official_outcome_audit: dict[str, Any] | None = None,
    verified_claim: str | None = None,
    claim_scope: str = "single_failure_reproduction",
    oracle_grounded_failure: str | None = None,
    retrieval_policy: str | None = None,
    memory_text: str | None = None,
    provenance: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any] | None:
    """Build one verified-memory item, or return None without grounded evidence."""

    if (
        admission_event.get("decision") != "admitted"
        or admission_event.get("policy") != "Exec"
        or str(admission_event.get("candidate_id") or "")
        != str(candidate.get("candidate_id") or "")
    ):
        return None

    grounded_run = _first_grounded_oracle_run(admission_event, list(oracle_runs or []))
    if grounded_run is None:
        return None

    raw_claim = _redact_text(str(candidate.get("claim") or ""))
    claim = _redact_text(
        verified_claim
        or _derive_verified_claim(candidate=candidate, oracle_run=grounded_run)
    )
    if not claim:
        raise VerifiedMemoryItemValidationError("verified_claim must be non-empty")
    if claim.strip() == raw_claim.strip():
        raise VerifiedMemoryItemValidationError(
            "verified_claim must be narrower than raw_claim"
        )

    official = _normalize_official_audit(official_outcome_audit)
    policy = retrieval_policy or _default_retrieval_policy(official)
    grounded_failure = _redact_text(
        oracle_grounded_failure
        or _derive_oracle_grounded_failure(candidate=candidate, oracle_run=grounded_run)
    )
    text = _redact_text(memory_text or _build_memory_text(claim, official))
    if raw_claim and text.strip() == raw_claim.strip():
        raise VerifiedMemoryItemValidationError("memory_text must not be raw_claim")
    if official["official_resolved"] is False and not _has_unresolved_caution(text):
        raise VerifiedMemoryItemValidationError(
            "official unresolved memory_text must include a narrow-oracle caution"
        )

    item = {
        "schema_version": VERIFIED_MEMORY_ITEM_SCHEMA_VERSION,
        "verified_memory_id": "",
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "admission_event_id": str(admission_event.get("admission_event_id") or ""),
        "created_at": created_at or _utc_now(),
        "raw_claim": raw_claim,
        "verified_claim": claim,
        "claim_scope": str(claim_scope or "single_failure_reproduction"),
        "oracle_grounded_failure": grounded_failure,
        "oracle_evidence": {
            "oracle_id": str(grounded_run.get("oracle_id") or ""),
            "oracle_run_id": str(grounded_run.get("oracle_run_id") or ""),
            "pre_patch_result": str(grounded_run.get("pre_patch_result") or ""),
            "post_patch_result": str(grounded_run.get("post_patch_result") or ""),
            "fail_before": bool(grounded_run.get("fail_before") is True),
            "pass_after": bool(grounded_run.get("pass_after") is True),
        },
        "official_outcome_audit": official,
        "retrieval_policy": policy,
        "memory_text": text,
        "provenance": _jsonable(
            {
                **(provenance or {}),
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "admission_event_id": str(admission_event.get("admission_event_id") or ""),
                "admission_decision": str(admission_event.get("decision") or ""),
                "oracle_run_id": str(grounded_run.get("oracle_run_id") or ""),
                "source": "gem_verified_memory_item_builder",
            }
        ),
    }
    item["verified_memory_id"] = verified_memory_item_id(item)
    validate_verified_memory_item(item)
    return item


def validate_verified_memory_item(item: dict[str, Any]) -> None:
    """Validate a verified-memory item."""

    if not isinstance(item, dict):
        raise VerifiedMemoryItemValidationError("verified memory item must be a mapping")
    for key in (
        "schema_version",
        "verified_memory_id",
        "candidate_id",
        "admission_event_id",
        "created_at",
        "raw_claim",
        "verified_claim",
        "claim_scope",
        "oracle_grounded_failure",
        "oracle_evidence",
        "official_outcome_audit",
        "retrieval_policy",
        "memory_text",
        "provenance",
    ):
        if key not in item:
            raise VerifiedMemoryItemValidationError(f"missing verified item field: {key}")
    if item["schema_version"] != VERIFIED_MEMORY_ITEM_SCHEMA_VERSION:
        raise VerifiedMemoryItemValidationError("unsupported verified item schema_version")
    for key in (
        "verified_memory_id",
        "candidate_id",
        "admission_event_id",
        "created_at",
        "raw_claim",
        "verified_claim",
        "claim_scope",
        "oracle_grounded_failure",
        "retrieval_policy",
        "memory_text",
    ):
        if not isinstance(item[key], str) or not item[key]:
            raise VerifiedMemoryItemValidationError(f"{key} must be a non-empty string")
    if item["claim_scope"] not in CLAIM_SCOPES:
        raise VerifiedMemoryItemValidationError("invalid claim_scope")
    if item["retrieval_policy"] not in RETRIEVAL_POLICIES:
        raise VerifiedMemoryItemValidationError("invalid retrieval_policy")
    if item["verified_claim"].strip() == item["raw_claim"].strip():
        raise VerifiedMemoryItemValidationError("verified_claim must differ from raw_claim")
    if item["memory_text"].strip() == item["raw_claim"].strip():
        raise VerifiedMemoryItemValidationError("memory_text must not equal raw_claim")

    _validate_oracle_evidence(item["oracle_evidence"])
    _validate_official_audit(item["official_outcome_audit"])
    if not isinstance(item["provenance"], dict):
        raise VerifiedMemoryItemValidationError("provenance must be a mapping")
    if item["official_outcome_audit"]["official_resolved"] is False:
        if item["retrieval_policy"] != "narrow_only":
            raise VerifiedMemoryItemValidationError(
                "official unresolved items must default to narrow_only retrieval"
            )
        if not _has_unresolved_caution(item["memory_text"]):
            raise VerifiedMemoryItemValidationError(
                "official unresolved memory_text missing caution"
            )
    if item["verified_memory_id"] != verified_memory_item_id(item):
        raise VerifiedMemoryItemValidationError("verified_memory_id does not match payload")
    _assert_safe_text_payload(item)


def verified_memory_item_id(item: dict[str, Any]) -> str:
    """Return a deterministic id based on candidate/event/claim/oracle ids."""

    evidence = item.get("oracle_evidence", {}) if isinstance(item, dict) else {}
    payload = {
        "candidate_id": item.get("candidate_id") if isinstance(item, dict) else "",
        "admission_event_id": item.get("admission_event_id") if isinstance(item, dict) else "",
        "verified_claim": item.get("verified_claim") if isinstance(item, dict) else "",
        "oracle_run_id": evidence.get("oracle_run_id") if isinstance(evidence, dict) else "",
    }
    encoded = json.dumps(
        _jsonable(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"gemvmi_{hashlib.sha256(encoded).hexdigest()[:20]}"


def read_verified_memory_items_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read verified-memory items from JSONL."""

    source = Path(path)
    if not source.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VerifiedMemoryItemValidationError(
                f"invalid JSON at {source}:{line_number}"
            ) from exc
        validate_verified_memory_item(record)
        records.append(record)
    return records


def write_verified_memory_items_jsonl(
    path: str | Path,
    items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> None:
    """Write validated verified-memory items to JSONL."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for item in items:
        validate_verified_memory_item(item)
        lines.append(
            json.dumps(
                _jsonable(item),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _first_grounded_oracle_run(
    admission_event: dict[str, Any],
    oracle_runs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    allowed_ids = set(_string_list(admission_event.get("oracle_run_ids", [])))
    if not allowed_ids:
        return None
    candidate_id = str(admission_event.get("candidate_id") or "")
    for run in oracle_runs:
        if str(run.get("oracle_run_id") or "") not in allowed_ids:
            continue
        if str(run.get("candidate_id") or "") != candidate_id:
            continue
        if run.get("fail_before") is True and run.get("pass_after") is True:
            if run.get("pre_patch_result") == "fail" and run.get("post_patch_result") == "pass":
                return run
    return None


def _derive_verified_claim(
    *,
    candidate: dict[str, Any],
    oracle_run: dict[str, Any],
) -> str:
    oracle_blob = json.dumps(
        {
            "oracle_id": oracle_run.get("oracle_id"),
            "command": oracle_run.get("command"),
            "logs": oracle_run.get("logs"),
        },
        sort_keys=True,
        ensure_ascii=True,
    ).lower()
    if (
        "astropy" in str(candidate.get("repo") or "").lower()
        and "required" in oracle_blob
        and "flux" in oracle_blob
        and "time" in oracle_blob
    ):
        return (
            "For Astropy TimeSeries required-column validation, when existing "
            "columns are a prefix of required_columns and a later required column "
            "is missing, report the missing required column rather than the "
            "first-column mismatch."
        )
    failure = candidate.get("failure_signature", {})
    observable = str(failure.get("observable") or "the public reproduction failure")
    return (
        "For the oracle-grounded reproduction only, preserve the behavior that "
        f"changes {observable} from failing before the patch to passing after it."
    )


def _derive_oracle_grounded_failure(
    *,
    candidate: dict[str, Any],
    oracle_run: dict[str, Any],
) -> str:
    oracle_blob = json.dumps(
        {
            "oracle_id": oracle_run.get("oracle_id"),
            "command": oracle_run.get("command"),
            "logs": oracle_run.get("logs"),
        },
        sort_keys=True,
        ensure_ascii=True,
    ).lower()
    if (
        "astropy" in str(candidate.get("repo") or "").lower()
        and "required" in oracle_blob
        and "flux" in oracle_blob
    ):
        return (
            "Astropy TimeSeries remove_column on a later required column reported "
            "a first-column mismatch instead of identifying the missing required "
            "column."
        )
    failure = candidate.get("failure_signature", {})
    return str(failure.get("observable") or "public reproduction oracle failure")


def _build_memory_text(verified_claim: str, official: dict[str, Any]) -> str:
    text = verified_claim.strip()
    if not text.endswith("."):
        text += "."
    if official["official_resolved"] is False:
        text += (
            " Verified only on a public reproduction oracle; official SWE-bench "
            "task outcome was unresolved, so this should be used only as a narrow "
            "reproduction-specific memory and not as evidence that the full task "
            "was solved."
        )
    elif official["official_resolved"] is True:
        text += (
            " Verified by fail-before/pass-after oracle evidence and accompanied "
            "by an official resolved audit; keep retrieval scoped to the verified "
            "failure pattern until broader scope review."
        )
    else:
        text += (
            " Verified only by fail-before/pass-after oracle evidence; no official "
            "task-level audit was supplied, so keep retrieval scope narrow."
        )
    return text


def _normalize_official_audit(audit: dict[str, Any] | None) -> dict[str, Any]:
    audit = audit if isinstance(audit, dict) else {}
    return {
        "official_resolved": _nullable_bool(audit.get("official_resolved")),
        "official_patch_applied": _nullable_bool(
            audit.get("official_patch_applied", audit.get("patch_applied"))
        ),
        "official_pass_to_pass_regression": _nullable_bool(
            audit.get(
                "official_pass_to_pass_regression",
                audit.get("pass_to_pass_regression"),
            )
        ),
        "interpretation": _redact_text(
            str(audit.get("interpretation") or "official audit not supplied")
        ),
    }


def _default_retrieval_policy(official: dict[str, Any]) -> str:
    if official.get("official_resolved") is False:
        return "narrow_only"
    if official.get("official_resolved") is True:
        return "narrow_only"
    return "not_retrievable_until_scope_review"


def _validate_oracle_evidence(evidence: Any) -> None:
    if not isinstance(evidence, dict):
        raise VerifiedMemoryItemValidationError("oracle_evidence must be a mapping")
    for key in (
        "oracle_id",
        "oracle_run_id",
        "pre_patch_result",
        "post_patch_result",
        "fail_before",
        "pass_after",
    ):
        if key not in evidence:
            raise VerifiedMemoryItemValidationError(f"missing oracle_evidence field: {key}")
    if evidence["pre_patch_result"] != "fail":
        raise VerifiedMemoryItemValidationError("oracle pre_patch_result must be fail")
    if evidence["post_patch_result"] != "pass":
        raise VerifiedMemoryItemValidationError("oracle post_patch_result must be pass")
    if evidence["fail_before"] is not True or evidence["pass_after"] is not True:
        raise VerifiedMemoryItemValidationError("oracle evidence must be fail/pass grounded")


def _validate_official_audit(audit: Any) -> None:
    if not isinstance(audit, dict):
        raise VerifiedMemoryItemValidationError("official_outcome_audit must be a mapping")
    for key in (
        "official_resolved",
        "official_patch_applied",
        "official_pass_to_pass_regression",
        "interpretation",
    ):
        if key not in audit:
            raise VerifiedMemoryItemValidationError(f"missing official audit field: {key}")
    for key in (
        "official_resolved",
        "official_patch_applied",
        "official_pass_to_pass_regression",
    ):
        if audit[key] is not None and not isinstance(audit[key], bool):
            raise VerifiedMemoryItemValidationError(f"{key} must be bool or null")
    if not isinstance(audit["interpretation"], str):
        raise VerifiedMemoryItemValidationError("official interpretation must be a string")


def _has_unresolved_caution(text: str) -> bool:
    lowered = text.lower()
    return "official" in lowered and "unresolved" in lowered and "narrow" in lowered


def _assert_safe_text_payload(value: Any, prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_safe_text_payload(item, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_safe_text_payload(item, f"{prefix}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        normalized = lowered.replace("-", "_")
        for marker in FORBIDDEN_TEXT_MARKERS:
            if marker in lowered or marker in normalized:
                raise VerifiedMemoryItemValidationError(
                    f"forbidden text marker in {prefix or 'payload'}: {marker}"
                )


def _nullable_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
        if lowered in {"", "none", "null", "unknown"}:
            return None
    return bool(value)


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
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
