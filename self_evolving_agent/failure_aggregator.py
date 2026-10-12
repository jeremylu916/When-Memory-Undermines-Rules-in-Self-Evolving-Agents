"""Deterministic failure-pattern aggregation for prompt evolution.

M5.1 critic analyses are per-task judgments. M5.2 turns those judgments into a
compact, auditable diagnosis that the optimizer can consume without reading raw
logs or reinterpreting harness semantics.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any

from self_evolving_agent.config import PROMPT_MODULES


AGGREGATION_SCHEMA_VERSION = "m5.2.failure_aggregator.v1"
TOP_PATTERN_LIMIT = 5
REPRESENTATIVE_TRACE_LIMIT = 3
LOW_CONFIDENCE_THRESHOLD = 0.5

EVIDENCE_QUALITIES = (
    "official_harness",
    "validation",
    "materialization",
    "trace_only",
    "llm_only",
    "unknown",
)

STAGE_WEIGHTS = {
    "official_harness": 1.25,
    "validation": 1.0,
    "materialization": 0.85,
    "generation": 0.8,
    "localization": 0.8,
    "environment": 0.55,
    "cost": 0.65,
}

EVIDENCE_QUALITY_WEIGHTS = {
    "official_harness": 1.3,
    "validation": 1.0,
    "materialization": 0.85,
    "trace_only": 0.75,
    "llm_only": 0.6,
    "unknown": 0.5,
}

_TARGETS_BY_LABEL = {
    "wrong_file_localization": ("localization", "planning"),
    "repository_understanding_failure": ("planning",),
    "compilation_failure": ("patch_generation", "verification"),
    "test_failure": ("verification", "patch_generation"),
    "regression_introduced": ("verification",),
    "vulnerability_not_fixed": ("security_review", "verification"),
    "tool_misuse": ("planning",),
    "excessive_cost": ("planning", "system"),
}

_TARGETS_BY_SUBCATEGORY = {
    "missing_relevant_file": ("localization",),
    "wrong_function_or_class": ("localization",),
    "docstring_or_header_distraction": ("localization",),
    "insufficient_test_patch_signal": ("planning", "verification"),
    "empty_output": ("patch_generation", "localization"),
    "length_exhausted": ("patch_generation", "planning"),
    "structured_edit_anchor_mismatch": ("localization", "patch_generation"),
    "structured_edit_parse_failed": ("patch_generation",),
    "git_apply_failure": ("patch_generation",),
    "no_patch_intent": ("planning", "patch_generation"),
    "validation_rejected": ("patch_generation", "verification"),
    "api_error": ("planning",),
    "unknown_empty_patch": ("planning",),
    "malformed_json": ("patch_generation",),
    "hallucinated_old_code": ("patch_generation", "localization"),
    "unsafe_or_overbroad_patch": ("patch_generation", "verification"),
    "exact_find_not_found": ("localization", "patch_generation"),
    "line_range_guard_mismatch": ("patch_generation",),
    "partial_edit_rejected": ("patch_generation",),
    "patch_syntax_error": ("patch_generation", "verification"),
    "fail_to_pass_failure": ("verification", "patch_generation"),
    "pass_to_pass_regression": ("verification",),
    "flaky_or_environmental_test": ("planning",),
    "overfit_to_added_tests": ("verification",),
    "validation_environment_failure": ("planning",),
    "validation_patch_failure": ("patch_generation", "verification"),
    "image_missing": ("planning",),
    "testbed_setup_failed": ("planning",),
    "dependency_missing": ("planning",),
    "network_service_failure": ("planning",),
    "gold_failing_instance": ("planning",),
    "excessive_retries": ("planning",),
    "excessive_tokens": ("planning", "system"),
    "timeout_loop": ("planning",),
    "security_review_missing": ("security_review",),
    "vulnerability_pattern_unaddressed": ("security_review",),
}

_NON_ACTIONABLE_SUBCATEGORIES = {
    "length_exhausted",
    "gold_failing_instance",
    "validation_environment_failure",
    "flaky_or_environmental_test",
    "testbed_setup_failed",
    "dependency_missing",
    "network_service_failure",
    "image_missing",
    "api_error",
    "unknown_empty_patch",
}

_NON_ACTIONABLE_TEXT_MARKERS = (
    "gold failing",
    "gold_failing",
    "testbed setup",
    "dependency missing",
    "modulenotfounderror",
    "network",
    "image missing",
    "environmental",
    "flaky",
)

_EMPTY_PATCH_CAUSE_TO_SUBCATEGORY = {
    "api_error": "api_error",
    "length_exhausted": "length_exhausted",
    "structured_edit_anchor_mismatch": "structured_edit_anchor_mismatch",
    "structured_edit_parse_failed": "structured_edit_parse_failed",
    "git_apply_failure": "git_apply_failure",
    "no_patch_intent": "no_patch_intent",
    "validation_rejected": "validation_rejected",
    "unknown": "unknown_empty_patch",
}

_EMPTY_PATCH_SUBCATEGORY_TO_CAUSE = {
    subcategory: cause
    for cause, subcategory in _EMPTY_PATCH_CAUSE_TO_SUBCATEGORY.items()
}

_EMPTY_PATCH_NON_ACTIONABLE_CAUSES = {"api_error", "length_exhausted", "unknown"}


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


def aggregate_failure_diagnosis(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Return ``diagnosis`` enriched with M5.2 pattern aggregation fields.

    The function is intentionally deterministic: pattern grouping, pattern IDs,
    support weighting, representative traces, and optimizer input are all stable
    for a given diagnosis payload.
    """

    enriched = copy.deepcopy(diagnosis)
    analyses = _analysis_records(enriched)
    pattern_items = [_pattern_item(analysis) for analysis in analyses]

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in pattern_items:
        grouped[item["pattern_key"]].append(item)

    failure_patterns = [
        _build_pattern(pattern_key, items)
        for pattern_key, items in sorted(grouped.items(), key=lambda entry: entry[0])
    ]
    failure_patterns.sort(key=_pattern_sort_key)

    top_failure_patterns = failure_patterns[:TOP_PATTERN_LIMIT]
    optimizer_input = _build_optimizer_input(top_failure_patterns, failure_patterns)
    audit = _build_audit(enriched, analyses, failure_patterns)

    enriched["aggregation_schema_version"] = AGGREGATION_SCHEMA_VERSION
    enriched["failure_patterns"] = failure_patterns
    enriched["top_failure_patterns"] = top_failure_patterns
    enriched["optimizer_input"] = optimizer_input
    enriched["aggregation_audit"] = audit
    return enriched


def _analysis_records(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
    analyses = diagnosis.get("failure_analyses")
    if isinstance(analyses, list) and analyses:
        return [analysis for analysis in analyses if isinstance(analysis, dict)]

    fallback = []
    for entry in diagnosis.get("per_task_diagnosis", []) or []:
        if not isinstance(entry, dict):
            continue
        fallback.append(
            {
                "task_id": entry.get("task_id"),
                "category": entry.get("category"),
                "execution_status": entry.get("execution_status"),
                "labels": entry.get("labels", []),
                "primary_label": entry.get("primary_label"),
                "subcategories": entry.get("subcategories", []),
                "failure_stage": entry.get("failure_stage", "unknown"),
                "evidence": [],
                "confidence": entry.get("confidence", 0.5),
                "optimizer_hint": entry.get("optimizer_hint", ""),
                "critic_source": entry.get("critic_source", "unknown"),
            }
        )
    return fallback


def _normalize_empty_patch_subcategories(
    analysis: dict[str, Any],
    subcategories: list[Any],
) -> tuple[str, ...]:
    cleaned = [_clean_identifier(value) for value in subcategories]
    cause = _empty_patch_cause_from_analysis(analysis, tuple(cleaned))
    if cause:
        mapped = _EMPTY_PATCH_CAUSE_TO_SUBCATEGORY.get(cause, "unknown_empty_patch")
        if "empty_output" in cleaned and mapped not in cleaned:
            cleaned.append(mapped)
    return tuple(sorted(_dedupe(cleaned)))


def _empty_patch_cause_from_analysis(
    analysis: dict[str, Any],
    subcategories: tuple[str, ...],
) -> str:
    cause = _clean_optional_identifier(analysis.get("empty_patch_cause"))
    if cause and cause != "unknown":
        return cause
    if cause == "unknown":
        return "unknown"
    for subcategory in subcategories:
        mapped = _EMPTY_PATCH_SUBCATEGORY_TO_CAUSE.get(subcategory)
        if mapped:
            return mapped
    if analysis.get("empty_patch") is True and "empty_output" in subcategories:
        return "unknown"
    return ""


def _empty_patch_actionability(cause: str, actionable: bool) -> str:
    if not cause:
        return ""
    if cause in _EMPTY_PATCH_NON_ACTIONABLE_CAUSES or not actionable:
        return "non_actionable"
    return "prompt_learnable"


def _empty_patch_cause_summary(
    failure_patterns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for pattern in failure_patterns:
        cause = str(pattern.get("empty_patch_cause") or "")
        if not cause:
            continue
        entry = grouped.setdefault(
            cause,
            {
                "cause": cause,
                "support_count": 0,
                "weighted_support": 0.0,
                "pattern_ids": [],
                "optimizer_targets": [],
                "actionability": "prompt_learnable",
                "recommended_delta": _empty_patch_recommended_delta(cause),
            },
        )
        entry["support_count"] += int(pattern.get("support_count") or 0)
        entry["weighted_support"] += float(pattern.get("weighted_support") or 0.0)
        entry["pattern_ids"].append(str(pattern.get("pattern_id") or ""))
        entry["optimizer_targets"] = _ordered_targets(
            [
                *entry["optimizer_targets"],
                *[
                    str(module)
                    for module in pattern.get("optimizer_targets", [])
                    if module in PROMPT_MODULES
                ],
            ]
        )
        if (
            pattern.get("empty_patch_actionability") == "non_actionable"
            or not pattern.get("actionable", True)
        ):
            entry["actionability"] = "non_actionable"
    summary = []
    for entry in grouped.values():
        normalized = dict(entry)
        normalized["weighted_support"] = _round_metric(normalized["weighted_support"])
        normalized["pattern_ids"] = [
            pattern_id for pattern_id in normalized["pattern_ids"] if pattern_id
        ][:TOP_PATTERN_LIMIT]
        summary.append(normalized)
    summary.sort(
        key=lambda item: (
            -float(item.get("weighted_support", 0.0)),
            -int(item.get("support_count", 0)),
            str(item.get("cause", "")),
        )
    )
    return summary


def _empty_patch_recommended_delta(cause: str) -> str:
    if cause == "structured_edit_anchor_mismatch":
        return (
            "When exact structured-edit anchors fail, broaden to nearby stable "
            "symbols and emit compact replacements rather than ending with no patch."
        )
    if cause == "length_exhausted":
        return (
            "Treat length exhaustion as budget/truncation evidence; do not "
            "spend prompt-update slots on it when correctness evidence exists."
        )
    if cause == "structured_edit_parse_failed":
        return "Require parseable structured edits or fall back to a minimal unified diff."
    if cause == "git_apply_failure":
        return "Emit applyable unified diffs with valid file headers and minimal hunks."
    if cause == "no_patch_intent":
        return (
            "Continue issue-linked localization before deciding no code change is needed."
        )
    if cause == "validation_rejected":
        return "Treat validation rejection as a bounded repair signal before finalizing."
    return "Do not learn a generic empty-patch lesson without a concrete cause."


def _ordered_targets(modules: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered = []
    for module in PROMPT_MODULES:
        if module in modules and module not in seen:
            seen.add(module)
            ordered.append(module)
    return ordered


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _pattern_item(analysis: dict[str, Any]) -> dict[str, Any]:
    primary_label = _clean_identifier(analysis.get("primary_label") or "unknown")
    labels = _normalize_labels(primary_label, analysis.get("labels", []))
    subcategories = _normalize_empty_patch_subcategories(
        analysis,
        _as_list(analysis.get("subcategories")),
    )
    empty_patch_cause = _empty_patch_cause_from_analysis(analysis, subcategories)
    failure_stage = _clean_identifier(analysis.get("failure_stage") or "unknown")
    evidence_quality = _infer_evidence_quality(analysis)
    confidence = _confidence(analysis.get("confidence"))
    item_weight = (
        confidence
        * STAGE_WEIGHTS.get(failure_stage, 0.7)
        * EVIDENCE_QUALITY_WEIGHTS.get(evidence_quality, 0.5)
    )

    pattern_key = (
        primary_label,
        labels,
        subcategories,
        failure_stage,
        evidence_quality,
        empty_patch_cause,
    )
    return {
        "analysis": analysis,
        "pattern_key": pattern_key,
        "primary_label": primary_label,
        "labels": labels,
        "subcategories": subcategories,
        "failure_stage": failure_stage,
        "evidence_quality": evidence_quality,
        "empty_patch_cause": empty_patch_cause,
        "confidence": confidence,
        "item_weight": item_weight,
    }


def _build_pattern(
    pattern_key: tuple[Any, ...],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    primary_label, labels, subcategories, failure_stage, evidence_quality, empty_patch_cause = pattern_key
    weighted_support = sum(item["item_weight"] for item in items)
    mean_confidence = sum(item["confidence"] for item in items) / max(len(items), 1)
    task_ids = sorted(
        str(item["analysis"].get("task_id") or "unknown") for item in items
    )
    optimizer_targets = _optimizer_targets(primary_label, subcategories)
    actionable = _is_actionable(primary_label, subcategories, failure_stage, items)
    empty_patch_actionability = _empty_patch_actionability(
        empty_patch_cause,
        actionable,
    )
    representatives = _representative_traces(items)

    low_support = _is_low_support_pattern(
        support_count=len(items),
        primary_label=primary_label,
        subcategories=subcategories,
        failure_stage=failure_stage,
        evidence_quality=evidence_quality,
    )
    pattern = {
        "pattern_id": _pattern_id(pattern_key),
        "primary_label": primary_label,
        "labels": list(labels),
        "subcategories": list(subcategories),
        "failure_stage": failure_stage,
        "evidence_quality": evidence_quality,
        "empty_patch_cause": empty_patch_cause,
        "empty_patch_actionability": empty_patch_actionability,
        "support_count": len(items),
        "weighted_support": _round_metric(weighted_support),
        "mean_confidence": _round_metric(mean_confidence),
        "task_ids": task_ids,
        "representative_traces": representatives,
        "optimizer_targets": optimizer_targets,
        "optimizer_hint_summary": _optimizer_hint_summary(items, primary_label),
        "low_support": low_support,
        "actionable": actionable,
    }
    return pattern


def _is_low_support_pattern(
    *,
    support_count: int,
    primary_label: str,
    subcategories: tuple[str, ...],
    failure_stage: str,
    evidence_quality: str,
) -> bool:
    if support_count >= 2:
        return False
    if (
        failure_stage == "official_harness"
        and evidence_quality == "official_harness"
        and (
            (
                primary_label == "test_failure"
                and "fail_to_pass_failure" in subcategories
            )
            or (
                primary_label == "regression_introduced"
                and "pass_to_pass_regression" in subcategories
            )
        )
    ):
        return False
    return True


def _pattern_sort_key(pattern: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -float(pattern.get("weighted_support", 0.0)),
        -int(pattern.get("support_count", 0)),
        str(pattern.get("primary_label", "")),
        str(pattern.get("pattern_id", "")),
    )


def _pattern_id(pattern_key: tuple[Any, ...]) -> str:
    primary_label, labels, subcategories, failure_stage, evidence_quality, empty_patch_cause = pattern_key
    payload = json.dumps(
        {
            "primary_label": primary_label,
            "labels": list(labels),
            "subcategories": list(subcategories),
            "failure_stage": failure_stage,
            "evidence_quality": evidence_quality,
            "empty_patch_cause": empty_patch_cause,
        },
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
    readable = "__".join(
        _slug(part)
        for part in (
            primary_label,
            failure_stage,
            evidence_quality,
            "_".join(subcategories) or "none",
            empty_patch_cause or "no_empty_patch_cause",
        )
    )
    return f"{readable}__{digest}"


def _infer_evidence_quality(analysis: dict[str, Any]) -> str:
    stage = _clean_identifier(analysis.get("failure_stage") or "")
    evidence = analysis.get("evidence", []) or []
    sources = " ".join(
        str(item.get("source", ""))
        for item in evidence
        if isinstance(item, dict)
    ).lower()

    if stage == "official_harness" or "official_harness" in sources or "harness" in sources:
        return "official_harness"
    if stage == "validation" or "command_results" in sources or "validation" in sources:
        return "validation"
    if stage == "materialization" or "patch_materializer" in sources:
        return "materialization"
    if evidence:
        return "trace_only"
    if analysis.get("critic_source") == "llm":
        return "llm_only"
    return "unknown"


def _optimizer_targets(
    primary_label: str,
    subcategories: tuple[str, ...],
) -> list[str]:
    targets: list[str] = []
    targets.extend(_TARGETS_BY_LABEL.get(primary_label, ()))
    for subcategory in subcategories:
        targets.extend(_TARGETS_BY_SUBCATEGORY.get(subcategory, ()))

    allowed = set(PROMPT_MODULES)
    seen: set[str] = set()
    ordered = []
    for module in PROMPT_MODULES:
        if module in targets and module in allowed and module not in seen:
            seen.add(module)
            ordered.append(module)
    return ordered


def _representative_traces(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        items,
        key=lambda item: (
            -float(item["item_weight"]),
            str(item["analysis"].get("task_id") or "unknown"),
        ),
    )
    traces = []
    for item in ranked[:REPRESENTATIVE_TRACE_LIMIT]:
        analysis = item["analysis"]
        traces.append(
            {
                "task_id": str(analysis.get("task_id") or "unknown"),
                "evidence": _compact_evidence(analysis.get("evidence", [])),
                "optimizer_hint": _clean_text(analysis.get("optimizer_hint", ""), 240),
            }
        )
    return traces


def _compact_evidence(evidence: Any) -> list[dict[str, str]]:
    compact = []
    for entry in _as_list(evidence)[:2]:
        if not isinstance(entry, dict):
            continue
        compact.append(
            {
                "source": _clean_text(entry.get("source", ""), 120),
                "summary": _clean_text(entry.get("summary", ""), 240),
                "quote": _clean_text(entry.get("quote", ""), 160),
            }
        )
    return compact


def _optimizer_hint_summary(items: list[dict[str, Any]], primary_label: str) -> str:
    hints: list[str] = []
    ranked = sorted(
        items,
        key=lambda item: (
            -float(item["item_weight"]),
            str(item["analysis"].get("task_id") or "unknown"),
        ),
    )
    for item in ranked:
        hint = _clean_text(item["analysis"].get("optimizer_hint", ""), 220)
        if hint and hint not in hints:
            hints.append(hint)
        if len(hints) == 2:
            break
    if hints:
        return " | ".join(hints)
    return f"Address {primary_label} using the cited evidence."


def _is_actionable(
    primary_label: str,
    subcategories: tuple[str, ...],
    failure_stage: str,
    items: list[dict[str, Any]],
) -> bool:
    if set(subcategories) & _NON_ACTIONABLE_SUBCATEGORIES:
        return False
    if failure_stage == "environment" and primary_label in {
        "repository_understanding_failure",
        "tool_misuse",
    }:
        return False
    evidence_text = " ".join(
        _evidence_text(item["analysis"].get("evidence", []))
        for item in items
    ).lower()
    if any(marker in evidence_text for marker in _NON_ACTIONABLE_TEXT_MARKERS):
        return False
    return True


def _build_optimizer_input(
    top_failure_patterns: list[dict[str, Any]],
    failure_patterns: list[dict[str, Any]],
) -> dict[str, Any]:
    dominant_patterns = [
        _compact_pattern_for_optimizer(pattern)
        for pattern in top_failure_patterns
        if _is_optimizer_actionable_pattern(pattern)
    ]
    recommended_targets = _recommended_update_targets(dominant_patterns)
    blocked = [
        {
            "pattern_id": pattern["pattern_id"],
            "primary_label": pattern["primary_label"],
            "support_count": pattern["support_count"],
            "mean_confidence": pattern["mean_confidence"],
            "low_support": pattern["low_support"],
            "actionable": pattern["actionable"],
            "subcategories": list(pattern.get("subcategories", [])),
            "empty_patch_cause": pattern.get("empty_patch_cause", ""),
            "empty_patch_actionability": pattern.get("empty_patch_actionability", ""),
        }
        for pattern in failure_patterns
        if (
            pattern.get("low_support")
            or not pattern.get("actionable", True)
            or pattern.get("empty_patch_actionability") == "non_actionable"
            or float(pattern.get("mean_confidence", 0.0)) < LOW_CONFIDENCE_THRESHOLD
        )
    ]
    return {
        "dominant_patterns": dominant_patterns,
        "recommended_update_targets": recommended_targets,
        "blocked_or_low_confidence_patterns": blocked,
        "evidence_summary": _evidence_summary(failure_patterns, recommended_targets),
        "empty_patch_cause_summary": _empty_patch_cause_summary(failure_patterns),
    }


def _is_optimizer_actionable_pattern(pattern: dict[str, Any]) -> bool:
    return bool(
        pattern.get("actionable", True)
        and pattern.get("empty_patch_actionability") != "non_actionable"
    )


def _compact_pattern_for_optimizer(pattern: dict[str, Any]) -> dict[str, Any]:
    return {
        "pattern_id": pattern["pattern_id"],
        "primary_label": pattern["primary_label"],
        "subcategories": list(pattern.get("subcategories", [])),
        "failure_stage": pattern["failure_stage"],
        "evidence_quality": pattern["evidence_quality"],
        "empty_patch_cause": pattern.get("empty_patch_cause", ""),
        "empty_patch_actionability": pattern.get("empty_patch_actionability", ""),
        "support_count": pattern["support_count"],
        "weighted_support": pattern["weighted_support"],
        "mean_confidence": pattern["mean_confidence"],
        "optimizer_targets": list(pattern.get("optimizer_targets", [])),
        "optimizer_hint_summary": _clean_text(
            pattern.get("optimizer_hint_summary", ""),
            300,
        ),
        "low_support": bool(pattern.get("low_support", False)),
    }


def _recommended_update_targets(dominant_patterns: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []
    for pattern in dominant_patterns:
        for target in pattern.get("optimizer_targets", []):
            if target in PROMPT_MODULES and target not in seen:
                seen.add(target)
                targets.append(target)
    return targets


def _evidence_summary(
    failure_patterns: list[dict[str, Any]],
    recommended_targets: list[str],
) -> dict[str, Any]:
    by_quality = Counter(str(pattern.get("evidence_quality", "unknown")) for pattern in failure_patterns)
    by_stage = Counter(str(pattern.get("failure_stage", "unknown")) for pattern in failure_patterns)
    return {
        "pattern_count": len(failure_patterns),
        "by_evidence_quality": dict(sorted(by_quality.items())),
        "by_failure_stage": dict(sorted(by_stage.items())),
        "top_pattern_ids": [
            str(pattern.get("pattern_id", ""))
            for pattern in failure_patterns[:TOP_PATTERN_LIMIT]
        ],
        "recommended_update_targets": list(recommended_targets),
        "by_empty_patch_cause": dict(
            sorted(
                Counter(
                    str(pattern.get("empty_patch_cause", ""))
                    for pattern in failure_patterns
                    if pattern.get("empty_patch_cause")
                ).items()
            )
        ),
    }


def _build_audit(
    diagnosis: dict[str, Any],
    analyses: list[dict[str, Any]],
    failure_patterns: list[dict[str, Any]],
) -> dict[str, Any]:
    critic_sources = sorted(
        {
            str(analysis.get("critic_source") or "unknown")
            for analysis in analyses
        }
    )
    prompt_tokens = 0
    completion_tokens = 0
    for analysis in analyses:
        usage = analysis.get("critic_usage")
        if isinstance(usage, dict):
            prompt_tokens += _int_or_zero(usage.get("prompt_tokens"))
            completion_tokens += _int_or_zero(usage.get("completion_tokens"))
    return {
        "failed_task_count": int(diagnosis.get("failed_task_count") or len(analyses)),
        "critic_sources": critic_sources,
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "low_support_pattern_count": sum(
            1 for pattern in failure_patterns if pattern.get("low_support")
        ),
        "non_actionable_pattern_count": sum(
            1 for pattern in failure_patterns if not pattern.get("actionable", True)
        ),
        "empty_patch_cause_pattern_count": sum(
            1 for pattern in failure_patterns if pattern.get("empty_patch_cause")
        ),
    }


def _normalize_labels(primary_label: str, labels: Any) -> tuple[str, ...]:
    cleaned = [_clean_identifier(label) for label in _as_list(labels)]
    if primary_label not in cleaned:
        cleaned.insert(0, primary_label)
    remainder = sorted(label for label in cleaned if label != primary_label)
    return tuple([primary_label, *remainder])


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(parsed, 1.0))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _clean_identifier(value: Any) -> str:
    text = str(value or "unknown").strip()
    return text if text else "unknown"


def _clean_optional_identifier(value: Any) -> str:
    return str(value or "").strip()


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: max(limit - 3, 0)] + "..."
    return text


def _evidence_text(evidence: Any) -> str:
    parts: list[str] = []
    for entry in _as_list(evidence):
        if not isinstance(entry, dict):
            continue
        parts.extend(
            str(entry.get(key, ""))
            for key in ("source", "summary", "quote")
        )
    return " ".join(parts)


def _slug(value: Any) -> str:
    text = str(value or "unknown").lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "unknown"


def _round_metric(value: float) -> float:
    return round(float(value), 6)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
