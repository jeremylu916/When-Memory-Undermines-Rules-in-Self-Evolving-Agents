"""LLM planner for Trace2Prompt edit operations.

This module lives between llm_optimizer and candidate_builder. It may ask an LLM
which edit operation to attach to already-proposed structured updates, but it
never materializes prompts. candidate_builder remains the deterministic verifier
and renderer.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from self_evolving_agent.candidate_builder import (
    PROMPT_EDIT_OPERATIONS,
    inspect_trace2prompt_blocks,
)
from self_evolving_agent.config import PROMPT_MODULES


OPERATION_PLANNER_SCHEMA_VERSION = "m5.4.operation_planner.v1"
MAX_OPERATION_REASON_CHARS = 240
MAX_REPLACEMENT_DELTA_CHARS = 600
MAX_PLANNER_INPUT_CHARS = 12000

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


def plan_prompt_edit_operations(
    base_config: dict[str, Any],
    structured_updates: list[dict[str, Any]],
    *,
    llm_client: Any | None,
    diagnosis: dict[str, Any] | None = None,
    max_completion_tokens: int = 700,
    timeout_s: int | None = None,
    return_metadata: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Attach prompt-edit operation fields before deterministic materialization."""

    planner = LLMOperationPlanner(
        llm_client,
        max_completion_tokens=max_completion_tokens,
        timeout_s=timeout_s,
    )
    return planner.plan_updates(
        base_config,
        structured_updates,
        diagnosis=diagnosis,
        return_metadata=return_metadata,
    )


class LLMOperationPlanner:
    """Schema-bound planner for add/preserve/suppress/replace choices."""

    def __init__(
        self,
        llm_client: Any | None,
        *,
        max_completion_tokens: int = 700,
        timeout_s: int | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.max_completion_tokens = max(128, int(max_completion_tokens))
        self.timeout_s = timeout_s

    def plan_updates(
        self,
        base_config: dict[str, Any],
        structured_updates: list[dict[str, Any]],
        *,
        diagnosis: dict[str, Any] | None = None,
        return_metadata: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        updates = [
            copy.deepcopy(update)
            for update in (structured_updates or [])
            if isinstance(update, dict)
        ]
        existing_blocks = inspect_trace2prompt_blocks(base_config)
        if self.llm_client is None:
            planned = _default_add_append_updates(updates)
            result = _planner_result(
                planned,
                existing_blocks=existing_blocks,
                fallback_reason="missing_llm_client",
            )
            return result if return_metadata else result["updates"]

        try:
            response = self.llm_client.chat(
                self.build_messages(
                    base_config,
                    updates,
                    diagnosis=diagnosis,
                    existing_blocks=existing_blocks,
                ),
                max_completion_tokens=self.max_completion_tokens,
                timeout_s=self.timeout_s,
            )
            payload = _parse_json_object(getattr(response, "content", ""))
            planned, rejected_plan_items = _apply_operation_plan_payload(
                payload,
                updates,
                existing_blocks,
            )
            result = _planner_result(
                planned,
                existing_blocks=existing_blocks,
                llm_used=True,
                rejected_plan_items=rejected_plan_items,
            )
        except Exception as exc:
            result = _planner_result(
                _default_add_append_updates(updates),
                existing_blocks=existing_blocks,
                fallback_reason=_clean_text(exc, 240),
            )
        return result if return_metadata else result["updates"]

    def build_messages(
        self,
        base_config: dict[str, Any],
        structured_updates: list[dict[str, Any]],
        *,
        diagnosis: dict[str, Any] | None = None,
        existing_blocks: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        blocks = existing_blocks
        if blocks is None:
            blocks = inspect_trace2prompt_blocks(base_config)
        proposed_updates = [
            _planner_update_view(index, update)
            for index, update in enumerate(structured_updates or [])
            if isinstance(update, dict)
        ]
        payload = {
            "schema_version": OPERATION_PLANNER_SCHEMA_VERSION,
            "allowed_operations": list(PROMPT_EDIT_OPERATIONS),
            "operation_plan_schema": {
                "update_index": "integer index into proposed_updates",
                "operation": list(PROMPT_EDIT_OPERATIONS),
                "target_update_id": "required for preserve/suppress/replace",
                "replacement_delta": (
                    "required for replace; usually the proposed instruction_delta"
                ),
                "operation_reason": "brief rationale",
            },
            "rules": [
                "Do not invent new prompt deltas; choose operations for proposed updates.",
                "Use add_append when the update introduces a new active lesson.",
                "Use preserve_existing_update for duplicates or explicit no-op decisions.",
                "Use suppress_existing_update only for an existing Trace2Prompt block.",
                "Use replace_existing_update when modifying an existing active block.",
                "If replacing, set replacement_delta to the intended new active body.",
                "decision_type is audit metadata and must not determine operation.",
                "candidate_builder will deterministically reject unsafe or invalid edits.",
            ],
            "existing_update_blocks": _compact_existing_blocks(blocks),
            "active_update_counts_by_module": _active_update_counts(blocks),
            "proposed_updates": proposed_updates,
            "diagnosis_context": _compact_diagnosis(diagnosis or {}),
        }
        text = json.dumps(_redact_jsonable(payload), sort_keys=True)
        if len(text) > MAX_PLANNER_INPUT_CHARS:
            payload["diagnosis_context"] = {}
            payload["existing_update_blocks"] = payload["existing_update_blocks"][:12]
            text = json.dumps(_redact_jsonable(payload), sort_keys=True)
        system = (
            "You are a schema-constrained operation planner for Trace2Prompt "
            "updates. You run after the LLM optimizer proposes structured "
            "updates and before deterministic candidate_builder validation. "
            "Return JSON only in this exact shape: "
            "{\"operation_plan\": [{\"update_index\": 0, \"operation\": "
            "\"add_append\", \"target_update_id\": \"\", "
            "\"replacement_delta\": \"\", \"operation_reason\": \"...\"}]}. "
            "Do not rewrite prompts. Do not add memory citations. Do not use "
            "decision_type as the operation."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]


def _apply_operation_plan_payload(
    payload: dict[str, Any],
    updates: list[dict[str, Any]],
    existing_blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_plan = payload.get("operation_plan")
    if not isinstance(raw_plan, list):
        raise ValueError("operation planner response missing operation_plan list")

    by_index: dict[int, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    for plan_item in raw_plan:
        if not isinstance(plan_item, dict):
            rejected.append({"reason": "invalid_plan_item_type", "item": str(plan_item)})
            continue
        index = _int_or_none(plan_item.get("update_index"))
        if index is None or index < 0 or index >= len(updates):
            rejected.append({"reason": "invalid_update_index", "item": _redact_jsonable(plan_item)})
            continue
        if index in by_index:
            rejected.append({"reason": "duplicate_update_index", "item": _redact_jsonable(plan_item)})
            continue
        by_index[index] = plan_item

    existing_by_module = _existing_ids_by_module(existing_blocks)
    planned: list[dict[str, Any]] = []
    for index, update in enumerate(updates):
        plan_item = by_index.get(index, {"operation": "add_append"})
        planned.append(
            _merge_plan_item(
                update,
                plan_item,
                index=index,
                existing_by_module=existing_by_module,
                rejected=rejected,
            )
        )
    return planned, rejected


def _merge_plan_item(
    update: dict[str, Any],
    plan_item: dict[str, Any],
    *,
    index: int,
    existing_by_module: dict[str, set[str]],
    rejected: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = copy.deepcopy(update)
    module = str(merged.get("target_module", "")).strip()
    operation = _clean_text(plan_item.get("operation") or "add_append", 80)
    if operation not in PROMPT_EDIT_OPERATIONS:
        rejected.append(
            {
                "update_index": index,
                "reason": "invalid_operation",
                "operation": operation,
            }
        )
        merged["operation"] = "add_append"
        return merged

    target_update_id = _clean_text(plan_item.get("target_update_id") or "", 160)
    reason = _clean_text(
        plan_item.get("operation_reason") or "",
        MAX_OPERATION_REASON_CHARS,
    )
    replacement_delta = _clean_text(
        plan_item.get("replacement_delta") or "",
        MAX_REPLACEMENT_DELTA_CHARS,
    )

    if operation in {
        "preserve_existing_update",
        "suppress_existing_update",
        "replace_existing_update",
    }:
        if not target_update_id:
            rejected.append(
                {
                    "update_index": index,
                    "reason": "missing_target_update_id",
                    "operation": operation,
                }
            )
            merged["operation"] = "add_append"
            return merged
        if target_update_id not in existing_by_module.get(module, set()):
            rejected.append(
                {
                    "update_index": index,
                    "reason": "unknown_target_update_id",
                    "operation": operation,
                    "target_update_id": target_update_id,
                }
            )
            merged["operation"] = "add_append"
            return merged
        merged["target_update_id"] = target_update_id

    if operation == "replace_existing_update":
        if not replacement_delta:
            replacement_delta = _clean_text(
                merged.get("instruction_delta") or "",
                MAX_REPLACEMENT_DELTA_CHARS,
            )
        if not replacement_delta:
            rejected.append(
                {
                    "update_index": index,
                    "reason": "missing_replacement_delta",
                    "operation": operation,
                    "target_update_id": target_update_id,
                }
            )
            merged["operation"] = "add_append"
            merged.pop("target_update_id", None)
            return merged
        merged["replacement_delta"] = replacement_delta

    merged["operation"] = operation
    if reason:
        merged["operation_reason"] = reason
    return merged


def _default_add_append_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    planned = []
    for update in updates:
        item = copy.deepcopy(update)
        item.setdefault("operation", "add_append")
        planned.append(item)
    return planned


def _planner_result(
    updates: list[dict[str, Any]],
    *,
    existing_blocks: list[dict[str, Any]],
    llm_used: bool = False,
    fallback_reason: str = "",
    rejected_plan_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    defaulted = [
        index
        for index, update in enumerate(updates)
        if isinstance(update, dict) and update.get("operation") == "add_append"
    ]
    return {
        "updates": updates,
        "operation_planner": {
            "schema_version": OPERATION_PLANNER_SCHEMA_VERSION,
            "llm_used": bool(llm_used and not fallback_reason),
            "fallback_reason": fallback_reason,
            "planned_update_count": len(updates),
            "defaulted_update_indices": defaulted,
            "rejected_plan_items": rejected_plan_items or [],
            "existing_update_blocks": existing_blocks,
        },
    }


def _planner_update_view(index: int, update: dict[str, Any]) -> dict[str, Any]:
    return {
        "update_index": index,
        "target_module": _clean_text(update.get("target_module") or "", 80),
        "failure_pattern": _clean_text(update.get("failure_pattern") or "", 120),
        "instruction_delta": _clean_text(
            update.get("instruction_delta") or "",
            MAX_REPLACEMENT_DELTA_CHARS,
        ),
        "expected_effect": _clean_text(update.get("expected_effect") or "", 240),
        "decision_type": _clean_text(update.get("decision_type") or "", 80),
        "evidence_role": _clean_text(update.get("evidence_role") or "", 80),
    }


def _compact_existing_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        compact.append(
            {
                "target_module": _clean_text(block.get("target_module") or "", 80),
                "update_id": _clean_text(block.get("update_id") or "", 160),
                "operation": _clean_text(block.get("operation") or "", 80),
                "status": _clean_text(block.get("status") or "", 80),
                "body_preview": _clean_text(block.get("body_preview") or "", 240),
                "replaces": _clean_text(block.get("replaces") or "", 160),
                "legacy": bool(block.get("legacy", False)),
            }
        )
    return compact


def _active_update_counts(blocks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("status") or "active") != "active":
            continue
        module = str(block.get("target_module") or "")
        if module in PROMPT_MODULES:
            counts[module] = counts.get(module, 0) + 1
    return dict(sorted(counts.items()))


def _existing_ids_by_module(blocks: list[dict[str, Any]]) -> dict[str, set[str]]:
    by_module: dict[str, set[str]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("status") or "active") != "active":
            continue
        module = str(block.get("target_module") or "")
        update_id = str(block.get("update_id") or "")
        if module and update_id:
            by_module.setdefault(module, set()).add(update_id)
    return by_module


def _compact_diagnosis(diagnosis: dict[str, Any]) -> dict[str, Any]:
    optimizer_input = diagnosis.get("optimizer_input")
    if not isinstance(optimizer_input, dict):
        optimizer_input = {}
    return {
        "failure_taxonomy": _redact_jsonable(diagnosis.get("failure_taxonomy", {})),
        "dominant_patterns": _redact_jsonable(
            optimizer_input.get("dominant_patterns", [])[:5]
        ),
        "recommended_update_targets": [
            str(module)
            for module in optimizer_input.get("recommended_update_targets", [])
            if module in PROMPT_MODULES
        ],
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    fence = chr(96) * 3
    if cleaned.startswith(fence):
        cleaned = re.sub(r"^" + fence + r"(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*" + fence + r"$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("operation planner response did not contain JSON")
    payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("operation planner response must be a JSON object")
    return payload


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    return text[:limit]


def _redact_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value, 2000)
    if isinstance(value, dict):
        return {
            str(key): _redact_jsonable(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_redact_jsonable(item) for item in value[:20]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean_text(value, 2000)
