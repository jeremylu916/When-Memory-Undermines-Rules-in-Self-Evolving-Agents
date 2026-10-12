"""Schema-constrained LLM optimizer for controlled prompt deltas.

M5.3a lets an LLM propose structured update objects, but it is not allowed to
rewrite prompts freely. Unsafe or invalid output falls back to the existing rule
optimizer.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from self_evolving_agent.config import PROMPT_MODULES
from self_evolving_agent.optimizer_memory_contract import (
    DECISION_TYPES,
    EVIDENCE_ROLES,
    build_optimizer_memory_input,
    apply_memory_verifier_actions,
    render_optimizer_memory_prompt,
    verify_optimizer_memory_use,
)
from self_evolving_agent.optimizer import STRUCTURED_UPDATE_KEYS


MAX_INSTRUCTION_DELTA_CHARS = 600
MAX_EXPECTED_EFFECT_CHARS = 300
MAX_OPTIMIZER_INPUT_CHARS = 8000

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

_UNSAFE_UPDATE_MARKERS = (
    "disable verification",
    "turn off verification",
    "skip verification",
    "do not verify",
    "don't verify",
    "skip tests",
    "do not run tests",
    "don't run tests",
    "remove security review",
    "disable security",
    "turn off security",
    "ignore safety",
    "bypass safety",
)

_FREE_REWRITE_MARKERS = (
    "rewrite the whole prompt",
    "rewrite the entire prompt",
    "replace the whole prompt",
    "replace the entire prompt",
    "full prompt rewrite",
    "new complete prompt",
    "overwrite all prompts",
    "use this full prompt",
    "system prompt:",
    "task_prompt_template",
)

_FREE_REWRITE_KEYS = {
    "prompt",
    "prompts",
    "full_prompt",
    "new_prompt",
    "system_prompt",
    "task_prompt_template",
}


FallbackBuilder = Callable[[dict[str, Any]], list[dict[str, str]]]


def build_llm_structured_updates(
    diagnosis: dict[str, Any],
    *,
    llm_client: Any | None,
    fallback_updates: list[dict[str, str]] | None = None,
    fallback_builder: FallbackBuilder | None = None,
    max_updates: int = 3,
    max_completion_tokens: int = 900,
    timeout_s: int | None = None,
    memory_block: dict[str, Any] | None = None,
    return_metadata: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Build structured updates via LLM, falling back on rule updates."""

    optimizer = LLMOptimizer(
        llm_client,
        max_updates=max_updates,
        max_completion_tokens=max_completion_tokens,
        timeout_s=timeout_s,
    )
    return optimizer.build_updates(
        diagnosis,
        fallback_updates=fallback_updates,
        fallback_builder=fallback_builder,
        memory_block=memory_block,
        return_metadata=return_metadata,
    )


class LLMOptimizer:
    """Evidence-bound LLM optimizer with strict schema validation."""

    def __init__(
        self,
        llm_client: Any | None,
        *,
        max_updates: int = 3,
        max_completion_tokens: int = 900,
        timeout_s: int | None = None,
    ):
        self.llm_client = llm_client
        self.max_updates = max(1, min(int(max_updates), 3))
        self.max_completion_tokens = max(256, int(max_completion_tokens))
        self.timeout_s = timeout_s

    def build_updates(
        self,
        diagnosis: dict[str, Any],
        *,
        fallback_updates: list[dict[str, str]] | None = None,
        fallback_builder: FallbackBuilder | None = None,
        memory_block: dict[str, Any] | None = None,
        return_metadata: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        if memory_block is None and not return_metadata:
            if self.llm_client is None:
                return _fallback(diagnosis, fallback_updates, fallback_builder)

            try:
                response = self.llm_client.chat(
                    self.build_messages(diagnosis),
                    max_completion_tokens=self.max_completion_tokens,
                    timeout_s=self.timeout_s,
                )
                payload = _parse_json_object(getattr(response, "content", ""))
                return _validate_updates_payload(
                    payload,
                    diagnosis,
                    max_updates=self.max_updates,
                )
            except Exception:
                return _fallback(diagnosis, fallback_updates, fallback_builder)

        memory_input = None
        if memory_block is not None:
            memory_input = build_optimizer_memory_input(memory_block)
        if self.llm_client is None:
            updates = _fallback(diagnosis, fallback_updates, fallback_builder)
            return _optimizer_result(
                updates,
                memory_input=memory_input,
                verifier_report=None,
                fallback_reason="missing_llm_client",
            )
        try:
            response = self.llm_client.chat(
                self.build_messages(diagnosis, memory_block=memory_block),
                max_completion_tokens=self.max_completion_tokens,
                timeout_s=self.timeout_s,
            )
            payload = _parse_json_object(getattr(response, "content", ""))
            updates = _validate_updates_payload(
                payload,
                diagnosis,
                max_updates=self.max_updates,
                allow_memory_fields=memory_input is not None,
                memory_input=memory_input,
            )
            verifier_report = None
            if memory_input is not None:
                verifier_report = verify_optimizer_memory_use(updates, memory_input)
                verified_updates = apply_memory_verifier_actions(
                    updates,
                    verifier_report,
                )
                updates = verified_updates or _fallback(
                    diagnosis,
                    fallback_updates,
                    fallback_builder,
                )
            if return_metadata:
                return _optimizer_result(
                    updates,
                    memory_input=memory_input,
                    verifier_report=verifier_report,
                )
            return updates
        except Exception as exc:
            updates = _fallback(diagnosis, fallback_updates, fallback_builder)
            if return_metadata:
                return _optimizer_result(
                    updates,
                    memory_input=memory_input,
                    verifier_report=None,
                    fallback_reason=_clean_text(exc, 240),
                )
            return updates

    def build_messages(
        self,
        diagnosis: dict[str, Any],
        *,
        memory_block: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        compact = _compact_optimizer_diagnosis(diagnosis)
        if memory_block is not None:
            memory_input = build_optimizer_memory_input(memory_block)
            return self._build_memory_grounded_messages(compact, memory_input)
        system = (
            "You are a schema-constrained optimizer for controlled prompt "
            "evolution. Use only the supplied M5.2 optimizer evidence. Return "
            "JSON only in this exact shape: {\"updates\": [{\"target_module\": "
            "\"verification\", \"failure_pattern\": \"test_failure\", "
            "\"instruction_delta\": \"...\", \"expected_effect\": \"...\"}]}. "
            "Do not rewrite full prompts. Do not remove, weaken, or disable "
            "verification or security review. Propose at most 3 compact prompt "
            "deltas. Prefer actionable dominant_patterns and avoid optimizing "
            "low-support or blocked patterns when better actionable patterns "
            "exist. If empty_patch_cause evidence is available, optimize the "
            "specific cause using failure_pattern empty_patch.<cause>; do not "
            "propose generic avoid-empty-patch wording. Use only allowed prompt modules."
        )
        user = json.dumps(
            {
                "allowed_prompt_modules": list(PROMPT_MODULES),
                "structured_update_schema": list(STRUCTURED_UPDATE_KEYS),
                "max_updates": self.max_updates,
                "optimizer_input": compact.get("optimizer_input", {}),
                "failure_taxonomy": compact.get("failure_taxonomy", {}),
                "aggregation_schema_version": compact.get(
                    "aggregation_schema_version", ""
                ),
            },
            sort_keys=True,
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_memory_grounded_messages(
        self,
        compact: dict[str, Any],
        memory_input: dict[str, Any],
    ) -> list[dict[str, str]]:
        memory_prompt = render_optimizer_memory_prompt(memory_input)
        system = (
            "You are a schema-constrained optimizer for controlled prompt "
            "evolution. Use the supplied M5.2 optimizer evidence and the "
            "Trace2Prompt M6.7 memory contract. Memory may influence prompt "
            "updates only; do not use it for direct patch generation. Every "
            "memory-supported decision must cite memory_id. If an update is "
            "not based on memory, set memory_citations=[] and "
            "evidence_role=\"not_used\". Do not generalize task or repo memory "
            "into global policy. Critical negative memory is a guardrail. "
            "Cautions may prevent false lessons but cannot become positive "
            "patch-generation rules. Separate evidence, hypothesis, and "
            "decision. A memory citation proves only that retrieved memory "
            "existed, not that the update is automatically correct. Return "
            "JSON only in this exact shape: {\"updates\": [{\"target_module\": "
            "\"verification\", \"failure_pattern\": \"test_failure\", "
            "\"instruction_delta\": \"...\", \"expected_effect\": \"...\", "
            "\"memory_citations\": [\"mem_...\"], \"evidence_role\": "
            "\"support\", \"decision_type\": \"modify\", \"scope_assumption\": "
            "\"benchmark\", \"anti_overfit_rationale\": \"...\", \"risk\": "
            "\"...\", \"do_not_repeat\": []}]}. Do not rewrite full prompts. "
            "Do not remove, weaken, or disable verification or security review. "
            "Propose at most 3 compact prompt deltas. Use only allowed prompt modules."
        )
        user = json.dumps(
            {
                "allowed_prompt_modules": list(PROMPT_MODULES),
                "base_structured_update_schema": list(STRUCTURED_UPDATE_KEYS),
                "memory_grounded_update_schema": {
                    "memory_citations": "list of cited selected memory_id values",
                    "evidence_role": list(EVIDENCE_ROLES),
                    "decision_type": list(DECISION_TYPES),
                    "scope_assumption": ["task", "repo", "benchmark", "global"],
                    "anti_overfit_rationale": "brief rationale",
                    "risk": "brief risk statement",
                    "do_not_repeat": "list of prompt-update mistakes to avoid",
                },
                "max_updates": self.max_updates,
                "optimizer_input": compact.get("optimizer_input", {}),
                "failure_taxonomy": compact.get("failure_taxonomy", {}),
                "aggregation_schema_version": compact.get(
                    "aggregation_schema_version",
                    "",
                ),
                "trace2prompt_memory": _memory_prompt_payload(memory_input),
                "trace2prompt_memory_text": memory_prompt,
                "evidence_hierarchy": memory_input["evidence_hierarchy"],
                "anti_overfit_rules": [
                    "Do not generalize task or repo memory to global wording.",
                    "Do not turn infrastructure cautions into positive repair rules.",
                    "Do not repeat critical negative memory as an update.",
                    "Cite only selected_memory_ids.",
                ],
            },
            sort_keys=True,
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]


def _validate_updates_payload(
    payload: dict[str, Any],
    diagnosis: dict[str, Any],
    *,
    max_updates: int,
    allow_memory_fields: bool = False,
    memory_input: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    updates = payload.get("updates")
    if not isinstance(updates, list):
        raise ValueError("optimizer response missing updates list")

    context = _optimizer_context(diagnosis, memory_input=memory_input)
    validated = []
    for raw_update in updates[:max_updates]:
        if not isinstance(raw_update, dict):
            raise ValueError("each update must be an object")
        validated.append(
            _validate_update(
                raw_update,
                context,
                allow_memory_fields=allow_memory_fields,
            )
        )
    return validated


def _validate_update(
    update: dict[str, Any],
    context: dict[str, Any],
    *,
    allow_memory_fields: bool = False,
) -> dict[str, Any]:
    if _FREE_REWRITE_KEYS & set(update):
        raise ValueError("free prompt rewrite keys are not allowed")

    missing = [key for key in STRUCTURED_UPDATE_KEYS if not update.get(key)]
    if missing:
        raise ValueError(f"structured update missing keys: {missing}")

    target_module = _clean_text(update["target_module"], 80)
    if target_module not in PROMPT_MODULES:
        raise ValueError(f"illegal target_module: {target_module}")

    failure_pattern = _clean_text(update["failure_pattern"], 120)
    allowed_labels = context["preferred_labels"] or context["known_labels"]
    if allowed_labels and failure_pattern not in allowed_labels:
        raise ValueError(
            f"failure_pattern does not match dominant patterns: {failure_pattern}"
        )

    instruction_delta = _clean_text(
        update["instruction_delta"],
        MAX_INSTRUCTION_DELTA_CHARS,
    )
    expected_effect = _clean_text(
        update["expected_effect"],
        MAX_EXPECTED_EFFECT_CHARS,
    )
    if not instruction_delta.strip() or not expected_effect.strip():
        raise ValueError("instruction_delta and expected_effect must be non-empty")
    if _generic_empty_patch_update(
        failure_pattern,
        instruction_delta,
        expected_effect,
        context,
    ):
        raise ValueError("generic empty-patch update rejected")
    if _looks_like_full_prompt_rewrite(update) or _unsafe_update_text(
        instruction_delta + " " + expected_effect
    ):
        raise ValueError("unsafe or free-form prompt rewrite rejected")

    validated: dict[str, Any] = {
        "target_module": target_module,
        "failure_pattern": failure_pattern,
        "instruction_delta": instruction_delta,
        "expected_effect": expected_effect,
    }
    if allow_memory_fields:
        validated.update(_validate_memory_update_fields(update))
    return validated


def _validate_memory_update_fields(update: dict[str, Any]) -> dict[str, Any]:
    citations = [
        _clean_text(item, 120)
        for item in (update.get("memory_citations") or [])
        if isinstance(item, str)
    ][:8]
    do_not_repeat = [
        _clean_text(item, 240)
        for item in (update.get("do_not_repeat") or [])
        if isinstance(item, str)
    ][:8]
    evidence_role = _clean_text(update.get("evidence_role") or "not_used", 80)
    decision_type = _clean_text(update.get("decision_type") or "modify", 80)
    scope_assumption = _clean_text(update.get("scope_assumption") or "", 80)
    return {
        "memory_citations": citations,
        "evidence_role": evidence_role,
        "decision_type": decision_type,
        "scope_assumption": scope_assumption,
        "anti_overfit_rationale": _clean_text(
            update.get("anti_overfit_rationale") or "",
            300,
        ),
        "risk": _clean_text(update.get("risk") or "", 300),
        "do_not_repeat": do_not_repeat,
    }


def _optimizer_context(
    diagnosis: dict[str, Any],
    *,
    memory_input: dict[str, Any] | None = None,
) -> dict[str, set[str]]:
    optimizer_input = diagnosis.get("optimizer_input")
    if not isinstance(optimizer_input, dict):
        optimizer_input = {}

    blocked_ids = {
        str(pattern.get("pattern_id", ""))
        for pattern in optimizer_input.get("blocked_or_low_confidence_patterns", [])
        if isinstance(pattern, dict)
    }
    known_labels: set[str] = set()
    preferred_labels: set[str] = set()
    empty_patch_causes: set[str] = set()
    for pattern in optimizer_input.get("dominant_patterns", []) or []:
        if not isinstance(pattern, dict):
            continue
        label = str(pattern.get("primary_label") or "").strip()
        if not label:
            continue
        known_labels.add(label)
        pattern_id = str(pattern.get("pattern_id", ""))
        low_support = bool(pattern.get("low_support", False))
        preferred = pattern_id not in blocked_ids and not low_support
        if preferred:
            preferred_labels.add(label)
        for subcategory in pattern.get("subcategories", []) or []:
            subcategory = str(subcategory or "").strip()
            if not subcategory:
                continue
            known_labels.add(subcategory)
            if preferred:
                preferred_labels.add(subcategory)
        empty_patch_cause = str(pattern.get("empty_patch_cause") or "").strip()
        if empty_patch_cause:
            empty_patch_causes.add(empty_patch_cause)
            for cause_label in (
                empty_patch_cause,
                f"empty_patch.{empty_patch_cause}",
            ):
                known_labels.add(cause_label)
                if preferred:
                    preferred_labels.add(cause_label)

    if not known_labels:
        for label in (diagnosis.get("failure_taxonomy", {}) or {}):
            known_labels.add(str(label))
    if isinstance(memory_input, dict):
        for item in memory_input.get("memory_items", []):
            if not isinstance(item, dict):
                continue
            root_cause = str(item.get("root_cause") or "").strip()
            if root_cause:
                known_labels.add(root_cause)
                preferred_labels.add(root_cause)
    return {
        "known_labels": known_labels,
        "preferred_labels": preferred_labels,
        "empty_patch_causes": empty_patch_causes,
    }


def _compact_optimizer_diagnosis(diagnosis: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "aggregation_schema_version": diagnosis.get("aggregation_schema_version", ""),
        "optimizer_input": _compact_optimizer_input(diagnosis.get("optimizer_input", {})),
        "failure_taxonomy": _clean_jsonable(diagnosis.get("failure_taxonomy", {}), 500),
    }
    text = json.dumps(compact, sort_keys=True)
    if len(text) <= MAX_OPTIMIZER_INPUT_CHARS:
        return compact
    compact["optimizer_input"] = {
        "dominant_patterns": compact["optimizer_input"].get("dominant_patterns", [])[:3],
        "recommended_update_targets": compact["optimizer_input"].get(
            "recommended_update_targets", []
        ),
        "blocked_or_low_confidence_patterns": compact["optimizer_input"].get(
            "blocked_or_low_confidence_patterns", []
        )[:3],
        "evidence_summary": compact["optimizer_input"].get("evidence_summary", {}),
    }
    return compact


def _compact_optimizer_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "dominant_patterns": [
            _compact_pattern(pattern)
            for pattern in value.get("dominant_patterns", [])[:5]
            if isinstance(pattern, dict)
        ],
        "recommended_update_targets": [
            str(module)
            for module in value.get("recommended_update_targets", [])
            if module in PROMPT_MODULES
        ],
        "blocked_or_low_confidence_patterns": [
            _clean_jsonable(pattern, 500)
            for pattern in value.get("blocked_or_low_confidence_patterns", [])[:5]
            if isinstance(pattern, dict)
        ],
        "evidence_summary": _clean_jsonable(value.get("evidence_summary", {}), 1000),
        "empty_patch_cause_summary": _clean_jsonable(
            value.get("empty_patch_cause_summary", []),
            1200,
        ),
    }


def _compact_pattern(pattern: dict[str, Any]) -> dict[str, Any]:
    return {
        "pattern_id": _clean_text(pattern.get("pattern_id", ""), 160),
        "primary_label": _clean_text(pattern.get("primary_label", ""), 120),
        "subcategories": _clean_jsonable(pattern.get("subcategories", []), 300),
        "failure_stage": _clean_text(pattern.get("failure_stage", ""), 80),
        "evidence_quality": _clean_text(pattern.get("evidence_quality", ""), 80),
        "empty_patch_cause": _clean_text(pattern.get("empty_patch_cause", ""), 120),
        "empty_patch_actionability": _clean_text(
            pattern.get("empty_patch_actionability", ""),
            80,
        ),
        "support_count": pattern.get("support_count", 0),
        "weighted_support": pattern.get("weighted_support", 0.0),
        "mean_confidence": pattern.get("mean_confidence", 0.0),
        "optimizer_targets": [
            str(module)
            for module in pattern.get("optimizer_targets", [])
            if module in PROMPT_MODULES
        ],
        "optimizer_hint_summary": _clean_text(
            pattern.get("optimizer_hint_summary", ""),
            400,
        ),
        "low_support": bool(pattern.get("low_support", False)),
    }


def _generic_empty_patch_update(
    failure_pattern: str,
    instruction_delta: str,
    expected_effect: str,
    context: dict[str, set[str]],
) -> bool:
    causes = {
        str(cause).strip()
        for cause in context.get("empty_patch_causes", set())
        if str(cause).strip()
    }
    if not causes:
        return False

    text = f"{failure_pattern} {instruction_delta} {expected_effect}".lower()
    if not any(
        marker in text
        for marker in (
            "empty patch",
            "empty patches",
            "empty_output",
            "empty output",
            "no patch",
        )
    ):
        return False

    if any(cause.lower() in text for cause in causes):
        return False
    if any(f"empty_patch.{cause}".lower() in text for cause in causes):
        return False

    cause_markers = {
        "structured_edit_anchor_mismatch": (
            "anchor",
            "exact",
            "target text",
            "nearby",
            "symbol",
            "contextual replacement",
        ),
        "length_exhausted": (
            "length",
            "token",
            "budget",
            "exhaust",
            "one-file",
            "two-file",
            "minimal edit",
        ),
        "structured_edit_parse_failed": (
            "parse",
            "json",
            "structured edit",
            "malformed",
            "unified diff fallback",
        ),
        "git_apply_failure": (
            "git apply",
            "unified diff",
            "file header",
            "hunk",
            "applyable",
        ),
        "no_patch_intent": (
            "decline",
            "no change",
            "cannot reproduce",
            "already fixed",
            "issue-linked",
        ),
        "validation_rejected": (
            "validation",
            "rejected",
            "repair",
            "failing check",
        ),
    }
    specific_markers = []
    for cause in causes:
        specific_markers.extend(cause_markers.get(cause, ()))
    return not any(marker in text for marker in specific_markers)


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("optimizer response did not contain JSON")
    payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("optimizer response must be a JSON object")
    return payload


def _looks_like_full_prompt_rewrite(update: dict[str, Any]) -> bool:
    text = " ".join(str(value) for value in update.values()).lower()
    if any(marker in text for marker in _FREE_REWRITE_MARKERS):
        return True
    module_mentions = sum(1 for module in PROMPT_MODULES if f"{module}:" in text)
    return module_mentions >= 2


def _unsafe_update_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _UNSAFE_UPDATE_MARKERS)


def _fallback(
    diagnosis: dict[str, Any],
    fallback_updates: list[dict[str, str]] | None,
    fallback_builder: FallbackBuilder | None,
) -> list[dict[str, str]]:
    if fallback_updates is not None:
        return list(fallback_updates)
    if fallback_builder is not None:
        return fallback_builder(diagnosis)
    from self_evolving_agent.optimizer import build_structured_updates

    return build_structured_updates(diagnosis)


def _optimizer_result(
    updates: list[dict[str, Any]],
    *,
    memory_input: dict[str, Any] | None,
    verifier_report: dict[str, Any] | None,
    fallback_reason: str = "",
) -> dict[str, Any]:
    return {
        "updates": list(updates),
        "optimizer_memory_input": memory_input,
        "verifier_report": verifier_report,
        "fallback_reason": fallback_reason,
        "memory_used": bool(memory_input is not None and not fallback_reason),
    }


def _memory_prompt_payload(memory_input: dict[str, Any]) -> dict[str, Any]:
    sections = {
        "critical_negative": [],
        "cautions": [],
        "contradictions_and_scope_limits": [],
        "positive_scoped_lessons": [],
    }
    for item in memory_input.get("memory_items", []):
        section = item.get("section")
        if section in {"global_invariants", "benchmark_lessons", "repo_lessons", "task_lessons"}:
            sections["positive_scoped_lessons"].append(item)
        elif section in sections:
            sections[section].append(item)
    return {
        "selected_memory_ids": memory_input.get("selected_memory_ids", []),
        "retrieval_boundary": memory_input.get("retrieval_boundary", {}),
        **sections,
    }


def _clean_jsonable(value: Any, text_limit: int) -> Any:
    if isinstance(value, str):
        return _clean_text(value, text_limit)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = _clean_text(key, 120)
            if _looks_secret_key(str(key)):
                cleaned[clean_key] = "[REDACTED]"
            else:
                cleaned[clean_key] = _clean_jsonable(item, text_limit)
        return cleaned
    if isinstance(value, list):
        return [_clean_jsonable(item, text_limit) for item in value[:20]]
    if isinstance(value, tuple):
        return [_clean_jsonable(item, text_limit) for item in value[:20]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean_text(value, text_limit)


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: max(limit - 3, 0)] + "..."
    return text


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        marker in lowered
        for marker in ("api_key", "apikey", "authorization", "password", "secret", "token")
    )
