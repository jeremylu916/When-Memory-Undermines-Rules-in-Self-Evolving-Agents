"""GEM execution-gated admission policy."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from self_evolving_agent.gem_memory_candidate import (
    GemMemoryCandidateValidationError,
    validate_gem_memory_candidate,
)
from self_evolving_agent.oracle_runner import validate_oracle_run


ADMISSION_EVENT_SCHEMA_VERSION = "gem.admission_event.v1"

ADMISSION_POLICY = "Exec"

ADMISSION_DECISIONS = ("admitted", "quarantined")

# These spellings occur in the stream smoke wrapper and in direct offline
# admission. They describe the same paper-table condition: no executable
# oracle was available for the candidate.
ORACLE_UNAVAILABLE_REASONS = (
    "oracle_unavailable_in_smoke",
    "alignment_or_leakage_error:no_oracle_runs",
)


class GemAdmissionValidationError(ValueError):
    """Raised when a GEM admission event is malformed."""


def is_oracle_unavailable_admission_event(event: dict[str, Any]) -> bool:
    """Return whether a quarantined event represents unavailable oracle evidence."""

    if not isinstance(event, dict) or event.get("decision") != "quarantined":
        return False
    if event.get("reason") in ORACLE_UNAVAILABLE_REASONS:
        return True
    summary = event.get("evidence_summary")
    errors = summary.get("alignment_errors") if isinstance(summary, dict) else []
    return "no_oracle_runs" in _string_list(errors)


def admit_gem_candidate(
    candidate: dict[str, Any],
    oracle_runs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    created_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply GEM admission and return an updated candidate copy plus event."""

    return apply_gem_admission(candidate, oracle_runs, created_at=created_at)


def apply_gem_admission(
    candidate: dict[str, Any],
    oracle_runs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    created_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a candidate copy with admission_state written from the event."""

    event = build_gem_admission_event(
        candidate,
        oracle_runs,
        created_at=created_at,
    )
    updated = deepcopy(candidate)
    updated["admission_state"] = event["decision"]
    if _candidate_valid_for_writeback(updated):
        validate_gem_memory_candidate(updated)
    return updated, event


def build_gem_admission_event(
    candidate: dict[str, Any],
    oracle_runs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic GEM admission event.

    The policy admits only when every provided oracle run shows fail-before and
    pass-after evidence and the candidate/run ids align.
    """

    runs = list(oracle_runs or [])
    decision, reason, evidence_summary = _admission_decision(candidate, runs)
    candidate_id = str(candidate.get("candidate_id") or "")
    oracle_run_ids = _string_list([run.get("oracle_run_id") for run in runs])
    evidence_refs = _dedupe(
        _string_list(candidate.get("evidence_refs", []))
        + [f"oracle_run:{oracle_run_id}" for oracle_run_id in oracle_run_ids]
    )
    event = {
        "schema_version": ADMISSION_EVENT_SCHEMA_VERSION,
        "admission_event_id": "",
        "candidate_id": candidate_id,
        "policy": ADMISSION_POLICY,
        "decision": decision,
        "reason": reason,
        "oracle_run_ids": oracle_run_ids,
        "evidence_refs": evidence_refs,
        "created_at": created_at or _utc_now(),
        "evidence_summary": evidence_summary,
    }
    event["admission_event_id"] = gem_admission_event_id(event)
    validate_gem_admission_event(event)
    return event


def validate_gem_admission_event(event: dict[str, Any]) -> None:
    """Validate a GEM admission event."""

    if not isinstance(event, dict):
        raise GemAdmissionValidationError("admission event must be a mapping")
    for key in (
        "schema_version",
        "admission_event_id",
        "candidate_id",
        "policy",
        "decision",
        "reason",
        "oracle_run_ids",
        "evidence_refs",
        "created_at",
    ):
        if key not in event:
            raise GemAdmissionValidationError(f"missing admission event field: {key}")
    if event["schema_version"] != ADMISSION_EVENT_SCHEMA_VERSION:
        raise GemAdmissionValidationError("unsupported admission event schema_version")
    for key in (
        "admission_event_id",
        "candidate_id",
        "policy",
        "decision",
        "reason",
        "created_at",
    ):
        if not isinstance(event[key], str) or not event[key]:
            raise GemAdmissionValidationError(f"{key} must be a non-empty string")
    if event["policy"] != ADMISSION_POLICY:
        raise GemAdmissionValidationError("unsupported admission policy")
    if event["decision"] not in ADMISSION_DECISIONS:
        raise GemAdmissionValidationError("invalid admission decision")
    _validate_string_list(event["oracle_run_ids"], "oracle_run_ids")
    _validate_string_list(event["evidence_refs"], "evidence_refs")
    if event["admission_event_id"] != gem_admission_event_id(event):
        raise GemAdmissionValidationError("admission_event_id does not match payload")


def gem_admission_event_id(event: dict[str, Any]) -> str:
    """Return a deterministic id for an admission-event-like payload."""

    payload = _jsonable(deepcopy(event))
    if isinstance(payload, dict):
        payload.pop("admission_event_id", None)
        payload.pop("created_at", None)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"gemadm_{hashlib.sha256(encoded).hexdigest()[:20]}"


def read_admission_events_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read validated GEM admission events from JSONL."""

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
            raise GemAdmissionValidationError(
                f"invalid JSON at {source}:{line_number}"
            ) from exc
        validate_gem_admission_event(record)
        records.append(record)
    return records


def write_admission_events_jsonl(
    path: str | Path,
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> None:
    """Write validated GEM admission events to JSONL."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for record in records:
        validate_gem_admission_event(record)
        lines.append(
            json.dumps(
                _jsonable(record),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _admission_decision(
    candidate: dict[str, Any],
    oracle_runs: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    errors: list[str] = []
    try:
        validate_gem_memory_candidate(candidate)
    except GemMemoryCandidateValidationError as exc:
        errors.append(f"candidate_validation_failed:{exc}")

    if not oracle_runs:
        errors.append("no_oracle_runs")

    candidate_id = str(candidate.get("candidate_id") or "")
    results: list[dict[str, Any]] = []
    for run in oracle_runs:
        try:
            validate_oracle_run(run)
        except ValueError as exc:
            errors.append(f"oracle_run_validation_failed:{exc}")
            continue
        if run["candidate_id"] != candidate_id:
            errors.append("oracle_candidate_id_mismatch")
        results.append(
            {
                "oracle_run_id": run["oracle_run_id"],
                "oracle_id": run["oracle_id"],
                "pre_patch_result": run["pre_patch_result"],
                "post_patch_result": run["post_patch_result"],
                "fail_before": run["fail_before"],
                "pass_after": run["pass_after"],
            }
        )

    evidence_summary = {
        "oracle_count": len(oracle_runs),
        "grounded_oracle_count": sum(
            1
            for run in oracle_runs
            if run.get("fail_before") is True and run.get("pass_after") is True
        ),
        "oracle_results": results,
        "alignment_errors": sorted(set(errors)),
    }
    if errors:
        return (
            "quarantined",
            "alignment_or_leakage_error:" + ";".join(sorted(set(errors))),
            evidence_summary,
        )

    all_grounded = all(
        run.get("fail_before") is True and run.get("pass_after") is True
        for run in oracle_runs
    )
    if all_grounded:
        return (
            "admitted",
            "pre_patch_failed_and_post_patch_passed",
            evidence_summary,
        )

    if any(
        run.get("pre_patch_result") in {"error", "timeout"}
        or run.get("post_patch_result") in {"error", "timeout"}
        for run in oracle_runs
    ):
        reason = "oracle_error_or_timeout"
    elif any(run.get("fail_before") is not True for run in oracle_runs):
        reason = "pre_patch_did_not_fail"
    elif any(run.get("pass_after") is not True for run in oracle_runs):
        reason = "post_patch_did_not_pass"
    else:
        reason = "oracle_evidence_not_grounded"
    return "quarantined", reason, evidence_summary


def _candidate_valid_for_writeback(candidate: dict[str, Any]) -> bool:
    try:
        validate_gem_memory_candidate(candidate)
    except GemMemoryCandidateValidationError:
        return False
    return True


def _validate_string_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list):
        raise GemAdmissionValidationError(f"{field_name} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise GemAdmissionValidationError(f"{field_name} must contain only strings")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
