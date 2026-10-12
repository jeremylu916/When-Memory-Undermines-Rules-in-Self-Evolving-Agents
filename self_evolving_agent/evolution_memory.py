"""Append-only evolution memory ledger.

M5.5a is a durable audit log, not an online memory system. It stores one
redacted, hash-chained JSON object per candidate attempt and leaves compression,
retrieval, and optimizer context injection to later milestones.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


EVOLUTION_MEMORY_SCHEMA_VERSION = "m5.6.evolution_memory.v2"
EVOLUTION_MEMORY_SUMMARY_SCHEMA_VERSION = "m5.5.evolution_memory.summary.v1"

_HASH_EXCLUDED_KEYS = {"record_hash", "created_at"}

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

_SECRET_PATH_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
    "azure_agent.local",
)

_NON_SECRET_STAT_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "total_prompt_tokens",
    "total_completion_tokens",
}


def append_evolution_record(path: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one normalized memory record to ``path`` and return it."""

    target = Path(path)
    existing = read_evolution_memory(target)
    previous_hash = existing[-1].get("record_hash", "") if existing else ""
    normalized = _normalize_record(
        record,
        round_default=len(existing) + 1,
        previous_record_hash=previous_hash,
    )
    normalized["record_hash"] = _record_hash(normalized)

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(normalized) + "\n")
    return normalized


def read_evolution_memory(path: str | Path) -> list[dict[str, Any]]:
    """Read append-only JSONL memory records, preserving order."""

    source = Path(path)
    if not source.exists():
        return []
    records: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid evolution memory JSONL at line {line_number}: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise ValueError(
                    f"Evolution memory line {line_number} must be a JSON object"
                )
            records.append(payload)
    return records


def summarize_evolution_memory(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a compact deterministic summary for reporting and planning."""

    promoted = rejected = not_evaluated = 0
    dev_scores: list[float] = []
    updated_module_counts: dict[str, int] = {}
    pattern_counts: dict[str, int] = {}
    rejected_update_hash_counts: dict[str, int] = {}
    total_cost = _default_cost()
    taxonomy_delta: dict[str, int] = {}

    for record in records:
        decision = str(record.get("promotion_decision") or "not_evaluated")
        if decision == "promoted":
            promoted += 1
        elif decision == "rejected":
            rejected += 1
        else:
            not_evaluated += 1

        after_score = _number_or_none(record.get("dev_score_after"))
        if after_score is not None:
            dev_scores.append(after_score)

        for module in record.get("updated_modules", []) or []:
            module_key = str(module)
            updated_module_counts[module_key] = updated_module_counts.get(module_key, 0) + 1

        for pattern_id in _record_pattern_ids(record):
            pattern_counts[pattern_id] = pattern_counts.get(pattern_id, 0) + 1

        for update in record.get("rejected_updates", []) or []:
            digest = _stable_hash(_redact_jsonable(update))[:16]
            rejected_update_hash_counts[digest] = rejected_update_hash_counts.get(digest, 0) + 1

        _merge_cost(total_cost, record.get("cost", {}))
        _merge_taxonomy_delta(
            taxonomy_delta,
            record.get("failure_taxonomy_before", {}),
            record.get("failure_taxonomy_after", {}),
        )

    repeated_patterns = sorted(
        pattern_id for pattern_id, count in pattern_counts.items() if count > 1
    )
    repeated_rejected = sorted(
        digest for digest, count in rejected_update_hash_counts.items() if count > 1
    )
    return {
        "schema_version": EVOLUTION_MEMORY_SUMMARY_SCHEMA_VERSION,
        "total_records": len(records),
        "rounds": [record.get("round") for record in records],
        "promoted_count": promoted,
        "rejected_count": rejected,
        "not_evaluated_count": not_evaluated,
        "best_dev_score": max(dev_scores) if dev_scores else None,
        "latest_dev_score": dev_scores[-1] if dev_scores else None,
        "failure_taxonomy_delta": dict(sorted(taxonomy_delta.items())),
        "repeated_failure_patterns": repeated_patterns,
        "repeated_rejected_update_hashes": repeated_rejected,
        "updated_module_counts": dict(sorted(updated_module_counts.items())),
        "total_cost": total_cost,
        "hash_chain_valid": verify_hash_chain(records),
    }


def build_evolution_record(
    *,
    round: int,
    candidate_config: dict[str, Any] | None = None,
    diagnosis: dict[str, Any] | None = None,
    safety_result: Any | None = None,
    train_score: float | None = None,
    dev_score_before: float | None = None,
    dev_score_after: float | None = None,
    promotion_decision: Any | None = None,
    artifact_paths: dict[str, str] | None = None,
    failure_taxonomy_before: dict[str, Any] | None = None,
    failure_taxonomy_after: dict[str, Any] | None = None,
    training_failure_taxonomy: dict[str, Any] | None = None,
    evaluation_context: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a memory record from common M5.4/M5.6 artifacts."""

    builder = (
        candidate_config.get("candidate_builder", {})
        if isinstance(candidate_config, dict)
        else {}
    )
    optimizer = (
        candidate_config.get("optimizer", {})
        if isinstance(candidate_config, dict)
        else {}
    )
    safety_status, safety_reasons = _safety_status_and_reasons(safety_result)
    decision, reason = _promotion_decision_and_reason(promotion_decision)
    context = evaluation_context if isinstance(evaluation_context, dict) else {}
    return {
        "round": round,
        "base_config_hash": builder.get("base_config_hash", ""),
        "candidate_id": builder.get("candidate_id", ""),
        "candidate_config_hash": builder.get("candidate_config_hash", ""),
        "source_patterns": builder.get("source_patterns", []),
        "structured_updates": optimizer.get("proposed_structured_updates", []),
        "optimizer_structured_updates": optimizer.get(
            "optimizer_structured_updates", []
        ),
        "operation_planner": optimizer.get("operation_planner", {}),
        "applied_updates": builder.get("applied_updates", []),
        "preserved_updates": builder.get("preserved_updates", []),
        "suppressed_updates": builder.get("suppressed_updates", []),
        "replaced_updates": builder.get("replaced_updates", []),
        "rejected_updates": builder.get("rejected_updates", []),
        "prompt_diff_hash": _hash_text(builder.get("prompt_diff", "")),
        "updated_modules": builder.get("updated_modules", []),
        "safety_status": safety_status,
        "safety_reasons": safety_reasons,
        "train_score": train_score,
        "dev_score_before": dev_score_before,
        "dev_score_after": dev_score_after,
        "promotion_decision": decision,
        "promotion_reason": reason,
        "training_failure_taxonomy": (
            training_failure_taxonomy
            if isinstance(training_failure_taxonomy, dict)
            else (
                diagnosis.get("failure_taxonomy", {})
                if isinstance(diagnosis, dict)
                else {}
            )
        ),
        "failure_taxonomy_before": failure_taxonomy_before or {},
        "failure_taxonomy_after": (
            failure_taxonomy_after
            if isinstance(failure_taxonomy_after, dict)
            else (
                diagnosis.get("failure_taxonomy", {})
                if isinstance(diagnosis, dict)
                else {}
            )
        ),
        "baseline_cache_hit": bool(context.get("dev_current_cache_hit", False)),
        "candidate_cache_hit": bool(context.get("dev_candidate_cache_hit", False)),
        "official_result_feedback": bool(
            context.get("official_result_feedback", False)
        ),
        "evaluation_context": context,
        "cost": cost if isinstance(cost, dict) else _diagnosis_cost(diagnosis),
        "artifact_paths": artifact_paths or {},
    }


def verify_hash_chain(records: list[dict[str, Any]]) -> bool:
    previous_hash = ""
    for record in records:
        if record.get("previous_record_hash", "") != previous_hash:
            return False
        if record.get("record_hash", "") != _record_hash(record):
            return False
        previous_hash = str(record.get("record_hash", ""))
    return True


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _normalize_record(
    record: dict[str, Any],
    *,
    round_default: int,
    previous_record_hash: str,
) -> dict[str, Any]:
    cleaned = _redact_jsonable(copy.deepcopy(record))
    normalized = _default_record()
    if isinstance(cleaned, dict):
        normalized.update(cleaned)
    normalized["schema_version"] = str(
        normalized.get("schema_version") or EVOLUTION_MEMORY_SCHEMA_VERSION
    )
    normalized["round"] = int(normalized.get("round") or round_default)
    normalized["previous_record_hash"] = previous_record_hash
    normalized["record_hash"] = ""
    normalized["cost"] = _normalize_cost(normalized.get("cost", {}))
    normalized["artifact_paths"] = _normalize_artifact_paths(
        normalized.get("artifact_paths", {})
    )
    normalized["source_patterns"] = _as_list(normalized.get("source_patterns"))
    normalized["structured_updates"] = _as_list(normalized.get("structured_updates"))
    normalized["optimizer_structured_updates"] = _as_list(
        normalized.get("optimizer_structured_updates")
    )
    normalized["operation_planner"] = (
        normalized["operation_planner"]
        if isinstance(normalized.get("operation_planner"), dict)
        else {}
    )
    normalized["applied_updates"] = _as_list(normalized.get("applied_updates"))
    normalized["preserved_updates"] = _as_list(normalized.get("preserved_updates"))
    normalized["suppressed_updates"] = _as_list(normalized.get("suppressed_updates"))
    normalized["replaced_updates"] = _as_list(normalized.get("replaced_updates"))
    normalized["rejected_updates"] = _as_list(normalized.get("rejected_updates"))
    normalized["updated_modules"] = sorted(
        {str(module) for module in _as_list(normalized.get("updated_modules"))}
    )
    normalized["safety_reasons"] = [
        str(reason) for reason in _as_list(normalized.get("safety_reasons"))
    ]
    normalized["failure_taxonomy_before"] = _string_int_dict(
        normalized.get("failure_taxonomy_before", {})
    )
    normalized["failure_taxonomy_after"] = _string_int_dict(
        normalized.get("failure_taxonomy_after", {})
    )
    normalized["training_failure_taxonomy"] = _string_int_dict(
        normalized.get("training_failure_taxonomy", {})
    )
    normalized["baseline_cache_hit"] = bool(
        normalized.get("baseline_cache_hit", False)
    )
    normalized["candidate_cache_hit"] = bool(
        normalized.get("candidate_cache_hit", False)
    )
    normalized["official_result_feedback"] = bool(
        normalized.get("official_result_feedback", False)
    )
    normalized["evaluation_context"] = _normalize_evaluation_context(
        normalized.get("evaluation_context", {})
    )
    return normalized


def _default_record() -> dict[str, Any]:
    return {
        "schema_version": EVOLUTION_MEMORY_SCHEMA_VERSION,
        "round": 0,
        "base_config_hash": "",
        "candidate_id": "",
        "candidate_config_hash": "",
        "source_patterns": [],
        "structured_updates": [],
        "optimizer_structured_updates": [],
        "operation_planner": {},
        "applied_updates": [],
        "preserved_updates": [],
        "suppressed_updates": [],
        "replaced_updates": [],
        "rejected_updates": [],
        "prompt_diff_hash": "",
        "updated_modules": [],
        "safety_status": "unknown",
        "safety_reasons": [],
        "train_score": None,
        "dev_score_before": None,
        "dev_score_after": None,
        "promotion_decision": "not_evaluated",
        "promotion_reason": "",
        "training_failure_taxonomy": {},
        "failure_taxonomy_before": {},
        "failure_taxonomy_after": {},
        "baseline_cache_hit": False,
        "candidate_cache_hit": False,
        "official_result_feedback": False,
        "evaluation_context": {},
        "cost": _default_cost(),
        "artifact_paths": {
            "diagnosis": "",
            "candidate_config": "",
            "prompt_diff": "",
            "safety_report": "",
            "promotion_report": "",
        },
        "previous_record_hash": "",
        "record_hash": "",
    }


def _record_hash(record: dict[str, Any]) -> str:
    hashable = {
        key: value
        for key, value in record.items()
        if key not in _HASH_EXCLUDED_KEYS
    }
    return hashlib.sha256(canonical_json(hashable).encode("utf-8")).hexdigest()


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _hash_text(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest() if text else ""


def _redact_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _redact_text(str(key))
            if _looks_secret_key(str(key)):
                cleaned[key_text] = "[REDACTED]"
            else:
                cleaned[key_text] = _redact_jsonable(item)
        return cleaned
    if isinstance(value, list):
        return [_redact_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_jsonable(item) for item in value]
    return value


def _redact_text(text: str) -> str:
    redacted = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    lowered = redacted.lower()
    if any(marker in lowered for marker in _SECRET_PATH_MARKERS) and (
        "/" in redacted or "\\" in redacted
    ):
        return "[REDACTED_PATH]"
    return redacted


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _NON_SECRET_STAT_KEYS:
        return False
    return any(marker in lowered for marker in _SECRET_PATH_MARKERS)


def _default_cost() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }


def _normalize_cost(value: Any) -> dict[str, Any]:
    cost = _default_cost()
    if isinstance(value, dict):
        cost["prompt_tokens"] = _int_or_zero(value.get("prompt_tokens"))
        cost["completion_tokens"] = _int_or_zero(value.get("completion_tokens"))
        cost["total_tokens"] = _int_or_zero(value.get("total_tokens"))
        cost["cost_usd"] = _float_or_zero(value.get("cost_usd"))
    return cost


def _merge_cost(total: dict[str, Any], value: Any) -> None:
    cost = _normalize_cost(value)
    total["prompt_tokens"] += cost["prompt_tokens"]
    total["completion_tokens"] += cost["completion_tokens"]
    total["total_tokens"] += cost["total_tokens"]
    total["cost_usd"] = round(total["cost_usd"] + cost["cost_usd"], 10)


def _normalize_artifact_paths(value: Any) -> dict[str, str]:
    defaults = _default_record()["artifact_paths"]
    if isinstance(value, dict):
        for key, item in value.items():
            defaults[str(key)] = str(item or "")
    return _redact_jsonable(defaults)


def _normalize_evaluation_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _redact_jsonable(value)


def _string_int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _int_or_zero(item)
        for key, item in sorted(value.items(), key=lambda item: str(item[0]))
    }


def _merge_taxonomy_delta(
    total: dict[str, int],
    before: Any,
    after: Any,
) -> None:
    before_counts = _string_int_dict(before)
    after_counts = _string_int_dict(after)
    labels = set(before_counts) | set(after_counts)
    for label in labels:
        total[label] = total.get(label, 0) + (
            after_counts.get(label, 0) - before_counts.get(label, 0)
        )


def _record_pattern_ids(record: dict[str, Any]) -> list[str]:
    ids = []
    for pattern in record.get("source_patterns", []) or []:
        if isinstance(pattern, dict):
            pattern_id = pattern.get("pattern_id")
            if pattern_id:
                ids.append(str(pattern_id))
        elif pattern:
            ids.append(str(pattern))
    return ids


def _diagnosis_cost(diagnosis: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(diagnosis, dict):
        return _default_cost()
    audit = diagnosis.get("aggregation_audit")
    if isinstance(audit, dict):
        return {
            "prompt_tokens": _int_or_zero(audit.get("total_prompt_tokens")),
            "completion_tokens": _int_or_zero(audit.get("total_completion_tokens")),
            "total_tokens": _int_or_zero(audit.get("total_prompt_tokens"))
            + _int_or_zero(audit.get("total_completion_tokens")),
            "cost_usd": _float_or_zero(audit.get("cost_usd")),
        }
    return _normalize_cost(diagnosis.get("cost", {}))


def _safety_status_and_reasons(safety_result: Any | None) -> tuple[str, list[str]]:
    if safety_result is None:
        return "unknown", []
    passed = getattr(safety_result, "passed", None)
    reason = getattr(safety_result, "reason", "")
    violations = list(getattr(safety_result, "violations", []) or [])
    if passed is True:
        return "passed", [str(reason)] if reason else []
    if passed is False:
        reasons = [str(reason)] if reason else []
        reasons.extend(str(item) for item in violations)
        return "failed", reasons
    return "unknown", []


def _promotion_decision_and_reason(decision: Any | None) -> tuple[str, str]:
    if decision is None:
        return "not_evaluated", ""
    accepted = getattr(decision, "accepted", None)
    reason = str(getattr(decision, "reason", "") or "")
    if accepted is True:
        return "promoted", reason
    if accepted is False:
        return "rejected", reason
    return "not_evaluated", reason


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


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


def _number_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
