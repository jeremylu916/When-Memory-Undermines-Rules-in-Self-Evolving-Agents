"""Offline optimizer that proposes structured, auditable prompt updates.

Self-evolution may only edit the six prompt modules, and every change is a
*structured update object*:

    {
      "target_module":    which of the six prompt modules to edit,
      "failure_pattern":  the failure taxonomy label this addresses,
      "instruction_delta": the text appended to that module,
      "expected_effect":  what the optimizer expects to improve,
    }

Free-form rewriting of a whole prompt is intentionally not allowed (that is the
``naive rewrite`` baseline). This is the rule-based version (R3); in M4 the body
is replaced by an LLM while this structured-update contract stays fixed.
"""

from __future__ import annotations

from typing import Any

from self_evolving_agent.candidate_builder import build_candidate_prompt_config
from self_evolving_agent.config import PROMPT_MODULES


# Self-evolution is only allowed to edit the six auditable prompt modules.
ALLOWED_OPTIMIZATION_AREAS = set(PROMPT_MODULES)

# Keys every structured update object must carry.
STRUCTURED_UPDATE_KEYS = (
    "target_module",
    "failure_pattern",
    "instruction_delta",
    "expected_effect",
)

OPTIMIZER_MODE_RULE = "rule"
OPTIMIZER_MODE_LLM = "llm"
OPTIMIZER_MODE_LLM_V2 = "llm_v2"


def propose_candidate_config(
    base_config: dict[str, Any],
    diagnosis: dict[str, Any],
) -> dict[str, Any]:
    updates = build_structured_updates(diagnosis)
    candidate = build_candidate_prompt_config(
        base_config,
        updates,
        diagnosis=diagnosis,
        candidate_prefix="candidate",
    )
    applied_updates = candidate.get("candidate_builder", {}).get(
        "applied_updates",
        [],
    )
    candidate["optimizer"] = {
        "source": "offline_rule_optimizer",
        "allowed_areas": ", ".join(sorted(ALLOWED_OPTIMIZATION_AREAS)),
        "edited_modules": ", ".join(
            update["target_module"] for update in applied_updates
        ),
        "structured_updates": applied_updates,
        "proposed_structured_updates": updates,
        "diagnosis_summary": diagnosis.get("summary", ""),
    }
    return candidate


def build_structured_updates(
    diagnosis: dict[str, Any],
    *,
    optimizer_mode: str = OPTIMIZER_MODE_RULE,
    llm_client: Any | None = None,
    base_config: dict[str, Any] | None = None,
    existing_updates: list[dict[str, Any]] | None = None,
    max_updates: int = 3,
    max_completion_tokens: int = 900,
    timeout_s: int | None = None,
    memory_block: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if optimizer_mode == OPTIMIZER_MODE_RULE:
        return _apply_self_evo_update_policy(
            _build_rule_structured_updates(diagnosis),
            diagnosis,
            max_updates=max_updates,
        )
    if optimizer_mode == OPTIMIZER_MODE_LLM:
        from self_evolving_agent.llm_optimizer import build_llm_structured_updates

        return _apply_self_evo_update_policy(
            build_llm_structured_updates(
                diagnosis,
                llm_client=llm_client,
                fallback_updates=_build_rule_structured_updates(diagnosis),
                max_updates=max_updates,
                max_completion_tokens=max_completion_tokens,
                timeout_s=timeout_s,
                memory_block=memory_block,
            ),
            diagnosis,
            max_updates=max_updates,
        )
    if optimizer_mode == OPTIMIZER_MODE_LLM_V2:
        from self_evolving_agent.prompt_update_optimizer_v2 import (
            build_v2_prompt_update_result,
        )

        result = build_v2_prompt_update_result(
            diagnosis,
            llm_client=llm_client,
            base_config=base_config,
            existing_updates=existing_updates,
            legacy_updates=_build_rule_structured_updates(diagnosis),
            max_updates=max_updates,
            max_completion_tokens=max_completion_tokens,
            timeout_s=timeout_s,
        )
        return result["compiled_updates"][:max_updates]
    raise ValueError(f"Unknown optimizer mode: {optimizer_mode}")


def _build_rule_structured_updates(diagnosis: dict[str, Any]) -> list[dict[str, str]]:
    """Map a critic diagnosis to a list of structured prompt-update objects.

    The mapping prefers the failure taxonomy (R2) when present, and falls back
    to the optimizer-facing recommendations for backward compatibility.
    """
    taxonomy = diagnosis.get("failure_taxonomy", {}) or {}
    recommendations = set(diagnosis.get("recommendations", []))
    dominant_patterns = _dominant_optimizer_patterns(diagnosis)
    recommended_targets = _recommended_optimizer_targets(diagnosis)

    updates: list[dict[str, str]] = []
    used_modules: set[str] = set()

    def add(target_module: str, failure_pattern: str, delta: str, effect: str) -> None:
        if target_module in used_modules:
            return
        used_modules.add(target_module)
        updates.append(
            {
                "target_module": target_module,
                "failure_pattern": failure_pattern,
                "instruction_delta": delta,
                "expected_effect": effect,
            }
        )

    for target_module in recommended_targets:
        pattern = _first_pattern_for_target(dominant_patterns, target_module)
        failure_pattern = _pattern_failure_label(pattern, target_module)
        add(
            target_module,
            failure_pattern,
            _delta_for_target(target_module, pattern),
            _effect_for_target(target_module, failure_pattern),
        )

    if _has_only_ignored_optimizer_patterns(diagnosis):
        return updates

    if taxonomy.get("vulnerability_not_fixed") or "enable_security_review" in recommendations:
        add(
            "security_review",
            "vulnerability_not_fixed",
            "Always check security-sensitive tasks for injection risks.",
            "Reduce vulnerability_not_fixed failures.",
        )

    if (
        taxonomy.get("test_failure")
        or taxonomy.get("regression_introduced")
        or "verify_before_submit" in recommendations
    ):
        add(
            "verification",
            "test_failure",
            "Verify the change before submitting the patch.",
            "Reduce test_failure and regression_introduced failures.",
        )

    if taxonomy.get("wrong_file_localization") or taxonomy.get(
        "repository_understanding_failure"
    ):
        add(
            "localization",
            "wrong_file_localization",
            "List the imports and files referenced by the issue, and edit those first.",
            "Reduce wrong_file_localization failures.",
        )

    if taxonomy.get("compilation_failure"):
        add(
            "patch_generation",
            "compilation_failure",
            "Ensure the patch is syntactically valid and imports resolve before finishing.",
            "Reduce compilation_failure failures.",
        )

    return updates


def _apply_self_evo_update_policy(
    updates: list[dict[str, Any]],
    diagnosis: dict[str, Any],
    *,
    max_updates: int = 3,
) -> list[dict[str, Any]]:
    """Keep clean self-evolution from collapsing into weak verifier-only edits.

    The optimizer may still propose verification updates, but if aggregated
    evidence already says the repair scaffold failed in patch generation or
    localization, the next prompt candidate must contain an executable repair
    policy for those modules. Otherwise a clean prompt-only round can spend its
    single update budget on "be careful / verify more" wording that has little
    leverage over patch generation.
    """

    max_updates = max(1, min(int(max_updates), 3))
    if _has_only_ignored_optimizer_patterns(diagnosis):
        return []

    normalized = [dict(update) for update in updates if isinstance(update, dict)]
    recommended_targets = _recommended_optimizer_targets(diagnosis)
    dominant_patterns = _dominant_optimizer_patterns(diagnosis)

    required_targets = _required_self_evo_targets(
        recommended_targets,
        dominant_patterns,
    )
    if not required_targets:
        return normalized[:max_updates]

    by_module = {
        str(update.get("target_module") or ""): update
        for update in normalized
        if isinstance(update, dict) and update.get("target_module")
    }
    policy_updates: dict[str, dict[str, Any]] = {}
    for target in required_targets:
        if target in by_module:
            continue
        pattern = _first_pattern_for_target(dominant_patterns, target)
        failure_pattern = _pattern_failure_label(pattern, target)
        policy_updates[target] = {
            "target_module": target,
            "failure_pattern": failure_pattern,
            "instruction_delta": _delta_for_target(target, pattern),
            "expected_effect": _effect_for_target(target, failure_pattern),
        }

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Required repair-policy modules get first claim on the limited update
    # budget. This is especially important when localization evidence exists:
    # a generic planning update must not crowd out the concrete localization
    # instruction that addresses wrong-file / empty-patch failures.
    for target in required_targets:
        update = by_module.get(target) or policy_updates.get(target)
        if update is None:
            continue
        ordered.append(update)
        seen.add(target)
        if len(ordered) >= max_updates:
            return ordered

    for update in normalized:
        module = str(update.get("target_module") or "")
        if not module or module in seen:
            continue
        seen.add(module)
        ordered.append(update)
        if len(ordered) >= max_updates:
            break
    return ordered


def _required_self_evo_targets(
    recommended_targets: list[str],
    dominant_patterns: list[dict[str, Any]],
) -> list[str]:
    if not dominant_patterns:
        return []

    required: list[str] = []
    if "patch_generation" in recommended_targets and any(
        _is_patch_generation_policy_pattern(pattern) for pattern in dominant_patterns
    ):
        required.append("patch_generation")
    if "localization" in recommended_targets and any(
        _is_localization_policy_pattern(pattern) for pattern in dominant_patterns
    ):
        required.append("localization")
    return required


def _is_patch_generation_policy_pattern(pattern: dict[str, Any]) -> bool:
    label = str(pattern.get("primary_label") or "")
    stage = str(pattern.get("failure_stage") or "")
    subcategories = pattern.get("subcategories", [])
    subcategory_set = set(subcategories if isinstance(subcategories, list) else [])
    targets = pattern.get("optimizer_targets", [])
    target_set = set(targets if isinstance(targets, list) else [])
    return bool(
        "patch_generation" in target_set
        and (
            label
            in {
                "test_failure",
                "compilation_failure",
                "empty_output",
                "malformed_json",
                "wrong_file_localization",
                "validation_patch_failure",
            }
            or stage in {"patch_generation", "localization", "official_harness"}
            or subcategory_set
            & {
                "fail_to_pass_failure",
                "empty_output",
                "structured_edit_anchor_mismatch",
                "structured_edit_parse_failed",
                "git_apply_failure",
                "no_patch_intent",
                "validation_rejected",
                "exact_find_not_found",
                "patch_serialization_failure",
                "git_apply_corrupt_patch",
            }
        )
    )


def _is_localization_policy_pattern(pattern: dict[str, Any]) -> bool:
    label = str(pattern.get("primary_label") or "")
    stage = str(pattern.get("failure_stage") or "")
    subcategories = pattern.get("subcategories", [])
    subcategory_set = set(subcategories if isinstance(subcategories, list) else [])
    targets = pattern.get("optimizer_targets", [])
    target_set = set(targets if isinstance(targets, list) else [])
    return bool(
        "localization" in target_set
        and (
            label in {"wrong_file_localization", "empty_output"}
            or stage == "localization"
            or "empty_output" in subcategory_set
            or "structured_edit_anchor_mismatch" in subcategory_set
            or str(pattern.get("empty_patch_cause") or "") == "structured_edit_anchor_mismatch"
        )
    )


def _dominant_optimizer_patterns(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        pattern
        for pattern in _raw_dominant_optimizer_patterns(diagnosis)
        if not _is_ignored_optimizer_pattern(pattern)
    ]


def _raw_dominant_optimizer_patterns(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
    optimizer_input = diagnosis.get("optimizer_input")
    if isinstance(optimizer_input, dict):
        patterns = optimizer_input.get("dominant_patterns")
        if isinstance(patterns, list):
            return [pattern for pattern in patterns if isinstance(pattern, dict)]
    patterns = diagnosis.get("top_failure_patterns")
    if isinstance(patterns, list):
        return [pattern for pattern in patterns if isinstance(pattern, dict)]
    return []


def _recommended_optimizer_targets(diagnosis: dict[str, Any]) -> list[str]:
    optimizer_input = diagnosis.get("optimizer_input")
    if not isinstance(optimizer_input, dict):
        return []
    targets = optimizer_input.get("recommended_update_targets", [])
    if not isinstance(targets, list):
        return []
    raw_patterns = _raw_dominant_optimizer_patterns(diagnosis)
    active_patterns = [
        pattern for pattern in raw_patterns if not _is_ignored_optimizer_pattern(pattern)
    ]
    allowed_by_active_patterns: set[str] | None = None
    if raw_patterns:
        allowed_by_active_patterns = {
            str(module)
            for pattern in active_patterns
            for module in pattern.get("optimizer_targets", [])
            if module in ALLOWED_OPTIMIZATION_AREAS
        }
        if not allowed_by_active_patterns:
            return []
    seen: set[str] = set()
    ordered = []
    for module in targets:
        if (
            module in ALLOWED_OPTIMIZATION_AREAS
            and module not in seen
            and (
                allowed_by_active_patterns is None
                or module in allowed_by_active_patterns
            )
        ):
            seen.add(module)
            ordered.append(str(module))
    return ordered


def _is_ignored_optimizer_pattern(pattern: dict[str, Any]) -> bool:
    subcategories = pattern.get("subcategories", [])
    subcategory_set = set(subcategories if isinstance(subcategories, list) else [])
    return (
        str(pattern.get("empty_patch_cause") or "").strip() == "length_exhausted"
        or "length_exhausted" in subcategory_set
        or pattern.get("empty_patch_actionability") == "non_actionable"
    )


def _has_only_ignored_optimizer_patterns(diagnosis: dict[str, Any]) -> bool:
    raw_patterns = _raw_dominant_optimizer_patterns(diagnosis)
    return bool(raw_patterns) and all(
        _is_ignored_optimizer_pattern(pattern) for pattern in raw_patterns
    )


def _first_pattern_for_target(
    patterns: list[dict[str, Any]],
    target_module: str,
) -> dict[str, Any]:
    for pattern in patterns:
        targets = pattern.get("optimizer_targets", [])
        if isinstance(targets, list) and target_module in targets:
            return pattern
    return patterns[0] if patterns else {}


def _pattern_failure_label(pattern: dict[str, Any], target_module: str) -> str:
    cause = str(pattern.get("empty_patch_cause") or "").strip()
    if cause and cause not in {"unknown", "api_error", "not_empty_patch"}:
        return f"empty_patch.{cause}"
    label = str(pattern.get("primary_label") or "").strip()
    if label:
        return label
    return {
        "security_review": "vulnerability_not_fixed",
        "verification": "test_failure",
        "localization": "wrong_file_localization",
        "patch_generation": "compilation_failure",
        "planning": "repository_understanding_failure",
        "system": "excessive_cost",
    }.get(target_module, "unknown_failure_pattern")


def _delta_for_target(target_module: str, pattern: dict[str, Any]) -> str:
    subcategories = pattern.get("subcategories", [])
    subcategory_set = set(subcategories if isinstance(subcategories, list) else [])
    empty_patch_cause = str(pattern.get("empty_patch_cause") or "").strip()
    if target_module == "security_review":
        return "Always check security-sensitive tasks for injection risks."
    if target_module == "verification":
        if empty_patch_cause == "validation_rejected":
            return (
                "When validation rejects a generated change, perform one bounded "
                "repair using the validation evidence instead of finalizing no patch."
            )
        if "pass_to_pass_regression" in subcategory_set:
            return (
                "Verify fail-to-pass fixes and selected pass-to-pass regression "
                "guards before submitting."
            )
        if "fail_to_pass_failure" in subcategory_set:
            return (
                "Before finalizing a patch, preserve the exact fail-to-pass "
                "behavior as an acceptance condition and reject fixes that only "
                "address adjacent plausible behavior."
            )
        return "Verify the change with focused tests and selected regression guards."
    if target_module == "localization":
        if empty_patch_cause == "structured_edit_anchor_mismatch":
            return (
                "If an exact structured-edit anchor cannot be matched, broaden "
                "to nearby stable symbols, imports, or call sites and cite the "
                "replacement anchor before patch generation."
            )
        if empty_patch_cause == "no_patch_intent":
            return (
                "Before deciding no code change is needed, identify the "
                "issue-linked files and symbols that were checked and continue "
                "searching when the first location is uncertain."
            )
        if "empty_output" in subcategory_set:
            return (
                "Before patch generation, name the issue-linked source files, "
                "symbols, and call paths that must change; if no precise source "
                "location is found, keep searching instead of emitting no patch."
            )
        return "List the imports and files referenced by the issue, and edit those first."
    if target_module == "patch_generation":
        if empty_patch_cause == "length_exhausted":
            return (
                "Generate the smallest one- or two-file patch first; avoid "
                "long exploratory output that can exhaust the response before "
                "a diff is emitted."
            )
        if empty_patch_cause == "structured_edit_anchor_mismatch":
            return (
                "Use compact contextual replacements anchored by stable nearby "
                "symbols; if an exact anchor fails, emit a minimal alternate "
                "hunk instead of ending with an empty patch."
            )
        if empty_patch_cause == "structured_edit_parse_failed":
            return (
                "Keep structured edits parseable and complete; when structure "
                "is uncertain, fall back to a minimal unified diff."
            )
        if empty_patch_cause == "git_apply_failure":
            return (
                "Emit applyable unified diffs with valid file headers, correct "
                "paths, and small hunks that git can apply cleanly."
            )
        if empty_patch_cause == "no_patch_intent":
            return (
                "Do not stop at analysis-only output; after localization, emit "
                "the minimal source change needed unless evidence proves no "
                "code change is required."
            )
        if empty_patch_cause == "validation_rejected":
            return (
                "Use validation rejection details to make one minimal repair "
                "rather than discarding the patch into an empty result."
            )
        if "fail_to_pass_failure" in subcategory_set:
            return (
                "When generating the patch, explicitly preserve the observed "
                "fail-to-pass assertion as the required behavior and implement "
                "the smallest source change that makes that assertion true."
            )
        if subcategory_set & {"empty_output", "structured_edit_anchor_mismatch", "exact_find_not_found"}:
            return (
                "For structured edits, emit small replacements anchored by "
                "nearby stable lines or exact symbols; avoid long brittle find "
                "blocks, and keep searching rather than returning an empty patch."
            )
        return "Keep patches minimal, syntactically valid, and aligned to the localized code."
    if target_module == "planning":
        if empty_patch_cause == "length_exhausted":
            return (
                "Plan a bounded edit path before generation so the final answer "
                "has budget left to emit the patch."
            )
        if empty_patch_cause == "no_patch_intent":
            return (
                "Treat no-change conclusions as provisional until issue-linked "
                "files, symbols, and failing behavior have been checked."
            )
        return "Plan from the diagnosed failure pattern before choosing files or commands."
    if target_module == "system":
        return "Keep self-evolution updates evidence-bound and avoid broad unrelated changes."
    return "Use the aggregated failure evidence to guide the next prompt update."


def _effect_for_target(target_module: str, failure_pattern: str) -> str:
    if failure_pattern.startswith("empty_patch."):
        cause = failure_pattern.removeprefix("empty_patch.")
        return f"Reduce empty patches caused by {cause} via {target_module} guidance."
    return f"Reduce {failure_pattern} failures by improving {target_module} guidance."


def _validate_update(update: dict[str, Any]) -> None:
    missing = [key for key in STRUCTURED_UPDATE_KEYS if not update.get(key)]
    if missing:
        raise ValueError(f"Structured update missing keys: {missing}")
    if update["target_module"] not in ALLOWED_OPTIMIZATION_AREAS:
        raise ValueError(
            f"Structured update targets non-allowed module: {update['target_module']}"
        )
