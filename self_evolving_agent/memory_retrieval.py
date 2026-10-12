"""M6.6 prompt-safe memory retrieval and block assembly.

This module is the controlled exposure layer for active memory. It reads an
M6.5 active-memory store, selects prompt-safe M6.4 memory views, and assembles a
structured GPT-visible memory block plus a retrieval audit. It intentionally
does not update memory, call an LLM, run harnesses, or integrate with the
optimizer.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import math
import re
from typing import Any

from self_evolving_agent.active_memory_store import (
    validate_active_memory_store,
    validate_active_memory_store_item,
)
from self_evolving_agent.memory_candidate import EVIDENCE_SOURCE_STRENGTHS


MEMORY_QUERY_CONTEXT_SCHEMA_VERSION = "m6.6.memory_query_context.v1"
MEMORY_RETRIEVAL_RESULT_SCHEMA_VERSION = "m6.6.memory_retrieval_result.v1"
MEMORY_BLOCK_SCHEMA_VERSION = "m6.6.memory_block.v1"
MEMORY_BLOCK_ITEM_SCHEMA_VERSION = "m6.6.memory_block_item.v1"

RETRIEVAL_PURPOSES = ("prompt_optimization", "task_execution", "reporting")
SECTION_ORDER = (
    "memory_use_rules",
    "critical_negative",
    "cautions",
    "contradictions_and_scope_limits",
    "global_invariants",
    "benchmark_lessons",
    "repo_lessons",
    "task_lessons",
)
LESSON_SECTIONS = tuple(section for section in SECTION_ORDER if section != "memory_use_rules")

DEFAULT_MAX_ITEMS = 8
DEFAULT_MAX_ESTIMATED_TOKENS = 1200
MIN_PROMPT_BUDGET_TOKENS = 400
PROMPT_BUDGET_FRACTION = 0.12

DEFAULT_SECTION_CAPS = {
    "critical_negative": 3,
    "cautions": 3,
    "contradictions_and_scope_limits": 2,
    "global_invariants": 2,
    "benchmark_lessons": 2,
    "repo_lessons": 2,
    "task_lessons": 1,
}
NEGATIVE_CAUTION_COMBINED_CAP = 3

NEGATIVE_SOURCE_TYPES = {"promotion_rejection", "safety_rejection"}
INFRASTRUCTURE_FAILURES = {
    "validation_environment_failure",
    "conda_env_missing",
    "infrastructure_failure",
    "api_error",
    "unknown_apply_failure",
    "unknown_failure",
}
CRITICAL_NEGATIVE_CAUSES = {
    "pass_to_pass_regression",
    "regression_introduced",
    "prompt_update_rejected",
    "promotion_rejection",
}

SECTION_PRIORITY = {
    "critical_negative": 100,
    "cautions": 90,
    "contradictions_and_scope_limits": 80,
    "global_invariants": 70,
    "benchmark_lessons": 60,
    "repo_lessons": 50,
    "task_lessons": 40,
}
PROMPT_OPTIMIZATION_SCOPE_SCORE = {
    "global": 28,
    "benchmark": 24,
    "repo": 18,
    "task": 8,
}
LOCAL_SCOPE_SCORE = {
    "task": 28,
    "repo": 24,
    "benchmark": 18,
    "global": 12,
}
SOURCE_TYPE_SCORE = {
    "official_harness": 14,
    "promotion_decision": 12,
    "root_cause_event": 10,
    "materializer_trace": 8,
    "safety_rejection": 8,
    "builder_rejection": 7,
    "promotion_rejection": 7,
    "optimizer_history_only": 2,
}
TASK_MEMORY_PENALTY = 20
WEAK_OR_INFRA_PENALTY = 20

MEMORY_USE_RULES = (
    "Use these memories as scoped evidence, not as hidden facts.",
    "Do not generalize task or repo memories beyond their stated scope.",
    "If current verified evidence conflicts with memory, trust current verified evidence.",
    "Do not use hidden tests, gold patches, raw logs, file paths, task ids, or repo ids.",
    "Any later memory-based optimizer decision must cite memory_id.",
    "Separate evidence, hypothesis, and decision.",
)

PROMPT_VISIBLE_FIELDS = {
    "memory_id",
    "scope",
    "scope_id",
    "root_cause",
    "prompt_module",
    "lesson",
    "expected_effect",
    "status",
    "actionability",
    "confidence",
    "support_count",
    "source_types",
    "scope_caveat",
}

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
    "task_id",
    "task_ids",
    "repo_id",
    "repo_ids",
    "repo_path",
    "target_files",
    "file_path",
    "line_number",
    "line_numbers",
)

FORBIDDEN_PROMPT_CONTENT_MARKERS = (
    "gold_patch",
    "gold diff",
    "reference patch",
    "test_patch",
    "hidden test",
    "hidden_test",
    "raw stdout",
    "raw stderr",
    "official stdout",
    "official stderr",
    "traceback",
)

SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|authorization|bearer|token|password|secret)"
        r"(\s*[:=]\s*)([^\s,'\"]+)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"),
)


class MemoryRetrievalValidationError(ValueError):
    """Raised when an M6.6 query, retrieval result, or block is malformed."""


def build_memory_query_context(
    *,
    retrieval_purpose: str = "prompt_optimization",
    benchmark_id: str = "",
    repo_id: str = "",
    task_ids: list[str] | tuple[str, ...] | None = None,
    prompt_module: str = "",
    root_causes: list[str] | tuple[str, ...] | None = None,
    failure_types: list[str] | tuple[str, ...] | None = None,
    round_index: int = 0,
    total_prompt_budget_tokens: int | None = None,
) -> dict[str, Any]:
    """Build and validate a prompt-safe memory query context."""

    query = {
        "schema_version": MEMORY_QUERY_CONTEXT_SCHEMA_VERSION,
        "retrieval_purpose": str(retrieval_purpose or "prompt_optimization"),
        "benchmark_id": str(benchmark_id or ""),
        "repo_id": str(repo_id or ""),
        "task_ids": _string_list(task_ids or []),
        "prompt_module": str(prompt_module or ""),
        "root_causes": _string_list(root_causes or []),
        "failure_types": _string_list(failure_types or []),
        "round_index": int(round_index),
        "total_prompt_budget_tokens": total_prompt_budget_tokens,
    }
    validate_memory_query_context(query)
    return query


def validate_memory_query_context(query: dict[str, Any]) -> None:
    """Validate an M6.6 retrieval query context."""

    if not isinstance(query, dict):
        raise MemoryRetrievalValidationError("query context must be a mapping")
    for key in (
        "schema_version",
        "retrieval_purpose",
        "benchmark_id",
        "repo_id",
        "task_ids",
        "prompt_module",
        "root_causes",
        "failure_types",
        "round_index",
        "total_prompt_budget_tokens",
    ):
        if key not in query:
            raise MemoryRetrievalValidationError(f"missing query field: {key}")
    if query["schema_version"] != MEMORY_QUERY_CONTEXT_SCHEMA_VERSION:
        raise MemoryRetrievalValidationError("unsupported query schema_version")
    if query["retrieval_purpose"] not in RETRIEVAL_PURPOSES:
        raise MemoryRetrievalValidationError("invalid retrieval_purpose")
    for key in ("benchmark_id", "repo_id", "prompt_module"):
        if not isinstance(query[key], str):
            raise MemoryRetrievalValidationError(f"{key} must be a string")
    for key in ("task_ids", "root_causes", "failure_types"):
        _validate_string_list(query[key], key)
    if not isinstance(query["round_index"], int) or query["round_index"] < 0:
        raise MemoryRetrievalValidationError("round_index must be a non-negative integer")
    total_budget = query["total_prompt_budget_tokens"]
    if total_budget is not None and (
        not isinstance(total_budget, int) or total_budget <= 0
    ):
        raise MemoryRetrievalValidationError(
            "total_prompt_budget_tokens must be null or a positive integer"
        )


def retrieve_memory_items(
    active_store: dict[str, Any],
    query_context: dict[str, Any],
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return prompt-safe selected items and an audit for a memory query."""

    validate_active_memory_store(active_store)
    validate_memory_query_context(query_context)
    normalized_budget = _normalize_budget(query_context, budget)
    candidates: list[dict[str, Any]] = []
    selected_items: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    blocked_ids: list[str] = []
    audit: list[dict[str, Any]] = []

    for wrapper in active_store.get("memories", []):
        memory_id = _wrapper_memory_id(wrapper)
        try:
            validate_active_memory_store_item(wrapper)
            eligible, reasons = _eligibility_reasons(wrapper, query_context)
            if not eligible:
                blocked_ids.append(memory_id)
                audit.append(_audit_record(memory_id, "blocked", reasons))
                continue
            block_item = sanitize_memory_item_for_block(wrapper, query_context)
            ranking = rank_memory_item(wrapper, query_context)
            candidates.append(
                {
                    "wrapper": wrapper,
                    "item": block_item,
                    "ranking": ranking,
                    "estimated_tokens": estimate_memory_block_item_tokens(block_item),
                    "last_updated_at": str(
                        wrapper.get("store_metadata", {}).get("last_updated_at", "")
                    ),
                }
            )
        except MemoryRetrievalValidationError as exc:
            blocked_ids.append(memory_id)
            audit.append(_audit_record(memory_id, "blocked", [_safe_reason(str(exc))]))

    candidates = sorted(candidates, key=_candidate_sort_key)
    used_tokens = sum(
        estimate_memory_block_item_tokens(rule) for rule in MEMORY_USE_RULES
    )
    section_counts = {section: 0 for section in LESSON_SECTIONS}
    negative_caution_count = 0
    for candidate in candidates:
        item = candidate["item"]
        section = item["section"]
        memory_id = item["memory_id"]
        reasons: list[str] = []
        if len(selected_items) >= normalized_budget["max_items"]:
            reasons.append("max_items_exceeded")
        if section_counts[section] >= normalized_budget["section_caps"][section]:
            reasons.append("section_cap_exceeded")
        if section in {"critical_negative", "cautions"} and (
            negative_caution_count >= normalized_budget["negative_caution_combined_cap"]
        ):
            reasons.append("negative_caution_cap_exceeded")
        if used_tokens + candidate["estimated_tokens"] > normalized_budget["max_estimated_tokens"]:
            reasons.append("token_budget_exceeded")

        if reasons:
            blocked_ids.append(memory_id)
            audit.append(
                _audit_record(
                    memory_id,
                    "dropped",
                    reasons,
                    score=candidate["ranking"]["score"],
                    section=section,
                )
            )
            continue

        selected_items.append(item)
        selected_ids.append(memory_id)
        section_counts[section] += 1
        if section in {"critical_negative", "cautions"}:
            negative_caution_count += 1
        used_tokens += candidate["estimated_tokens"]
        audit.append(
            _audit_record(
                memory_id,
                "selected",
                ["eligible"],
                score=candidate["ranking"]["score"],
                section=section,
            )
        )

    selected_items = sorted(
        selected_items,
        key=lambda item: (
            SECTION_ORDER.index(item["section"]),
            selected_ids.index(item["memory_id"]),
        ),
    )
    selected_ids = [item["memory_id"] for item in selected_items]
    result = {
        "schema_version": MEMORY_RETRIEVAL_RESULT_SCHEMA_VERSION,
        "query_context": _jsonable(deepcopy(query_context)),
        "budget": {
            **normalized_budget,
            "used_estimated_tokens": used_tokens,
        },
        "selected_items": selected_items,
        "selected_memory_ids": selected_ids,
        "blocked_memory_ids": _dedupe(blocked_ids),
        "retrieval_audit": audit,
    }
    validate_memory_retrieval_result(result)
    return result


def validate_memory_retrieval_result(result: dict[str, Any]) -> None:
    """Validate a structured M6.6 retrieval result."""

    if not isinstance(result, dict):
        raise MemoryRetrievalValidationError("retrieval result must be a mapping")
    for key in (
        "schema_version",
        "query_context",
        "budget",
        "selected_items",
        "selected_memory_ids",
        "blocked_memory_ids",
        "retrieval_audit",
    ):
        if key not in result:
            raise MemoryRetrievalValidationError(f"missing retrieval result field: {key}")
    if result["schema_version"] != MEMORY_RETRIEVAL_RESULT_SCHEMA_VERSION:
        raise MemoryRetrievalValidationError("unsupported retrieval result schema_version")
    validate_memory_query_context(result["query_context"])
    _validate_budget(result["budget"])
    for key in ("selected_items", "retrieval_audit"):
        if not isinstance(result[key], list):
            raise MemoryRetrievalValidationError(f"{key} must be a list")
    _validate_string_list(result["selected_memory_ids"], "selected_memory_ids")
    _validate_string_list(result["blocked_memory_ids"], "blocked_memory_ids")
    for item in result["selected_items"]:
        _validate_block_item(item)
    for record in result["retrieval_audit"]:
        if not isinstance(record, dict):
            raise MemoryRetrievalValidationError("retrieval audit entries must be mappings")
        _assert_prompt_safe(record)


def build_memory_block(
    active_store: dict[str, Any],
    query_context: dict[str, Any],
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured GPT-visible memory block plus retrieval audit."""

    retrieval = retrieve_memory_items(active_store, query_context, budget)
    sections = {section: [] for section in SECTION_ORDER}
    sections["memory_use_rules"] = list(MEMORY_USE_RULES)
    for item in retrieval["selected_items"]:
        sections[item["section"]].append(item)

    block = {
        "schema_version": MEMORY_BLOCK_SCHEMA_VERSION,
        "query_context": retrieval["query_context"],
        "budget": {
            **retrieval["budget"],
            "used_estimated_tokens": 0,
        },
        "prompt_sections": sections,
        "selected_memory_ids": retrieval["selected_memory_ids"],
        "blocked_memory_ids": retrieval["blocked_memory_ids"],
        "retrieval_audit": retrieval["retrieval_audit"],
    }
    block["budget"]["used_estimated_tokens"] = estimate_memory_block_tokens(block)
    validate_memory_block(block)
    return block


def validate_memory_block(block: dict[str, Any]) -> None:
    """Validate a structured M6.6 memory block."""

    if not isinstance(block, dict):
        raise MemoryRetrievalValidationError("memory block must be a mapping")
    for key in (
        "schema_version",
        "query_context",
        "budget",
        "prompt_sections",
        "selected_memory_ids",
        "blocked_memory_ids",
        "retrieval_audit",
    ):
        if key not in block:
            raise MemoryRetrievalValidationError(f"missing memory block field: {key}")
    if block["schema_version"] != MEMORY_BLOCK_SCHEMA_VERSION:
        raise MemoryRetrievalValidationError("unsupported memory block schema_version")
    validate_memory_query_context(block["query_context"])
    _validate_budget(block["budget"], require_used=True)
    if not isinstance(block["prompt_sections"], dict):
        raise MemoryRetrievalValidationError("prompt_sections must be a mapping")
    for section in SECTION_ORDER:
        if section not in block["prompt_sections"]:
            raise MemoryRetrievalValidationError(f"missing prompt section: {section}")
        if not isinstance(block["prompt_sections"][section], list):
            raise MemoryRetrievalValidationError(f"{section} must be a list")
    for section, items in block["prompt_sections"].items():
        if section not in SECTION_ORDER:
            raise MemoryRetrievalValidationError(f"unknown prompt section: {section}")
        for item in items:
            if section == "memory_use_rules":
                if not isinstance(item, str):
                    raise MemoryRetrievalValidationError("memory_use_rules must contain strings")
                _assert_prompt_safe(item, allow_policy_terms=True)
            else:
                _validate_block_item(item)
                if item["section"] != section:
                    raise MemoryRetrievalValidationError("block item section mismatch")
    _validate_string_list(block["selected_memory_ids"], "selected_memory_ids")
    _validate_string_list(block["blocked_memory_ids"], "blocked_memory_ids")
    if not isinstance(block["retrieval_audit"], list):
        raise MemoryRetrievalValidationError("retrieval_audit must be a list")
    for record in block["retrieval_audit"]:
        if not isinstance(record, dict):
            raise MemoryRetrievalValidationError("retrieval audit entries must be mappings")
        _assert_prompt_safe(record)


def render_memory_block_text(block: dict[str, Any]) -> str:
    """Render a deterministic GPT-visible memory block."""

    validate_memory_block(block)
    lines = ["<trace2prompt_memory>"]
    for section in SECTION_ORDER:
        items = block["prompt_sections"][section]
        if not items:
            continue
        lines.append(f"[{section}]")
        if section == "memory_use_rules":
            lines.extend(f"- {rule}" for rule in items)
            continue
        for item in items:
            lines.append(
                "- "
                + json.dumps(
                    _renderable_item(item),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            )
    lines.append("</trace2prompt_memory>")
    rendered = "\n".join(lines)
    _assert_prompt_safe(rendered, allow_policy_terms=True)
    return rendered


def estimate_memory_block_item_tokens(item: dict[str, Any] | str) -> int:
    """Estimate prompt tokens using a deterministic JSON-length heuristic."""

    encoded = json.dumps(item, sort_keys=True, ensure_ascii=True)
    return int(math.ceil(len(encoded) / 4.0)) + 8


def estimate_memory_block_tokens(block: dict[str, Any]) -> int:
    """Estimate GPT-visible memory block tokens, excluding retrieval audit."""

    if not isinstance(block, dict) or not isinstance(block.get("prompt_sections"), dict):
        raise MemoryRetrievalValidationError("memory block prompt_sections missing")
    total = 0
    for section in SECTION_ORDER:
        for item in block["prompt_sections"].get(section, []):
            total += estimate_memory_block_item_tokens(item)
    return total


def sanitize_memory_item_for_block(
    store_item: dict[str, Any],
    query_context: dict[str, Any],
) -> dict[str, Any]:
    """Return a prompt-safe block item for an active-memory store wrapper."""

    validate_memory_query_context(query_context)
    validate_active_memory_store_item(store_item)
    memory = store_item["memory"]
    view = memory.get("prompt_injection_view", {})
    if not isinstance(view, dict):
        raise MemoryRetrievalValidationError("missing prompt_injection_view")
    _assert_prompt_safe(view)
    _validate_source_types(memory.get("source_types", []))

    section = assign_memory_section(store_item, query_context)
    item = {
        "schema_version": MEMORY_BLOCK_ITEM_SCHEMA_VERSION,
        "memory_id": str(view.get("memory_id") or memory.get("memory_id") or ""),
        "section": section,
        "scope": str(view.get("scope") or memory.get("scope") or ""),
        "scope_caveat": _scope_caveat(str(memory.get("scope") or "")),
        "root_cause": str(view.get("root_cause") or memory.get("root_cause") or ""),
        "prompt_module": str(view.get("prompt_module") or memory.get("prompt_module") or ""),
        "lesson": str(
            view.get("lesson")
            or view.get("normalized_lesson")
            or memory.get("normalized_lesson")
            or ""
        ),
        "expected_effect": str(
            view.get("expected_effect") or memory.get("expected_effect") or ""
        ),
        "status": str(view.get("status") or memory.get("status") or ""),
        "actionability": str(view.get("actionability") or _actionability(memory)),
        "confidence": _float_or_default(
            view.get("confidence", memory.get("confidence", 0.0)),
            0.0,
        ),
        "support_count": _int_or_zero(
            view.get("support_count", memory.get("support_count", 0))
        ),
        "source_types": _string_list(view.get("source_types") or memory.get("source_types") or []),
    }
    safe_scope_id = str(view.get("scope_id") or "")
    if safe_scope_id and item["scope"] in {"benchmark", "global"}:
        item["scope_id"] = safe_scope_id
    _validate_block_item(item)
    return item


def rank_memory_item(
    store_item: dict[str, Any],
    query_context: dict[str, Any],
) -> dict[str, Any]:
    """Return deterministic ranking data for an active-memory store wrapper."""

    validate_memory_query_context(query_context)
    validate_active_memory_store_item(store_item)
    memory = store_item["memory"]
    section = assign_memory_section(store_item, query_context)
    scope = str(memory.get("scope") or "")
    source_types = _string_list(memory.get("source_types", []))
    _validate_source_types(source_types)

    scope_scores = (
        PROMPT_OPTIMIZATION_SCOPE_SCORE
        if query_context["retrieval_purpose"] == "prompt_optimization"
        else LOCAL_SCOPE_SCORE
    )
    components = {
        "section_priority": SECTION_PRIORITY[section],
        "scope_score": scope_scores.get(scope, 0),
        "source_type_score": max([SOURCE_TYPE_SCORE[source] for source in source_types] or [0]),
        "prompt_module_match": (
            20
            if query_context.get("prompt_module")
            and query_context["prompt_module"] == memory.get("prompt_module")
            else 0
        ),
        "root_cause_match": (
            15
            if query_context.get("root_causes")
            and memory.get("root_cause") in set(query_context["root_causes"])
            else 0
        ),
        "failure_type_match": (
            10
            if query_context.get("failure_types")
            and memory.get("failure_type") in set(query_context["failure_types"])
            else 0
        ),
        "confidence_score": int(_float_or_default(memory.get("confidence"), 0.0) * 20),
        "support_score": min(_int_or_zero(memory.get("support_count")), 5) * 2,
        "contradiction_penalty": min(_int_or_zero(memory.get("contradiction_count")), 5) * 4,
        "weak_or_infra_penalty": WEAK_OR_INFRA_PENALTY if _is_caution(memory) else 0,
        "task_memory_penalty": TASK_MEMORY_PENALTY if scope == "task" else 0,
    }
    score = (
        components["section_priority"]
        + components["scope_score"]
        + components["source_type_score"]
        + components["prompt_module_match"]
        + components["root_cause_match"]
        + components["failure_type_match"]
        + components["confidence_score"]
        + components["support_score"]
        - components["contradiction_penalty"]
        - components["weak_or_infra_penalty"]
        - components["task_memory_penalty"]
    )
    return {
        "score": score,
        "section": section,
        "components": components,
    }


def assign_memory_section(
    store_item: dict[str, Any],
    query_context: dict[str, Any],
) -> str:
    """Return the M6.6 prompt section for an active-memory store wrapper."""

    validate_memory_query_context(query_context)
    validate_active_memory_store_item(store_item)
    memory = store_item["memory"]
    if _is_caution(memory):
        return "cautions"
    if _is_critical_negative(memory):
        return "critical_negative"
    if (
        str(memory.get("status") or "") == "contradicted"
        or _int_or_zero(memory.get("contradiction_count")) > 0
    ):
        return "contradictions_and_scope_limits"
    scope = str(memory.get("scope") or "")
    if scope == "global":
        return "global_invariants"
    if scope == "benchmark":
        return "benchmark_lessons"
    if scope == "repo":
        return "repo_lessons"
    return "task_lessons"


def _normalize_budget(
    query_context: dict[str, Any],
    budget: dict[str, Any] | None,
) -> dict[str, Any]:
    total_prompt_budget = query_context.get("total_prompt_budget_tokens")
    max_tokens = DEFAULT_MAX_ESTIMATED_TOKENS
    if isinstance(total_prompt_budget, int) and total_prompt_budget > 0:
        max_tokens = min(
            DEFAULT_MAX_ESTIMATED_TOKENS,
            max(MIN_PROMPT_BUDGET_TOKENS, int(total_prompt_budget * PROMPT_BUDGET_FRACTION)),
        )
    normalized = {
        "max_items": DEFAULT_MAX_ITEMS,
        "max_estimated_tokens": max_tokens,
        "used_estimated_tokens": 0,
        "section_caps": dict(DEFAULT_SECTION_CAPS),
        "negative_caution_combined_cap": NEGATIVE_CAUTION_COMBINED_CAP,
    }
    if budget:
        if "max_items" in budget:
            normalized["max_items"] = int(budget["max_items"])
        if "max_estimated_tokens" in budget:
            normalized["max_estimated_tokens"] = int(budget["max_estimated_tokens"])
        if isinstance(budget.get("section_caps"), dict):
            normalized["section_caps"] = {
                **normalized["section_caps"],
                **{
                    str(key): int(value)
                    for key, value in budget["section_caps"].items()
                    if str(key) in LESSON_SECTIONS
                },
            }
        if "negative_caution_combined_cap" in budget:
            normalized["negative_caution_combined_cap"] = int(
                budget["negative_caution_combined_cap"]
            )
    _validate_budget(normalized, require_used=True)
    return normalized


def _validate_budget(budget: Any, *, require_used: bool = False) -> None:
    if not isinstance(budget, dict):
        raise MemoryRetrievalValidationError("budget must be a mapping")
    for key in ("max_items", "max_estimated_tokens", "section_caps"):
        if key not in budget:
            raise MemoryRetrievalValidationError(f"missing budget field: {key}")
    if require_used and "used_estimated_tokens" not in budget:
        raise MemoryRetrievalValidationError("missing budget field: used_estimated_tokens")
    for key in ("max_items", "max_estimated_tokens"):
        if not isinstance(budget[key], int) or budget[key] < 0:
            raise MemoryRetrievalValidationError(f"{key} must be a non-negative integer")
    if require_used and (
        not isinstance(budget["used_estimated_tokens"], int)
        or budget["used_estimated_tokens"] < 0
    ):
        raise MemoryRetrievalValidationError(
            "used_estimated_tokens must be a non-negative integer"
        )
    if not isinstance(budget["section_caps"], dict):
        raise MemoryRetrievalValidationError("section_caps must be a mapping")
    for section, cap in budget["section_caps"].items():
        if section not in LESSON_SECTIONS:
            raise MemoryRetrievalValidationError(f"unknown section cap: {section}")
        if not isinstance(cap, int) or cap < 0:
            raise MemoryRetrievalValidationError("section caps must be non-negative integers")
    if "negative_caution_combined_cap" in budget and (
        not isinstance(budget["negative_caution_combined_cap"], int)
        or budget["negative_caution_combined_cap"] < 0
    ):
        raise MemoryRetrievalValidationError(
            "negative_caution_combined_cap must be a non-negative integer"
        )


def _validate_block_item(item: Any) -> None:
    if not isinstance(item, dict):
        raise MemoryRetrievalValidationError("memory block item must be a mapping")
    for key in (
        "schema_version",
        "memory_id",
        "section",
        "scope",
        "scope_caveat",
        "root_cause",
        "prompt_module",
        "lesson",
        "expected_effect",
        "status",
        "actionability",
        "confidence",
        "support_count",
        "source_types",
    ):
        if key not in item:
            raise MemoryRetrievalValidationError(f"missing block item field: {key}")
    if item["schema_version"] != MEMORY_BLOCK_ITEM_SCHEMA_VERSION:
        raise MemoryRetrievalValidationError("unsupported memory block item schema_version")
    if item["section"] not in LESSON_SECTIONS:
        raise MemoryRetrievalValidationError("invalid memory block section")
    for key in (
        "memory_id",
        "section",
        "scope",
        "scope_caveat",
        "root_cause",
        "prompt_module",
        "lesson",
        "expected_effect",
        "status",
        "actionability",
    ):
        if not isinstance(item[key], str):
            raise MemoryRetrievalValidationError(f"{key} must be a string")
    if not isinstance(item["confidence"], (int, float)):
        raise MemoryRetrievalValidationError("confidence must be numeric")
    if not isinstance(item["support_count"], int) or item["support_count"] < 0:
        raise MemoryRetrievalValidationError("support_count must be a non-negative integer")
    _validate_string_list(item["source_types"], "source_types")
    invalid_sources = [
        source for source in item["source_types"] if source not in EVIDENCE_SOURCE_STRENGTHS
    ]
    if invalid_sources:
        raise MemoryRetrievalValidationError("invalid block item source_type")
    extra_fields = sorted(set(item) - PROMPT_VISIBLE_FIELDS - {"schema_version", "section"})
    if extra_fields:
        raise MemoryRetrievalValidationError(f"forbidden block item fields: {extra_fields}")
    _assert_prompt_safe(item)


def _eligibility_reasons(
    store_item: dict[str, Any],
    query_context: dict[str, Any],
) -> tuple[bool, list[str]]:
    memory = store_item["memory"]
    audit = memory.get("audit_view", {})
    scope = str(memory.get("scope") or "")
    reasons: list[str] = []

    try:
        _validate_source_types(memory.get("source_types", []))
    except MemoryRetrievalValidationError:
        return False, ["invalid_source_type"]

    prompt_module = str(memory.get("prompt_module") or "")
    if query_context.get("prompt_module") and query_context["prompt_module"] != prompt_module:
        reasons.append("prompt_module_mismatch")

    if scope == "task":
        task_ids = set(_string_list(query_context.get("task_ids", [])))
        memory_tasks = set(_string_list(audit.get("task_ids", [])))
        memory_scope_id = str(memory.get("scope_id") or "")
        if not task_ids or (not task_ids.intersection(memory_tasks) and memory_scope_id not in task_ids):
            reasons.append("task_scope_mismatch")
    elif scope == "repo":
        repo_id = str(query_context.get("repo_id") or "")
        memory_repos = set(_string_list(audit.get("repo_ids", [])))
        memory_scope_id = str(memory.get("scope_id") or "")
        if not repo_id or (repo_id not in memory_repos and repo_id != memory_scope_id):
            reasons.append("repo_scope_mismatch")
    elif scope == "benchmark":
        benchmark_id = str(query_context.get("benchmark_id") or "")
        memory_benchmark = str(memory.get("benchmark_id") or "")
        memory_scope_id = str(memory.get("scope_id") or "")
        if not benchmark_id or benchmark_id not in {memory_benchmark, memory_scope_id}:
            reasons.append("benchmark_scope_mismatch")
    elif scope != "global":
        reasons.append("unknown_scope")

    if query_context.get("root_causes") and memory.get("root_cause") not in set(
        query_context["root_causes"]
    ):
        if not (_is_caution(memory) or _is_critical_negative(memory)):
            reasons.append("root_cause_mismatch")
    if query_context.get("failure_types") and memory.get("failure_type") not in set(
        query_context["failure_types"]
    ):
        if not (_is_caution(memory) or _is_critical_negative(memory)):
            reasons.append("failure_type_mismatch")

    if (
        query_context["retrieval_purpose"] == "prompt_optimization"
        and _is_infrastructure(memory)
        and assign_memory_section(store_item, query_context)
        not in {"cautions", "critical_negative"}
    ):
        reasons.append("infrastructure_not_prompt_positive")

    return not reasons, reasons


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    item = candidate["item"]
    ranking = candidate["ranking"]
    return (
        -int(ranking["score"]),
        -float(item["confidence"]),
        -int(item["support_count"]),
        _int_or_zero(candidate["wrapper"]["memory"].get("contradiction_count")),
        -_timestamp_value(candidate["last_updated_at"]),
        str(item["memory_id"]),
    )


def _audit_record(
    memory_id: str,
    decision: str,
    reasons: list[str],
    *,
    score: int | None = None,
    section: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "memory_id": str(memory_id or ""),
        "decision": str(decision or ""),
        "reasons": [_safe_reason(reason) for reason in reasons],
    }
    if score is not None:
        record["score"] = int(score)
    if section is not None:
        record["section"] = str(section)
    _assert_prompt_safe(record)
    return record


def _wrapper_memory_id(wrapper: Any) -> str:
    if isinstance(wrapper, dict) and isinstance(wrapper.get("memory"), dict):
        return str(wrapper["memory"].get("memory_id") or "")
    return ""


def _scope_caveat(scope: str) -> str:
    if scope == "global":
        return "Applies broadly, but defer to current verified evidence."
    if scope == "benchmark":
        return "Use only for the matching benchmark; do not treat as global."
    if scope == "repo":
        return "Use only for the matching repo; repo id is intentionally omitted."
    return "Use only for the matching task; task id is intentionally omitted."


def _actionability(memory: dict[str, Any]) -> str:
    if _is_critical_negative(memory):
        return "negative"
    if _is_caution(memory):
        return "low"
    if str(memory.get("status") or "") in {"active", "pinned"}:
        return "high"
    return "medium"


def _is_caution(memory: dict[str, Any]) -> bool:
    return _is_infrastructure(memory)


def _is_infrastructure(memory: dict[str, Any]) -> bool:
    values = {
        str(memory.get("root_cause") or ""),
        str(memory.get("failure_type") or ""),
    }
    return bool(values.intersection(INFRASTRUCTURE_FAILURES))


def _is_critical_negative(memory: dict[str, Any]) -> bool:
    source_types = set(_string_list(memory.get("source_types", [])))
    values = {
        str(memory.get("root_cause") or ""),
        str(memory.get("failure_type") or ""),
        str(memory.get("status") or ""),
    }
    return bool(
        source_types.intersection(NEGATIVE_SOURCE_TYPES)
        or values.intersection(CRITICAL_NEGATIVE_CAUSES)
        or str(memory.get("status") or "") == "suppressed"
    )


def _validate_source_types(source_types: Any) -> None:
    values = _string_list(source_types)
    if not values:
        raise MemoryRetrievalValidationError("missing source_types")
    invalid = [source for source in values if source not in EVIDENCE_SOURCE_STRENGTHS]
    if invalid:
        raise MemoryRetrievalValidationError("invalid_source_type")


def _safe_reason(reason: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_:-]+", "_", str(reason or "").strip())
    text = text.strip("_").lower()
    return text[:80] or "unknown"


def _timestamp_value(value: str) -> float:
    text = str(value or "")
    if not text:
        return 0.0
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


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


def _float_or_default(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _assert_prompt_safe(
    value: Any,
    path: str = "",
    *,
    allow_policy_terms: bool = False,
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if _is_forbidden_key(key_text):
                raise MemoryRetrievalValidationError(
                    f"forbidden prompt field: {path}.{key_text}".strip(".")
                )
            _assert_prompt_safe(
                item,
                f"{path}.{key_text}".strip("."),
                allow_policy_terms=allow_policy_terms,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_prompt_safe(
                item,
                f"{path}[{index}]",
                allow_policy_terms=allow_policy_terms,
            )
    elif isinstance(value, str):
        lowered = value.lower()
        if not allow_policy_terms and any(
            marker in lowered for marker in FORBIDDEN_PROMPT_CONTENT_MARKERS
        ):
            raise MemoryRetrievalValidationError("forbidden prompt content")
        if any(pattern.search(value) for pattern in SECRET_PATTERNS):
            raise MemoryRetrievalValidationError("secret-like prompt content")


def _is_forbidden_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
    return any(marker in normalized for marker in FORBIDDEN_PROMPT_FIELD_MARKERS)


def _renderable_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": item["memory_id"],
        "scope": item["scope"],
        "root_cause": item["root_cause"],
        "prompt_module": item["prompt_module"],
        "lesson": item["lesson"],
        "expected_effect": item["expected_effect"],
        "status": item["status"],
        "confidence": item["confidence"],
        "support_count": item["support_count"],
        "source_types": item["source_types"],
        "scope_caveat": item["scope_caveat"],
    }


def _validate_string_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list):
        raise MemoryRetrievalValidationError(f"{field_name} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise MemoryRetrievalValidationError(f"{field_name} must contain strings")


def _string_list(value: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    source = value if isinstance(value, (list, tuple)) else [value]
    for item in source:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


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
