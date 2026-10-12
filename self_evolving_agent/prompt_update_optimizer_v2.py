"""LLM-facing optimizer for v2 prompt-update proposals.

This module is the replay/evaluation bridge for the v2 representation layer. It
does not materialize prompts. Accepted proposals are compiled into the existing
candidate_builder update shape by prompt_update_contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from self_evolving_agent.candidate_builder import (
    PROMPT_EDIT_OPERATIONS,
    inspect_trace2prompt_blocks,
)
from self_evolving_agent.config import PROMPT_MODULES
from self_evolving_agent.prompt_behavior_fixtures import (
    evaluate_prompt_behavior_fixtures,
)
from self_evolving_agent.prompt_delta_linter import (
    PROMPT_DELTA_LINTER_SCHEMA_VERSION,
    lint_legacy_prompt_updates,
    lint_prompt_update_proposals,
)
from self_evolving_agent.prompt_update_contract import (
    PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION,
    adapt_prompt_update_proposals,
    normalize_prompt_update_proposal,
    prompt_update_proposal_json_schema,
)


PROMPT_UPDATE_OPTIMIZER_V2_SCHEMA_VERSION = "prompt_update_optimizer.v2.replay.v1"
MAX_V2_OPTIMIZER_INPUT_CHARS = 12000
DEFAULT_V2_OPTIMIZER_MAX_COMPLETION_TOKENS = 4096
MAX_STRUCTURED_OUTPUT_ATTEMPTS = 3
STRUCTURED_OUTPUT_SKILL_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "optimizer_structured_output"
    / "SKILL.md"
)
STRUCTURED_OUTPUT_SKILL_SOURCE = {
    "repository": "https://github.com/sickn33/antigravity-awesome-skills",
    "path": "skills/llm-structured-output/SKILL.md",
    "commit": "7595cdf403884c25112f8b06450b5f846244a845",
    "license": "MIT",
}


class PromptUpdatePayloadError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        response_preview: str,
        response_metadata: dict[str, Any],
        response_attempts: list[dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.response_preview = response_preview
        self.response_metadata = response_metadata
        self.response_attempts = response_attempts


def build_v2_prompt_update_result(
    diagnosis: dict[str, Any],
    *,
    llm_client: Any | None,
    base_config: dict[str, Any] | None = None,
    existing_updates: list[dict[str, Any]] | None = None,
    legacy_updates: list[dict[str, Any]] | None = None,
    max_updates: int = 3,
    max_completion_tokens: int = DEFAULT_V2_OPTIMIZER_MAX_COMPLETION_TOKENS,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Build, lint, and adapt v2 prompt-update proposals."""

    optimizer = PromptUpdateOptimizerV2(
        llm_client,
        max_updates=max_updates,
        max_completion_tokens=max_completion_tokens,
        timeout_s=timeout_s,
    )
    return optimizer.build_result(
        diagnosis,
        base_config=base_config,
        existing_updates=existing_updates,
        legacy_updates=legacy_updates,
    )


def compare_legacy_and_v2_quality(
    legacy_updates: list[dict[str, Any]],
    v2_result: dict[str, Any],
    *,
    diagnosis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare old structured updates against v2+linter output."""

    legacy_lint = lint_legacy_prompt_updates(
        legacy_updates,
        diagnosis=diagnosis,
    )
    v2_metrics = (v2_result.get("linter") or {}).get("metrics", {})
    legacy_metrics = legacy_lint["metrics"]
    metric_delta = {
        key: round(float(v2_metrics.get(key, 0.0)) - float(value), 6)
        for key, value in legacy_metrics.items()
        if isinstance(value, (int, float))
    }
    return {
        "schema_version": "prompt_update_optimizer.v2.quality_comparison.v1",
        "legacy_update_count": len(legacy_updates),
        "v2_proposal_count": int(v2_result.get("proposal_count") or 0),
        "legacy_linter": legacy_lint,
        "v2_linter": v2_result.get("linter", {}),
        "metric_delta_v2_minus_legacy": metric_delta,
    }


class PromptUpdateOptimizerV2:
    """Schema-bound optimizer for typed v2 prompt-update proposals."""

    def __init__(
        self,
        llm_client: Any | None,
        *,
        max_updates: int = 3,
        max_completion_tokens: int = DEFAULT_V2_OPTIMIZER_MAX_COMPLETION_TOKENS,
        timeout_s: int | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.max_updates = max(1, min(int(max_updates), 3))
        self.max_completion_tokens = max(256, int(max_completion_tokens))
        self.timeout_s = timeout_s

    def build_result(
        self,
        diagnosis: dict[str, Any],
        *,
        base_config: dict[str, Any] | None = None,
        existing_updates: list[dict[str, Any]] | None = None,
        legacy_updates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        existing = list(existing_updates or [])
        if base_config is not None:
            existing.extend(inspect_trace2prompt_blocks(base_config))

        messages = self.build_messages(
            diagnosis,
            base_config=base_config,
            existing_updates=existing,
        )
        fallback_reason = ""
        llm_used = False
        llm_response_content_preview = ""
        llm_response_metadata: dict[str, Any] = {}
        llm_response_attempts: list[dict[str, Any]] = []
        raw_proposals: list[dict[str, Any]] = []
        rejected_payload_items: list[dict[str, Any]] = []

        if self.llm_client is None:
            fallback_reason = "missing_llm_client"
        else:
            try:
                (
                    raw_proposals,
                    llm_response_content_preview,
                    llm_response_metadata,
                    llm_response_attempts,
                ) = self._call_llm_for_payload(
                    messages,
                    max_updates=self.max_updates,
                )
                llm_used = True
            except PromptUpdatePayloadError as exc:
                llm_response_content_preview = exc.response_preview
                llm_response_metadata = exc.response_metadata
                llm_response_attempts = exc.response_attempts
                fallback_reason = _clean_text(exc, 240)
            except Exception as exc:
                fallback_reason = _clean_text(exc, 240)

        lint = lint_prompt_update_proposals(
            raw_proposals,
            existing_updates=existing,
            diagnosis=diagnosis,
            solver_capabilities=_solver_capabilities(base_config),
        )
        lint_accepted_proposals = [
            result["proposal"]
            for result in lint["results"]
            if result["accepted"] and isinstance(result.get("proposal"), dict)
        ]
        accepted_proposals = list(lint_accepted_proposals)
        compiled_updates = adapt_prompt_update_proposals(lint_accepted_proposals)
        fixture_report = None
        fixture_rejected_proposal_count = 0
        if base_config is not None:
            fixture_report = evaluate_prompt_behavior_fixtures(
                base_config,
                lint_accepted_proposals,
                compiled_updates,
                diagnosis=diagnosis,
            )
            passed_indexes = {
                int(item.get("proposal_index"))
                for item in fixture_report.get("results", [])
                if isinstance(item, dict) and bool(item.get("passed"))
            }
            accepted_proposals = [
                proposal
                for index, proposal in enumerate(lint_accepted_proposals)
                if index in passed_indexes
            ]
            fixture_rejected_proposal_count = (
                len(lint_accepted_proposals) - len(accepted_proposals)
            )
            compiled_updates = adapt_prompt_update_proposals(accepted_proposals)
        fixture_gate_passed = bool(
            fixture_report is None or fixture_report.get("all_passed", False)
        )
        schema_valid = bool(llm_used and not fallback_reason)
        applicability_preservation_rate = _applicability_preservation_rate(
            accepted_proposals,
            compiled_updates,
        )
        result = {
            "schema_version": PROMPT_UPDATE_OPTIMIZER_V2_SCHEMA_VERSION,
            "proposal_schema_version": PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION,
            "linter_schema_version": PROMPT_DELTA_LINTER_SCHEMA_VERSION,
            "llm_used": bool(llm_used and not fallback_reason),
            "fallback_reason": fallback_reason,
            "llm_response_content_preview": llm_response_content_preview,
            "llm_response_metadata": llm_response_metadata,
            "llm_response_attempts": llm_response_attempts,
            "max_updates": self.max_updates,
            "proposal_count": len(raw_proposals),
            "accepted_proposal_count": len(accepted_proposals),
            "compiled_update_count": len(compiled_updates),
            "fixture_rejected_proposal_count": fixture_rejected_proposal_count,
            "fixture_gate_passed": fixture_gate_passed,
            "rejected_payload_items": rejected_payload_items,
            "proposals": raw_proposals,
            "accepted_proposals": accepted_proposals,
            "compiled_updates": compiled_updates,
            "linter": lint,
            "prompt_behavior_fixtures": fixture_report,
            "optimizer_messages": messages,
            "structured_output": {
                "schema_valid": schema_valid,
                "attempt_count": len(llm_response_attempts),
                "retry_count": max(0, len(llm_response_attempts) - 1),
                "valid_attempt_rate": (
                    1.0 / len(llm_response_attempts)
                    if schema_valid and llm_response_attempts
                    else 0.0
                ),
                "applicability_preservation_rate": applicability_preservation_rate,
                "skill_path": str(STRUCTURED_OUTPUT_SKILL_PATH),
                "source": STRUCTURED_OUTPUT_SKILL_SOURCE,
            },
        }
        if legacy_updates is not None:
            result["quality_comparison"] = compare_legacy_and_v2_quality(
                legacy_updates,
                result,
                diagnosis=diagnosis,
            )
        return result

    def _call_llm_for_payload(
        self,
        messages: list[dict[str, str]],
        *,
        max_updates: int,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
        current_budget = self.max_completion_tokens
        attempts: list[dict[str, Any]] = []
        repair_messages = list(messages)
        last_preview = ""
        last_metadata: dict[str, Any] = {}
        last_error = "structured output validation failed"
        for attempt_number in range(1, MAX_STRUCTURED_OUTPUT_ATTEMPTS + 1):
            response = self.llm_client.chat(
                repair_messages,
                max_completion_tokens=current_budget,
                timeout_s=self.timeout_s,
            )
            response_content = getattr(response, "content", "")
            preview = _clean_text(response_content, 1200)
            metadata = _llm_response_metadata(response)
            last_preview = preview
            last_metadata = metadata
            try:
                payload = _parse_json_object(response_content)
                proposals, rejected = _validate_payload(
                    payload,
                    max_updates=max_updates,
                )
                if rejected:
                    details = "; ".join(
                        str(item.get("message") or item.get("reason") or "invalid")
                        for item in rejected
                    )
                    raise ValueError(f"prompt update schema validation failed: {details}")
                attempts.append(
                    {
                        **metadata,
                        "attempt": attempt_number,
                        "raw_response_content": response_content,
                        "parse_error": "",
                        "validation_error": "",
                        "schema_valid": True,
                    }
                )
                return proposals, preview, metadata, attempts
            except Exception as exc:
                last_error = _clean_text(exc, 1000)
                is_parse_error = "JSON" in str(exc) or "json" in str(exc)
                metadata = {
                    **metadata,
                    "attempt": attempt_number,
                    "raw_response_content": response_content,
                    "parse_error": last_error if is_parse_error else "",
                    "validation_error": last_error,
                    "schema_valid": False,
                }
                attempts.append(metadata)
                next_budget = _next_completion_budget(self.llm_client, current_budget)
                if (
                    metadata.get("finish_reason") == "length"
                    and next_budget is not None
                    and next_budget > current_budget
                ):
                    current_budget = next_budget
                if attempt_number >= MAX_STRUCTURED_OUTPUT_ATTEMPTS:
                    break
                repair_messages = [
                    *messages,
                    {"role": "assistant", "content": response_content},
                    {
                        "role": "user",
                        "content": (
                            "Your previous output failed validation: "
                            f"{last_error}\nCorrect that exact error and return "
                            "one JSON object only."
                        ),
                    },
                ]
        raise PromptUpdatePayloadError(
            last_error,
            response_preview=last_preview,
            response_metadata=last_metadata,
            response_attempts=attempts,
        )

    def build_messages(
        self,
        diagnosis: dict[str, Any],
        *,
        base_config: dict[str, Any] | None = None,
        existing_updates: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        proposal_json_schema = prompt_update_proposal_json_schema(
            max_updates=self.max_updates
        )
        payload = {
            "schema_version": PROMPT_UPDATE_OPTIMIZER_V2_SCHEMA_VERSION,
            "proposal_schema": _proposal_schema_view(),
            "proposal_json_schema": proposal_json_schema,
            "allowed_prompt_modules": list(PROMPT_MODULES),
            "allowed_operations": list(PROMPT_EDIT_OPERATIONS),
            "max_updates": self.max_updates,
            "rules": [
                "Prefer preserve_existing_update, suppress_existing_update, replace_existing_update, or abstain over add_append.",
                "Only propose add_append when no existing active block covers the failure pattern.",
                "Every proposal needs an evidence-bound applicability_condition.",
                "Every proposal needs a prompt_behavior_fixture acceptance_test.",
                "Every acceptance_test.should_contain string must be copied exactly from instruction_delta or replacement_delta.",
                "Do not propose memory_compiler or optimizer as target modules.",
                "Do not use prompt_patch remove/add diffs.",
                "Do not generalize structured-edit anchor mismatch unless the supplied evidence shows that exact cause.",
                "Do not require running or executing tests when solver_capabilities.validation_enabled is false.",
                "The applicability_condition must be decidable from solver_capabilities.prompt_visible_evidence before the first patch is generated.",
                "Do not condition an appended or replacement prompt block on a generated patch, materialization result, structured-edit failure, or harness result; those facts are unavailable when prompts are rendered.",
                "For add_append or replace_existing_update, the compiled applicability_condition plus instruction must be at most 600 characters.",
                "When prior_round_feedback is present, do not repeat its rejected_proposals and use its official promotion regressions as negative evidence.",
                "Return an empty proposals list when the evidence is weak or the update would be generic.",
            ],
            "bad_examples": [
                {
                    "instruction_delta": "Verify the fix thoroughly before submitting.",
                    "rejection_reason": "Generic verification prose with no observable behavior fixture.",
                },
                {
                    "instruction_delta": "When anchor mismatch happens, broaden context.",
                    "rejection_reason": "Invalid because the current task's materialization result is unavailable when the solver prompt is rendered.",
                },
            ],
            "good_example": {
                "schema_version": PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION,
                "operation": "add_append",
                "target_module": "patch_generation",
                "target_update_id": "",
                "failure_pattern": "wrong_file_localization.ambiguous_symbol",
                "applicability_condition": "Only when localized repository context contains multiple definitions with the same symbol name.",
                "instruction_delta": "When localized context contains same-named symbols, use the file path and enclosing class or function to select the intended definition.",
                "replacement_delta": "",
                "expected_behavior_change": "Solver disambiguates same-named symbols using repository evidence already visible in its prompt.",
                "acceptance_test": {
                    "type": "prompt_behavior_fixture",
                    "should_contain": ["file path and enclosing class or function"],
                    "should_not_contain": ["verify thoroughly"],
                },
                "evidence_refs": ["pattern_id:ambiguous_symbol_localization"],
            },
            "existing_update_blocks": _compact_existing_updates(existing_updates or []),
            "diagnosis": _compact_diagnosis(diagnosis),
            "solver_capabilities": _solver_capabilities(base_config),
        }
        text = json.dumps(_redact_jsonable(payload), sort_keys=True)
        if len(text) > MAX_V2_OPTIMIZER_INPUT_CHARS:
            payload["diagnosis"] = _compact_diagnosis(diagnosis, severe=True)
            payload["existing_update_blocks"] = payload["existing_update_blocks"][:12]
            text = json.dumps(_redact_jsonable(payload), sort_keys=True)
        skill_instruction = _structured_output_skill_instruction()
        system = (
            skill_instruction
            + "\n\n"
            "You are a schema-constrained optimizer for Trace2Prompt v2 prompt "
            "update proposals. You are not rewarded for adding instructions. "
            "Prefer deletion, replacement, preservation, or abstention unless a "
            "new prompt block changes observable solver behavior for a named "
            "failure mode. Return JSON only in this exact shape: "
            "{\"proposals\": [{\"schema_version\": "
            "\"prompt_update_proposal.v2\", \"operation\": \"add_append\", "
            "\"target_module\": \"patch_generation\", \"target_update_id\": "
            "\"\", \"failure_pattern\": \"...\", "
            "\"applicability_condition\": \"Only when ...\", "
            "\"instruction_delta\": \"...\", \"replacement_delta\": \"\", "
            "\"expected_behavior_change\": \"...\", \"acceptance_test\": "
            "{\"type\": \"prompt_behavior_fixture\", \"should_contain\": [], "
            "\"should_not_contain\": []}, \"evidence_refs\": []}]}. "
            "Return {\"proposals\": []} when no concrete update is justified."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]


def _validate_payload(
    payload: dict[str, Any],
    *,
    max_updates: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    unknown_top_level = sorted(set(payload) - {"proposals"})
    if unknown_top_level:
        raise ValueError(
            f"v2 optimizer response has unknown keys: {unknown_top_level}"
        )
    raw = payload.get("proposals")
    if not isinstance(raw, list):
        raise ValueError("v2 optimizer response missing proposals list")
    proposals: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, item in enumerate(raw[: max(0, max_updates)]):
        if not isinstance(item, dict):
            rejected.append({"index": index, "reason": "invalid_proposal_type"})
            continue
        try:
            proposals.append(normalize_prompt_update_proposal(item))
        except Exception as exc:
            rejected.append(
                {
                    "index": index,
                    "reason": "contract_violation",
                    "message": _clean_text(exc, 240),
                }
            )
    if len(raw) > max_updates:
        rejected.append(
            {
                "reason": "max_updates_exceeded",
                "dropped_count": len(raw) - max_updates,
            }
        )
    return proposals, rejected


def _structured_output_skill_instruction() -> str:
    try:
        text = STRUCTURED_OUTPUT_SKILL_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"optimizer structured-output skill is unavailable: {exc}"
        ) from exc
    if "---" in text:
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


def _solver_capabilities(base_config: dict[str, Any] | None) -> dict[str, Any]:
    workflow = (base_config or {}).get("workflow", {}) or {}
    return {
        "validation_enabled": bool(workflow.get("validation_enabled", False)),
        "validation_max_commands": int(workflow.get("validation_max_commands", 0) or 0),
        "visible_repository_evidence": True,
        "prompt_visible_evidence": [
            "issue_text",
            "failing_test_names",
            "localized_repository_context",
            "repository_file_hints",
        ],
    }


def _applicability_preservation_rate(
    proposals: list[dict[str, Any]],
    compiled_updates: list[dict[str, Any]],
) -> float:
    if not proposals:
        return 1.0
    preserved = 0
    for proposal, update in zip(proposals, compiled_updates):
        condition = _clean_text(proposal.get("applicability_condition") or "", 1000)
        compiled = _clean_text(
            update.get("replacement_delta")
            or update.get("instruction_delta")
            or "",
            2000,
        )
        if condition and condition.casefold() in compiled.casefold():
            preserved += 1
    return preserved / len(proposals)


def _proposal_schema_view() -> dict[str, Any]:
    return {
        "schema_version": PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION,
        "operation": list(PROMPT_EDIT_OPERATIONS),
        "target_module": list(PROMPT_MODULES),
        "target_update_id": "required for preserve/suppress/replace; empty for add_append",
        "failure_pattern": "diagnosed failure pattern label",
        "applicability_condition": "evidence-bound condition beginning with Only when/Only if/When/If",
        "instruction_delta": "new append body; empty for pure suppress/preserve",
        "replacement_delta": "replacement body for replace_existing_update",
        "expected_behavior_change": "observable solver behavior change",
        "acceptance_test": {
            "type": "prompt_behavior_fixture",
            "should_contain": "list of concrete strings expected in compiled prompt",
            "should_not_contain": "list of generic or obsolete strings to exclude",
        },
        "evidence_refs": "list of pattern_id/memory_id/source references",
    }


def _compact_existing_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        compact.append(
            {
                "target_module": _clean_text(update.get("target_module") or "", 80),
                "update_id": _clean_text(update.get("update_id") or "", 160),
                "operation": _clean_text(update.get("operation") or "", 80),
                "status": _clean_text(update.get("status") or "active", 80),
                "failure_pattern": _clean_text(update.get("failure_pattern") or "", 160),
                "instruction_delta": _clean_text(
                    update.get("instruction_delta")
                    or update.get("body_preview")
                    or "",
                    300,
                ),
            }
        )
    return compact


def _compact_diagnosis(
    diagnosis: dict[str, Any],
    *,
    severe: bool = False,
) -> dict[str, Any]:
    optimizer_input = diagnosis.get("optimizer_input")
    if not isinstance(optimizer_input, dict):
        optimizer_input = {}
    dominant_patterns = [
        _compact_pattern(pattern)
        for pattern in optimizer_input.get("dominant_patterns", [])[: (3 if severe else 6)]
        if isinstance(pattern, dict)
    ]
    return {
        "aggregation_schema_version": _clean_text(
            diagnosis.get("aggregation_schema_version") or "",
            120,
        ),
        "summary": _clean_text(diagnosis.get("summary") or "", 500),
        "failure_taxonomy": _redact_jsonable(diagnosis.get("failure_taxonomy", {})),
        "recommended_update_targets": [
            str(module)
            for module in optimizer_input.get("recommended_update_targets", [])
            if module in PROMPT_MODULES
        ],
        "dominant_patterns": dominant_patterns,
        "blocked_or_low_confidence_patterns": _redact_jsonable(
            optimizer_input.get("blocked_or_low_confidence_patterns", [])[:3]
        ),
        "evidence_summary": _redact_jsonable(optimizer_input.get("evidence_summary", {})),
        "prior_round_feedback": _redact_jsonable(
            diagnosis.get("prior_round_feedback", {})
        ),
    }


def _compact_pattern(pattern: dict[str, Any]) -> dict[str, Any]:
    return {
        "pattern_id": _clean_text(pattern.get("pattern_id") or "", 180),
        "primary_label": _clean_text(pattern.get("primary_label") or "", 120),
        "subcategories": _redact_jsonable(pattern.get("subcategories", [])),
        "failure_stage": _clean_text(pattern.get("failure_stage") or "", 80),
        "empty_patch_cause": _clean_text(pattern.get("empty_patch_cause") or "", 120),
        "support_count": pattern.get("support_count", 0),
        "optimizer_targets": [
            str(module)
            for module in pattern.get("optimizer_targets", [])
            if module in PROMPT_MODULES
        ],
        "optimizer_hint_summary": _clean_text(
            pattern.get("optimizer_hint_summary") or "",
            400,
        ),
        "low_support": bool(pattern.get("low_support", False)),
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("v2 optimizer response content was empty")
    fence = chr(96) * 3
    if cleaned.startswith(fence):
        cleaned = re.sub(r"^" + fence + r"(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*" + fence + r"$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("v2 optimizer response did not contain JSON")
    payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("v2 optimizer response must be a JSON object")
    return payload


def _llm_response_metadata(response: Any) -> dict[str, Any]:
    raw = getattr(response, "raw_response", None)
    if not isinstance(raw, dict):
        raw = {}
    choices = raw.get("choices") if isinstance(raw.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    content = getattr(response, "content", "")
    metadata = {
        "model": _clean_text(getattr(response, "model", "") or raw.get("model") or "", 120),
        "requested_model": _clean_text(getattr(response, "requested_model", "") or "", 120),
        "deployment": _clean_text(getattr(response, "deployment", "") or "", 120),
        "finish_reason": _clean_text(choice.get("finish_reason") or "", 120),
        "choice_count": len(choices),
        "message_keys": sorted(str(key) for key in message.keys()),
        "content_chars": len(str(content or "")),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        "request_max_completion_tokens": getattr(
            response,
            "request_max_completion_tokens",
            None,
        ),
        "request_timeout_s": getattr(response, "request_timeout_s", None),
        "request_latency_ms": getattr(response, "request_latency_ms", None),
        "request_attempts": list(getattr(response, "request_attempts", ()) or ()),
    }
    refusal = message.get("refusal")
    if refusal:
        metadata["refusal_preview"] = _clean_text(refusal, 600)
    annotations = message.get("annotations")
    if isinstance(annotations, list):
        metadata["annotation_count"] = len(annotations)
    return metadata


def _next_completion_budget(llm_client: Any, current: int) -> int | None:
    next_budget_fn = getattr(llm_client, "next_completion_budget", None)
    if callable(next_budget_fn):
        value = next_budget_fn(current)
        if value is None:
            return None
        return int(value)
    fallback_cap = 16384
    doubled = current * 2
    if current >= fallback_cap:
        return None
    return min(fallback_cap, doubled)


def _redact_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value, 1200)
    if isinstance(value, dict):
        return {
            str(key): _redact_jsonable(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_redact_jsonable(item) for item in value[:20]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean_text(value, 300)


def _clean_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > max_chars:
        return text[:max_chars].rstrip()
    return text
