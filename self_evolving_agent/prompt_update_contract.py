"""Typed v2 proposal contract for Trace2Prompt prompt updates.

The v2 contract is an optimizer-facing envelope. It is intentionally compiled
back down to the existing candidate_builder operation fields so prompt
materialization remains deterministic and backward compatible.
"""

from __future__ import annotations

from typing import Any

from self_evolving_agent.candidate_builder import PROMPT_EDIT_OPERATIONS
from self_evolving_agent.config import PROMPT_MODULES


PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION = "prompt_update_proposal.v2"
PROMPT_UPDATE_PROPOSAL_OPERATIONS = tuple(PROMPT_EDIT_OPERATIONS)
PROMPT_UPDATE_PROPOSAL_TARGET_MODULES = tuple(PROMPT_MODULES)
PROMPT_UPDATE_PROPOSAL_REQUIRED_KEYS = (
    "schema_version",
    "operation",
    "target_module",
    "failure_pattern",
    "applicability_condition",
    "expected_behavior_change",
    "acceptance_test",
    "evidence_refs",
)
PROMPT_UPDATE_PROPOSAL_ALLOWED_KEYS = frozenset(
    PROMPT_UPDATE_PROPOSAL_REQUIRED_KEYS
    + (
        "target_update_id",
        "instruction_delta",
        "replacement_delta",
        "operation_reason",
        "_legacy_missing_acceptance_test",
    )
)
MAX_PROMPT_UPDATE_TEXT_CHARS = 600
MAX_PROMPT_UPDATE_REASON_CHARS = 300


def prompt_update_proposal_json_schema(*, max_updates: int = 3) -> dict[str, Any]:
    """Return the single machine-readable schema shared by prompt and validator."""

    string = {"type": "string"}
    string_list = {"type": "array", "items": string, "maxItems": 12}
    proposal = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {
                "type": "string",
                "const": PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION,
            },
            "operation": {
                "type": "string",
                "enum": list(PROMPT_UPDATE_PROPOSAL_OPERATIONS),
            },
            "target_module": {
                "type": "string",
                "enum": list(PROMPT_UPDATE_PROPOSAL_TARGET_MODULES),
            },
            "target_update_id": string,
            "failure_pattern": string,
            "applicability_condition": string,
            "instruction_delta": string,
            "replacement_delta": string,
            "expected_behavior_change": string,
            "acceptance_test": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "const": "prompt_behavior_fixture",
                    },
                    "should_contain": string_list,
                    "should_not_contain": string_list,
                },
                "required": ["type", "should_contain", "should_not_contain"],
            },
            "evidence_refs": string_list,
        },
        "required": [
            "schema_version",
            "operation",
            "target_module",
            "target_update_id",
            "failure_pattern",
            "applicability_condition",
            "instruction_delta",
            "replacement_delta",
            "expected_behavior_change",
            "acceptance_test",
            "evidence_refs",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "proposals": {
                "type": "array",
                "items": proposal,
                "maxItems": max(0, int(max_updates)),
            }
        },
        "required": ["proposals"],
    }


class PromptUpdateContractError(ValueError):
    """Raised when a v2 prompt-update proposal violates the public contract."""


def normalize_prompt_update_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """Return a validated, bounded v2 proposal.

    Operation-specific text rules are kept here, while quality judgments such
    as "generic wording" live in prompt_delta_linter.
    """

    if not isinstance(proposal, dict):
        raise PromptUpdateContractError("proposal must be a JSON object")

    unknown = sorted(set(proposal) - PROMPT_UPDATE_PROPOSAL_ALLOWED_KEYS)
    if unknown:
        raise PromptUpdateContractError(f"proposal has unknown keys: {unknown}")

    missing = [
        key
        for key in PROMPT_UPDATE_PROPOSAL_REQUIRED_KEYS
        if key not in proposal
        or (
            key not in {"applicability_condition", "evidence_refs"}
            and _missing_required_value(proposal.get(key))
        )
    ]
    if missing:
        raise PromptUpdateContractError(f"proposal missing keys: {missing}")

    schema_version = _clean_text(proposal.get("schema_version"), 80)
    if schema_version != PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION:
        raise PromptUpdateContractError(
            f"unsupported schema_version: {schema_version}"
        )

    operation = _clean_text(proposal.get("operation"), 80)
    if operation not in PROMPT_UPDATE_PROPOSAL_OPERATIONS:
        raise PromptUpdateContractError(f"unsupported operation: {operation}")

    target_module = _clean_text(proposal.get("target_module"), 80)
    if target_module not in PROMPT_UPDATE_PROPOSAL_TARGET_MODULES:
        raise PromptUpdateContractError(f"unsupported target_module: {target_module}")

    target_update_id = _clean_text(proposal.get("target_update_id") or "", 160)
    if operation in {
        "replace_existing_update",
        "suppress_existing_update",
        "preserve_existing_update",
    } and not target_update_id:
        raise PromptUpdateContractError(
            f"{operation} requires target_update_id"
        )

    instruction_delta = _clean_text(
        proposal.get("instruction_delta") or "",
        MAX_PROMPT_UPDATE_TEXT_CHARS,
    )
    replacement_delta = _clean_text(
        proposal.get("replacement_delta") or "",
        MAX_PROMPT_UPDATE_TEXT_CHARS,
    )

    if operation == "add_append" and not instruction_delta:
        raise PromptUpdateContractError("add_append requires instruction_delta")
    if operation == "replace_existing_update" and not replacement_delta:
        raise PromptUpdateContractError(
            "replace_existing_update requires replacement_delta"
        )

    acceptance_test = _normalize_acceptance_test(proposal.get("acceptance_test"))
    evidence_refs = _normalize_string_list(proposal.get("evidence_refs"), limit=12)

    normalized = {
        "schema_version": schema_version,
        "operation": operation,
        "target_module": target_module,
        "target_update_id": target_update_id,
        "failure_pattern": _clean_text(proposal.get("failure_pattern"), 160),
        "applicability_condition": _clean_text(
            proposal.get("applicability_condition"),
            MAX_PROMPT_UPDATE_REASON_CHARS,
        ),
        "instruction_delta": instruction_delta,
        "replacement_delta": replacement_delta,
        "expected_behavior_change": _clean_text(
            proposal.get("expected_behavior_change"),
            MAX_PROMPT_UPDATE_REASON_CHARS,
        ),
        "acceptance_test": acceptance_test,
        "evidence_refs": evidence_refs,
    }
    operation_reason = _clean_text(
        proposal.get("operation_reason") or "",
        MAX_PROMPT_UPDATE_REASON_CHARS,
    )
    if operation_reason:
        normalized["operation_reason"] = operation_reason
    if proposal.get("_legacy_missing_acceptance_test"):
        normalized["_legacy_missing_acceptance_test"] = True
    return normalized


def adapt_prompt_update_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """Compile one v2 proposal into candidate_builder's existing update shape."""

    normalized = normalize_prompt_update_proposal(proposal)
    operation = normalized["operation"]
    body = (
        normalized["replacement_delta"]
        if operation == "replace_existing_update"
        else normalized["instruction_delta"]
    )
    if not body:
        body = normalized["expected_behavior_change"]

    compiled_body = _compile_applicable_instruction(
        normalized["applicability_condition"],
        body,
    )
    update = {
        "target_module": normalized["target_module"],
        "failure_pattern": normalized["failure_pattern"],
        "instruction_delta": compiled_body,
        "expected_effect": normalized["expected_behavior_change"],
        "operation": operation,
        "operation_reason": normalized.get("operation_reason")
        or normalized["applicability_condition"],
        "applicability_condition": normalized["applicability_condition"],
        "acceptance_test": normalized["acceptance_test"],
        "evidence_refs": normalized["evidence_refs"],
        "proposal_schema_version": normalized["schema_version"],
    }
    if normalized["target_update_id"]:
        update["target_update_id"] = normalized["target_update_id"]
    if normalized["replacement_delta"]:
        update["replacement_delta"] = compiled_body
    return update


def _compile_applicable_instruction(condition: str, instruction: str) -> str:
    condition = str(condition or "").strip()
    instruction = str(instruction or "").strip()
    if not condition:
        return instruction
    return f"Applicability condition: {condition}\nInstruction: {instruction}"


def adapt_prompt_update_proposals(
    proposals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compile v2 proposals into existing Trace2Prompt operation updates."""

    return [adapt_prompt_update_proposal(item) for item in proposals]


def legacy_update_to_prompt_update_proposal(
    update: dict[str, Any],
    *,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap a legacy structured update in a v2-shaped proposal for auditing."""

    if not isinstance(update, dict):
        raise PromptUpdateContractError("legacy update must be a JSON object")
    operation = _clean_text(update.get("operation") or "add_append", 80)
    instruction_delta = _clean_text(
        update.get("instruction_delta") or "",
        MAX_PROMPT_UPDATE_TEXT_CHARS,
    )
    replacement_delta = _clean_text(
        update.get("replacement_delta") or "",
        MAX_PROMPT_UPDATE_TEXT_CHARS,
    )
    wrapped = {
        "schema_version": PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION,
        "operation": operation,
        "target_module": _clean_text(update.get("target_module") or "", 80),
        "target_update_id": _clean_text(update.get("target_update_id") or "", 160),
        "failure_pattern": _clean_text(update.get("failure_pattern") or "", 160),
        "applicability_condition": _clean_text(
            update.get("applicability_condition") or "",
            MAX_PROMPT_UPDATE_REASON_CHARS,
        ),
        "instruction_delta": instruction_delta,
        "replacement_delta": replacement_delta,
        "expected_behavior_change": _clean_text(
            update.get("expected_behavior_change")
            or update.get("expected_effect")
            or "",
            MAX_PROMPT_UPDATE_REASON_CHARS,
        ),
        "acceptance_test": update.get("acceptance_test")
        if isinstance(update.get("acceptance_test"), dict)
        else {
            "type": "prompt_behavior_fixture",
            "should_contain": [replacement_delta or instruction_delta]
            if replacement_delta or instruction_delta
            else [],
            "should_not_contain": [],
        },
        "evidence_refs": evidence_refs or _normalize_string_list(
            update.get("evidence_refs") or [], limit=12
        ),
    }
    if not isinstance(update.get("acceptance_test"), dict):
        wrapped["_legacy_missing_acceptance_test"] = True
    return wrapped


def _normalize_acceptance_test(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PromptUpdateContractError("acceptance_test must be an object")
    unknown = sorted(
        set(value) - {"type", "should_contain", "should_not_contain"}
    )
    if unknown:
        raise PromptUpdateContractError(
            f"acceptance_test has unknown keys: {unknown}"
        )
    test_type = _clean_text(value.get("type") or "", 80)
    if test_type != "prompt_behavior_fixture":
        raise PromptUpdateContractError(
            "acceptance_test.type must be prompt_behavior_fixture"
        )
    should_contain = _normalize_string_list(value.get("should_contain"), limit=12)
    should_not_contain = _normalize_string_list(
        value.get("should_not_contain"),
        limit=12,
    )
    if not should_contain and not should_not_contain:
        raise PromptUpdateContractError(
            "acceptance_test must include should_contain or should_not_contain"
        )
    return {
        "type": test_type,
        "should_contain": should_contain,
        "should_not_contain": should_not_contain,
    }


def _normalize_string_list(value: Any, *, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PromptUpdateContractError("expected a list of strings")
    if len(value) > limit:
        raise PromptUpdateContractError(f"list exceeds maximum length {limit}")
    if any(not isinstance(item, str) for item in value):
        raise PromptUpdateContractError("expected a list of strings")
    return [
        _clean_text(item, MAX_PROMPT_UPDATE_TEXT_CHARS)
        for item in value
        if item.strip()
    ]


def _missing_required_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _clean_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[:max_chars].rstrip()
    return text
