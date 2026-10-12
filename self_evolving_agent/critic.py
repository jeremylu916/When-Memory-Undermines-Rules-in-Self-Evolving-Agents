"""Offline critic that reads full traces and assigns a structured failure taxonomy.

This is the rule-based version (R2). The contract is what matters for late
work: the critic consumes complete trace records (command results, file edits,
test results, patch diff, cost) and emits, for each failed trace, one or more
labels from a fixed eight-class failure taxonomy. In M4 the rule body is
replaced by an LLM while this input/output contract stays fixed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from self_evolving_agent.failure_aggregator import aggregate_failure_diagnosis
from self_evolving_agent.failure_cause import build_failure_cause_event


# The eight structured failure types (matches idea.md §4 / PDF Table 4).
FAILURE_TAXONOMY = (
    "wrong_file_localization",
    "repository_understanding_failure",
    "compilation_failure",
    "test_failure",
    "regression_introduced",
    "vulnerability_not_fixed",
    "tool_misuse",
    "excessive_cost",
)

CRITIC_MODE_RULE = "rule"
CRITIC_MODE_LLM = "llm"

FAILURE_STAGES = (
    "localization",
    "generation",
    "materialization",
    "validation",
    "official_harness",
    "environment",
    "cost",
)

FAILURE_SUBCATEGORIES = (
    "missing_relevant_file",
    "wrong_function_or_class",
    "docstring_or_header_distraction",
    "insufficient_test_patch_signal",
    "empty_output",
    "length_exhausted",
    "structured_edit_anchor_mismatch",
    "structured_edit_parse_failed",
    "git_apply_failure",
    "no_patch_intent",
    "validation_rejected",
    "api_error",
    "unknown_empty_patch",
    "malformed_json",
    "hallucinated_old_code",
    "unsafe_or_overbroad_patch",
    "exact_find_not_found",
    "line_range_guard_mismatch",
    "partial_edit_rejected",
    "patch_syntax_error",
    "fail_to_pass_failure",
    "pass_to_pass_regression",
    "flaky_or_environmental_test",
    "overfit_to_added_tests",
    "validation_environment_failure",
    "validation_patch_failure",
    "image_missing",
    "testbed_setup_failed",
    "dependency_missing",
    "network_service_failure",
    "gold_failing_instance",
    "excessive_retries",
    "excessive_tokens",
    "timeout_loop",
    "security_review_missing",
    "vulnerability_pattern_unaddressed",
)

# Cost above this (mock units) is flagged as excessive. Real cost gating is M4.
EXCESSIVE_COST_THRESHOLD = 1.0

# Substrings in command stderr that indicate the patch did not compile / import.
_COMPILATION_ERROR_MARKERS = (
    "syntaxerror",
    "indentationerror",
    "importerror",
    "modulenotfounderror",
    "cannot import name",
    "compilation terminated",
    "error:",
)

# Substrings in agent steps that indicate it failed to find the target to edit.
_LOCALIZATION_MISS_MARKERS = (
    "target_file_missing",
    "target_text_not_found",
    "patch_file_missing",
    "patch_target_not_found",
    "no_strategy_matched",
)

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

_MAX_TEXT_FIELD_CHARS = 1200

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

_EMPTY_PATCH_CAUSE_STAGE = {
    "api_error": "environment",
    "length_exhausted": "generation",
    "structured_edit_anchor_mismatch": "materialization",
    "structured_edit_parse_failed": "generation",
    "git_apply_failure": "materialization",
    "no_patch_intent": "generation",
    "validation_rejected": "validation",
    "unknown": "localization",
}

_EMPTY_PATCH_CAUSE_TARGETS = {
    "structured_edit_anchor_mismatch": ["localization", "patch_generation"],
    "structured_edit_parse_failed": ["patch_generation"],
    "git_apply_failure": ["patch_generation"],
    "no_patch_intent": ["planning", "patch_generation"],
    "validation_rejected": ["patch_generation", "verification"],
}

_EMPTY_PATCH_PROMPT_LEARNABLE_CAUSES = {
    "structured_edit_anchor_mismatch",
    "structured_edit_parse_failed",
    "git_apply_failure",
    "no_patch_intent",
    "validation_rejected",
}


def diagnose_failures(
    records: list[dict[str, Any]],
    *,
    critic_mode: str = CRITIC_MODE_RULE,
    llm_client: Any | None = None,
    max_evidence_chars: int = 12000,
) -> dict[str, Any]:
    failed = [record for record in records if not record["success"]]

    per_task: list[dict[str, Any]] = []
    failure_analyses: list[dict[str, Any]] = []
    label_counts: dict[str, int] = {label: 0 for label in FAILURE_TAXONOMY}
    failed_categories: dict[str, int] = {}

    for record in failed:
        category = _record_category(record)
        failed_categories[category] = failed_categories.get(category, 0) + 1

        analysis = analyze_failure(
            record,
            critic_mode=critic_mode,
            llm_client=llm_client,
            max_evidence_chars=max_evidence_chars,
        )
        labels = analysis["labels"]
        for label in labels:
            label_counts[label] += 1
        failure_analyses.append(analysis)
        per_task.append(
            {
                "task_id": _record_task_id(record),
                "category": category,
                "execution_status": record.get("execution_status"),
                "labels": labels,
                "primary_label": analysis["primary_label"],
                "failure_stage": analysis["failure_stage"],
                "subcategories": analysis["subcategories"],
                "confidence": analysis["confidence"],
                "optimizer_hint": analysis["optimizer_hint"],
                "critic_source": analysis["critic_source"],
                "empty_patch": bool(analysis.get("empty_patch", False)),
                "empty_patch_cause": analysis.get("empty_patch_cause", ""),
                "empty_patch_actionability": analysis.get(
                    "empty_patch_actionability",
                    "",
                ),
            }
        )

    recommendations = _recommend(label_counts, failed_categories)

    diagnosis = {
        "failed_task_count": len(failed),
        "failed_categories": failed_categories,
        "failure_taxonomy": {
            label: count for label, count in label_counts.items() if count
        },
        "per_task_diagnosis": per_task,
        "failure_analyses": failure_analyses,
        "recommendations": recommendations,
        "summary": _summary(label_counts, failed_categories),
        "critic": {
            "mode": critic_mode,
            "taxonomy": list(FAILURE_TAXONOMY),
            "subcategories": list(FAILURE_SUBCATEGORIES),
        },
    }
    return aggregate_failure_diagnosis(diagnosis)


def analyze_failure(
    record: dict[str, Any],
    *,
    critic_mode: str = CRITIC_MODE_RULE,
    llm_client: Any | None = None,
    max_evidence_chars: int = 12000,
) -> dict[str, Any]:
    if critic_mode == CRITIC_MODE_RULE:
        return _rule_based_analysis(record)
    if critic_mode == CRITIC_MODE_LLM:
        return LLMCritic(
            llm_client,
            max_evidence_chars=max_evidence_chars,
        ).analyze(record)
    raise ValueError(f"Unknown critic mode: {critic_mode}")


class LLMCritic:
    """Evidence-bound LLM critic with schema validation and rule fallback."""

    def __init__(
        self,
        llm_client: Any | None,
        *,
        max_evidence_chars: int = 12000,
        max_completion_tokens: int = 1200,
        timeout_s: int | None = None,
    ):
        self.llm_client = llm_client
        self.max_evidence_chars = max(1000, int(max_evidence_chars))
        self.max_completion_tokens = max(256, int(max_completion_tokens))
        self.timeout_s = timeout_s

    def analyze(self, record: dict[str, Any]) -> dict[str, Any]:
        fallback = _rule_based_analysis(record)
        if self.llm_client is None:
            return _llm_fallback(
                fallback,
                reason="critic_not_configured",
                error="LLM critic requested without an LLM client.",
            )

        try:
            response = self.llm_client.chat(
                self.build_messages(record),
                max_completion_tokens=self.max_completion_tokens,
                timeout_s=self.timeout_s,
            )
        except Exception as exc:  # pragma: no cover - exact provider errors vary.
            return _llm_fallback(
                fallback,
                reason="llm_error",
                error=str(exc),
            )

        try:
            payload = _parse_json_object(response.content)
            analysis = _validate_llm_analysis(payload, record, fallback)
        except ValueError as exc:
            return _llm_fallback(
                fallback,
                reason="critic_parse_error",
                error=str(exc),
                response=response,
            )

        analysis = _apply_official_harness_invariants(analysis, record, fallback)
        analysis = _with_empty_patch_cause(analysis, record)
        analysis["critic_source"] = "llm"
        analysis["critic_model"] = getattr(response, "model", "")
        analysis["critic_usage"] = _usage_to_dict(getattr(response, "usage", None))
        analysis["fallback_labels"] = list(fallback["labels"])
        return analysis

    def build_messages(self, record: dict[str, Any]) -> list[dict[str, str]]:
        evidence_json = _build_evidence_json(record, self.max_evidence_chars)
        system = (
            "You are an evidence-bound failure critic for a self-evolving "
            "software agent. Classify only failed traces. Use only the supplied "
            "evidence bundle. Keep the fixed top-level taxonomy stable. Return "
            "JSON only with keys: labels, primary_label, subcategories, "
            "failure_stage, evidence, confidence, optimizer_hint, notes. Labels "
            "must come from the taxonomy; subcategories and failure_stage must "
            "come from the provided vocabularies. Evidence items must cite a "
            "field/source from the evidence bundle and include a short summary. "
            "When official_harness is present, it is the source of truth for "
            "final failure attribution and takes precedence over generation-time "
            "validation command_results. "
            "If patch.patch_text_present, patch.model_patch_chars, or "
            "official_harness.patch_exists show that a patch exists, do not "
            "infer empty_output merely because file_edits are absent; treat "
            "that as evidence_missing. If official_harness shows "
            "patch_successfully_applied=true, FAIL_TO_PASS has no failures, "
            "and PASS_TO_PASS has failures, classify it as "
            "regression_introduced with pass_to_pass_regression even when "
            "generation-time validation command_results contain environment "
            "failures. "
            "Do not reveal secrets; write [REDACTED] for any secret-like text."
        )
        user = json.dumps(
            {
                "taxonomy": list(FAILURE_TAXONOMY),
                "failure_stages": list(FAILURE_STAGES),
                "subcategories": list(FAILURE_SUBCATEGORIES),
                "required_json_shape": {
                    "labels": ["test_failure"],
                    "primary_label": "test_failure",
                    "subcategories": ["fail_to_pass_failure"],
                    "failure_stage": "validation",
                    "evidence": [
                        {
                            "source": "command_results[0].stderr",
                            "summary": "Focused test failed after the patch.",
                            "quote": "AssertionError",
                        }
                    ],
                    "confidence": 0.0,
                    "optimizer_hint": "Concrete prompt-update hint.",
                    "notes": "Optional short note.",
                },
                "evidence_bundle": json.loads(evidence_json),
            },
            sort_keys=True,
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]


def _apply_official_harness_invariants(
    analysis: dict[str, Any],
    record: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    if not _official_harness_decisive_labels(record):
        return analysis

    normalized = dict(analysis)
    normalized["llm_original_labels"] = list(analysis.get("labels", []))
    normalized["llm_original_primary_label"] = analysis.get("primary_label")
    normalized["llm_original_subcategories"] = list(analysis.get("subcategories", []))
    normalized["llm_original_failure_stage"] = analysis.get("failure_stage")
    normalized["labels"] = list(fallback["labels"])
    normalized["primary_label"] = fallback["primary_label"]
    normalized["subcategories"] = list(fallback["subcategories"])
    normalized["failure_stage"] = fallback["failure_stage"]
    normalized["evidence"] = list(fallback["evidence"])
    normalized["confidence"] = fallback["confidence"]
    normalized["optimizer_hint"] = fallback["optimizer_hint"]
    normalized["semantic_invariant_applied"] = "official_harness"
    return normalized


def classify_failure(record: dict[str, Any]) -> list[str]:
    """Assign one or more taxonomy labels to a single failed trace.

    Labels are assigned by inspecting the full trace, not just the task
    category. A trace can carry multiple labels (e.g. excessive cost on top of
    a test failure).
    """
    labels = _rule_labels(record)
    return _dedupe(labels)


def _rule_labels(record: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    status = record.get("execution_status")
    category = _record_category(record)
    command_results = record.get("command_results", []) or []
    file_edits = record.get("file_edits", []) or []
    steps = record.get("intermediate_steps", []) or []
    official_labels = _official_harness_labels(record)

    if official_labels:
        labels.extend(official_labels)
    else:
        # 1. Environment / repository preparation failed.
        if status == "environment_failed":
            labels.append("repository_understanding_failure")

        # 2. The agent never localized an edit (no file edits, or steps say "not found").
        localization_missed = any(
            any(marker in str(step) for marker in _LOCALIZATION_MISS_MARKERS)
            for step in steps
        )
        if status not in {"environment_failed", "test_failed"} and (
            not file_edits or localization_missed
        ):
            labels.append("wrong_file_localization")

        # 3. Tests ran but failed -> distinguish compilation vs plain test failure.
        if status == "test_failed":
            if _has_compilation_error(command_results):
                labels.append("compilation_failure")
            else:
                labels.append("test_failure")
            if _regression_introduced(command_results):
                labels.append("regression_introduced")

    # 4. Security task that did not succeed -> vulnerability not fixed.
    if category == "security":
        labels.append("vulnerability_not_fixed")

    # 5. Tool misuse: setup/test commands errored outside the env-failed path.
    if status != "environment_failed" and _has_tool_misuse(command_results):
        labels.append("tool_misuse")

    # 6. Excessive cost (independent, additive).
    if float(record.get("cost", 0.0)) > EXCESSIVE_COST_THRESHOLD:
        labels.append("excessive_cost")

    # Fallback: a failure we could not otherwise characterize.
    if not labels:
        labels.append("wrong_file_localization")

    return labels


def _rule_based_analysis(record: dict[str, Any]) -> dict[str, Any]:
    labels = classify_failure(record)
    primary_label = labels[0]
    subcategories = _rule_subcategories(record, labels)
    failure_stage = _infer_failure_stage(labels, record)
    analysis = {
        "task_id": _record_task_id(record),
        "category": _record_category(record),
        "execution_status": record.get("execution_status"),
        "labels": labels,
        "primary_label": primary_label,
        "subcategories": subcategories,
        "failure_stage": failure_stage,
        "evidence": _rule_evidence(record, labels),
        "confidence": _rule_confidence(record, labels),
        "optimizer_hint": _optimizer_hint(primary_label, subcategories),
        "notes": "",
        "critic_source": "rule",
    }
    return _with_empty_patch_cause(analysis, record)


def _official_harness(record: dict[str, Any]) -> dict[str, Any]:
    official = record.get("official_harness")
    if isinstance(official, dict):
        return official
    harness = record.get("harness")
    if isinstance(harness, dict):
        return harness
    return {}


def _official_harness_labels(record: dict[str, Any]) -> list[str]:
    official = _official_harness(record)
    if not official or bool(record.get("success", False)):
        return []
    if _official_empty_patch(record):
        return ["wrong_file_localization"]

    f2p_failures = _official_fail_to_pass_failures(record)
    p2p_failures = _official_pass_to_pass_failures(record)
    patch_successfully_applied = _official_patch_successfully_applied(record)

    labels: list[str] = []
    if patch_successfully_applied is True:
        if f2p_failures:
            labels.append("test_failure")
        if p2p_failures:
            labels.append("regression_introduced")
        if labels:
            return labels

    if record.get("execution_status") == "test_failed":
        return ["test_failure"]
    if patch_successfully_applied is False or official.get("patch_exists") is False:
        return ["wrong_file_localization"]
    return []


def _official_harness_decisive_labels(record: dict[str, Any]) -> list[str]:
    official = _official_harness(record)
    if not official or bool(record.get("success", False)):
        return []
    if _official_empty_patch(record):
        return ["wrong_file_localization"]
    if _official_patch_successfully_applied(record) is not True:
        return []

    labels = []
    if _official_fail_to_pass_failures(record):
        labels.append("test_failure")
    if _official_pass_to_pass_failures(record):
        labels.append("regression_introduced")
    return labels


def _official_empty_patch(record: dict[str, Any]) -> bool:
    official = _official_harness(record)
    if official.get("empty_patch") is True:
        return True
    if official.get("patch_is_None") is True or official.get("patch_exists") is False:
        return not _record_patch_text(record).strip()
    return False


def _official_patch_successfully_applied(record: dict[str, Any]) -> bool | None:
    official = _official_harness(record)
    value = official.get("patch_successfully_applied")
    return value if isinstance(value, bool) else None


def _official_fail_to_pass_failures(record: dict[str, Any]) -> list[str]:
    return _official_test_bucket(record, "FAIL_TO_PASS", "failure")


def _official_pass_to_pass_failures(record: dict[str, Any]) -> list[str]:
    return _official_test_bucket(record, "PASS_TO_PASS", "failure")


def _official_test_bucket(
    record: dict[str, Any],
    bucket_name: str,
    outcome: str,
) -> list[str]:
    official = _official_harness(record)
    tests_status = official.get("tests_status")
    if not isinstance(tests_status, dict):
        return []
    bucket = tests_status.get(bucket_name)
    if not isinstance(bucket, dict):
        return []
    values = bucket.get(outcome, [])
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def _record_patch_text(record: dict[str, Any]) -> str:
    patch_diff = str(record.get("patch_diff") or "")
    if patch_diff.strip():
        return patch_diff
    return str(record.get("model_patch") or "")


def _validation_guard_evidence(record: dict[str, Any]) -> dict[str, Any]:
    validation = record.get("validation")
    if not isinstance(validation, dict):
        validation = {}
    selected = (
        record.get("selected_pass_to_pass_guards")
        or validation.get("selected_pass_to_pass_guards")
        or []
    )
    reason = (
        record.get("guard_selection_reason")
        or validation.get("guard_selection_reason")
        or ""
    )
    coverage = (
        record.get("guard_coverage_count")
        if record.get("guard_coverage_count") is not None
        else validation.get("guard_coverage_count")
    )
    return {
        "selected_pass_to_pass_guards": _clean_jsonable(selected, 300),
        "guard_selection_reason": _clean_text(reason, 300),
        "guard_coverage_count": coverage,
    }


def _record_task_id(record: dict[str, Any]) -> str:
    task = record.get("task", {}) or {}
    return str(task.get("id", "unknown"))


def _record_category(record: dict[str, Any]) -> str:
    task = record.get("task", {}) or {}
    return str(task.get("category", "software"))


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [item for item in items if item and not (item in seen or seen.add(item))]


def _rule_subcategories(record: dict[str, Any], labels: list[str]) -> list[str]:
    command_results = record.get("command_results", []) or []
    file_edits = record.get("file_edits", []) or []
    steps_text = "\n".join(str(step).lower() for step in record.get("intermediate_steps", []) or [])
    empty_patch_cause = _empty_patch_cause_context(record)
    subcategories: list[str] = []

    if "wrong_file_localization" in labels:
        if _official_empty_patch(record) or (
            not _record_patch_text(record).strip() and not file_edits
        ):
            subcategories.append("empty_output")
            if empty_patch_cause:
                subcategories.append(empty_patch_cause["subcategory"])
        elif not file_edits and not _official_patch_successfully_applied(record):
            subcategories.append("missing_relevant_file")
        if any(marker in steps_text for marker in _LOCALIZATION_MISS_MARKERS):
            subcategories.append("exact_find_not_found")
        if not subcategories:
            subcategories.append("wrong_function_or_class")

    if "repository_understanding_failure" in labels:
        subcategories.append("testbed_setup_failed")

    if "compilation_failure" in labels:
        subcategories.append("patch_syntax_error")

    if "test_failure" in labels:
        if _official_fail_to_pass_failures(record):
            subcategories.append("fail_to_pass_failure")
        elif _has_validation_environment_failure(command_results):
            subcategories.append("validation_environment_failure")
        else:
            subcategories.append("fail_to_pass_failure")

    if "regression_introduced" in labels:
        subcategories.append("pass_to_pass_regression")

    if "vulnerability_not_fixed" in labels:
        subcategories.append("vulnerability_pattern_unaddressed")

    if "tool_misuse" in labels:
        if _command_text(command_results).lower().find("modulenotfounderror") >= 0:
            subcategories.append("dependency_missing")
        else:
            subcategories.append("testbed_setup_failed")

    if "excessive_cost" in labels:
        subcategories.append("excessive_tokens")

    return [
        item
        for item in _dedupe(subcategories)
        if item in FAILURE_SUBCATEGORIES
    ]


def _infer_failure_stage(labels: list[str], record: dict[str, Any]) -> str:
    if _official_harness(record) and (
        "test_failure" in labels or "regression_introduced" in labels
    ):
        return "official_harness"
    empty_patch_cause = _empty_patch_cause_context(record)
    if empty_patch_cause and empty_patch_cause["failure_stage"] in FAILURE_STAGES:
        return empty_patch_cause["failure_stage"]
    if labels == ["excessive_cost"]:
        return "cost"
    if "wrong_file_localization" in labels:
        return "localization"
    if "repository_understanding_failure" in labels:
        return "environment"
    if "tool_misuse" in labels and _has_setup_failure(record.get("command_results", []) or []):
        return "environment"
    if "compilation_failure" in labels or "test_failure" in labels:
        return "validation"
    if "regression_introduced" in labels:
        return "validation"
    if "vulnerability_not_fixed" in labels:
        return "validation"
    return "generation"


def _rule_evidence(record: dict[str, Any], labels: list[str]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    command_results = record.get("command_results", []) or []
    steps = record.get("intermediate_steps", []) or []
    empty_patch_cause = _empty_patch_cause_context(record)

    if "wrong_file_localization" in labels:
        if _official_empty_patch(record) or not _record_patch_text(record).strip():
            evidence.append(
                _evidence_item(
                    "prediction.model_patch_chars",
                    "No model patch text was available for the failed task.",
                    str(len(_record_patch_text(record))),
                )
            )
            if empty_patch_cause:
                evidence.append(_empty_patch_cause_evidence(empty_patch_cause))
        elif not record.get("file_edits") and not _official_patch_successfully_applied(record):
            evidence.append(
                _evidence_item(
                    "file_edits",
                    "No file edits were recorded for a failed task.",
                    "[]",
                )
            )
        for index, step in enumerate(steps):
            if any(marker in str(step) for marker in _LOCALIZATION_MISS_MARKERS):
                evidence.append(
                    _evidence_item(
                        f"intermediate_steps[{index}]",
                        "The trace reports a localization or materialization miss.",
                        str(step),
                    )
                )
                break

    if "repository_understanding_failure" in labels:
        workspace_error = (record.get("workspace") or {}).get("error")
        evidence.append(
            _evidence_item(
                "workspace.error",
                "Workspace preparation or repository setup failed.",
                str(workspace_error or record.get("execution_status", "")),
            )
        )

    if "compilation_failure" in labels:
        result, index = _first_failing_command(command_results, phase="test")
        evidence.append(
            _evidence_item(
                f"command_results[{index}].stderr",
                "A test command failed with an import or syntax-like error.",
                _command_output(result),
            )
        )

    if "test_failure" in labels:
        f2p_failures = _official_fail_to_pass_failures(record)
        if f2p_failures:
            evidence.append(
                _evidence_item(
                    "official_harness.tests_status.FAIL_TO_PASS.failure",
                    "Official harness FAIL_TO_PASS tests still failed.",
                    json.dumps(f2p_failures[:12], sort_keys=True),
                )
            )
        else:
            result, index = _first_failing_command(command_results, phase="test")
            evidence.append(
                _evidence_item(
                    f"command_results[{index}]",
                    "A validation or test command failed after the patch.",
                    _command_output(result),
                )
            )

    if "regression_introduced" in labels:
        p2p_failures = _official_pass_to_pass_failures(record)
        if p2p_failures:
            evidence.append(
                _evidence_item(
                    "official_harness.patch_successfully_applied",
                    "Official harness successfully applied the patch before tests.",
                    str(_official_patch_successfully_applied(record)),
                )
            )
            evidence.append(
                _evidence_item(
                    "official_harness.tests_status.PASS_TO_PASS.failure",
                    "Official harness PASS_TO_PASS tests failed after the patch.",
                    json.dumps(p2p_failures[:12], sort_keys=True),
                )
            )
        else:
            result, index = _first_failing_command(command_results, phase="test")
            evidence.append(
                _evidence_item(
                    f"command_results[{index}].command",
                    "A regression or PASS_TO_PASS command failed.",
                    str(result.get("command", "")),
                )
            )

    if "vulnerability_not_fixed" in labels:
        evidence.append(
            _evidence_item(
                "task.category",
                "Security task remained unresolved.",
                _record_category(record),
            )
        )

    if "tool_misuse" in labels:
        result, index = _first_failing_command(command_results, phases={"setup", "test_patch"})
        evidence.append(
            _evidence_item(
                f"command_results[{index}]",
                "A setup or test-patch tool command failed.",
                _command_output(result),
            )
        )

    if "excessive_cost" in labels:
        evidence.append(
            _evidence_item(
                "cost",
                "Trace cost exceeded the critic threshold.",
                str(record.get("cost", 0.0)),
            )
        )

    if not evidence:
        evidence.append(
            _evidence_item(
                "execution_status",
                "The trace failed without a more specific rule match.",
                str(record.get("execution_status", "")),
            )
        )

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        key = (item["source"], item["summary"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped[:6]


def _rule_confidence(record: dict[str, Any], labels: list[str]) -> float:
    if _official_harness(record) and (
        "regression_introduced" in labels or "test_failure" in labels
    ):
        return 0.9
    if "compilation_failure" in labels or "regression_introduced" in labels:
        return 0.86
    if "test_failure" in labels and record.get("command_results"):
        return 0.78
    if "wrong_file_localization" in labels and not record.get("file_edits"):
        return 0.74
    if "repository_understanding_failure" in labels:
        return 0.72
    return 0.62


def _optimizer_hint(primary_label: str, subcategories: list[str]) -> str:
    subcategory_set = set(subcategories)
    if "structured_edit_anchor_mismatch" in subcategory_set:
        return (
            "When structured-edit anchors fail, broaden localization to nearby "
            "stable symbols and generate compact replacements instead of ending "
            "with no patch."
        )
    if "length_exhausted" in subcategory_set:
        return (
            "Constrain patch generation to the smallest one- or two-file edit "
            "before long reasoning can exhaust output and produce an empty patch."
        )
    if "structured_edit_parse_failed" in subcategory_set:
        return "Require parseable structured edits or a minimal unified diff fallback."
    if "git_apply_failure" in subcategory_set:
        return "Keep generated diffs applyable with valid file headers and minimal hunks."
    if "no_patch_intent" in subcategory_set:
        return (
            "Do not decline edits merely because the first location is uncertain; "
            "continue issue-linked localization before deciding no code change is needed."
        )
    if "validation_rejected" in subcategory_set:
        return "Use validation rejection as a bounded repair signal before finalizing no patch."
    if subcategory_set & {"api_error", "unknown_empty_patch"}:
        return "Do not optimize generic empty-patch symptoms without a concrete cause."
    if primary_label == "wrong_file_localization":
        return "Strengthen localization to cite issue-linked files and exact symbols before patch generation."
    if primary_label == "repository_understanding_failure":
        return "Add repository setup evidence to the planning prompt and reject candidates when the testbed is broken."
    if primary_label == "compilation_failure":
        return "Add syntax/import self-checks before finalizing generated edits."
    if primary_label == "test_failure":
        if "validation_patch_failure" in subcategories:
            return "Feed focused validation failures back into one bounded patch repair step."
        return "Strengthen verification around fail-to-pass tests and preserve the observed failing assertion."
    if primary_label == "regression_introduced":
        return "Require PASS_TO_PASS or nearby regression guards before accepting a focused fix."
    if primary_label == "vulnerability_not_fixed":
        return "Route security tasks through the security_review prompt and require vulnerability-specific evidence."
    if primary_label == "tool_misuse":
        return "Separate tool/environment failures from patch failures before optimizing prompts."
    if primary_label == "excessive_cost":
        return "Lower retry breadth or evidence size when the same failure pattern repeats."
    return "Inspect the trace evidence before applying a prompt update."


def _with_empty_patch_cause(
    analysis: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    context = _empty_patch_cause_context(record)
    if not context:
        return analysis

    enriched = dict(analysis)
    subcategories = _dedupe(
        [
            *[str(item) for item in enriched.get("subcategories", []) or []],
            "empty_output",
            context["subcategory"],
        ]
    )
    enriched["subcategories"] = [
        item for item in subcategories if item in FAILURE_SUBCATEGORIES
    ]
    if context["failure_stage"] in FAILURE_STAGES:
        enriched["failure_stage"] = context["failure_stage"]
    enriched["empty_patch"] = True
    enriched["empty_patch_cause"] = context["cause"]
    enriched["empty_patch_cause_confidence"] = context["confidence"]
    enriched["empty_patch_actionability"] = context["actionability"]
    enriched["empty_patch_optimizer_targets"] = list(context["optimizer_targets"])
    return enriched


def _empty_patch_cause_context(record: dict[str, Any]) -> dict[str, Any]:
    try:
        event = build_failure_cause_event(record)
    except Exception:
        return {}
    if event.get("failure_type") != "empty_patch":
        return {}
    cause = str(event.get("empty_patch_reason") or "").strip()
    if not cause or cause == "not_empty_patch":
        return {}
    subcategory = _EMPTY_PATCH_CAUSE_TO_SUBCATEGORY.get(
        cause,
        "unknown_empty_patch",
    )
    actionability = (
        "prompt_learnable"
        if cause in _EMPTY_PATCH_PROMPT_LEARNABLE_CAUSES
        else "non_actionable"
    )
    return {
        "cause": cause,
        "subcategory": subcategory,
        "failure_stage": _EMPTY_PATCH_CAUSE_STAGE.get(cause, "localization"),
        "confidence": _coerce_event_confidence(event.get("confidence")),
        "actionability": actionability,
        "optimizer_targets": _EMPTY_PATCH_CAUSE_TARGETS.get(cause, []),
        "event": event,
    }


def _empty_patch_cause_evidence(context: dict[str, Any]) -> dict[str, str]:
    event = context.get("event", {}) if isinstance(context.get("event"), dict) else {}
    envelopes = event.get("evidence_envelopes") if isinstance(event, dict) else {}
    apply_failure = (
        envelopes.get("apply_failure")
        if isinstance(envelopes, dict)
        else None
    )
    if isinstance(apply_failure, dict) and apply_failure.get("type"):
        return _evidence_item(
            "apply_failure.type",
            f"Empty patch cause classified as {context['cause']}.",
            str(apply_failure.get("type") or ""),
        )
    return _evidence_item(
        "failure_cause.empty_patch_reason",
        f"Empty patch cause classified as {context['cause']}.",
        str(context.get("cause") or ""),
    )


def _coerce_event_confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(parsed, 1.0))


def _build_evidence_json(record: dict[str, Any], max_chars: int) -> str:
    bundle = _build_evidence_bundle(record, text_limit=_MAX_TEXT_FIELD_CHARS)
    text = json.dumps(bundle, sort_keys=True)
    if len(text) <= max_chars:
        return text

    compact = _build_evidence_bundle(
        record,
        text_limit=max(160, max_chars // 30),
        include_prompts=False,
        include_llm_calls=False,
    )
    compact["truncation"] = {
        "max_evidence_chars": max_chars,
        "strategy": "field-level",
    }
    text = json.dumps(compact, sort_keys=True)
    if len(text) <= max_chars:
        return text

    minimal = {
        "task": compact["task"],
        "execution": compact["execution"],
        "commands": [
            {
                "phase": item.get("phase"),
                "command": item.get("command"),
                "exit_code": item.get("exit_code"),
                "timed_out": item.get("timed_out"),
                "failure_type": item.get("failure_type"),
                "stderr": _clean_text(item.get("stderr", ""), 120),
            }
            for item in compact["commands"][:6]
        ],
        "patch": {"file_edits": compact["patch"]["file_edits"][:6]},
        "truncation": {
            "max_evidence_chars": max_chars,
            "strategy": "minimal-valid-json",
        },
    }
    return json.dumps(minimal, sort_keys=True)


def _build_evidence_bundle(
    record: dict[str, Any],
    *,
    text_limit: int,
    include_prompts: bool = True,
    include_llm_calls: bool = True,
) -> dict[str, Any]:
    task = record.get("task", {}) or {}
    workspace = record.get("workspace", {}) or {}
    patch_text = _record_patch_text(record)
    official_harness = _official_harness(record)
    bundle: dict[str, Any] = {
        "task": {
            "id": _record_task_id(record),
            "benchmark": _clean_text(task.get("benchmark", ""), 200),
            "split": _clean_text(task.get("split", ""), 80),
            "category": _record_category(record),
            "prompt": _clean_text(task.get("prompt", ""), text_limit),
            "issue_description": _clean_text(task.get("issue_description", ""), text_limit),
            "test_commands": [_clean_text(command, 300) for command in task.get("test_commands", [])],
            "metadata": _clean_jsonable(task.get("metadata", {}), 500),
        },
        "execution": {
            "success": bool(record.get("success", False)),
            "execution_status": _clean_text(record.get("execution_status", ""), 80),
            "cost": record.get("cost", 0.0),
            "latency_ms": record.get("latency_ms", 0),
        },
        "workspace": {
            "error": _clean_text(workspace.get("error", ""), text_limit),
            "repo_url": _clean_text(workspace.get("repo_url", ""), 300),
            "base_commit": _clean_text(workspace.get("base_commit", ""), 120),
        },
        "patch": {
            "diff": _clean_text(patch_text, text_limit),
            "patch_text_chars": len(patch_text),
            "patch_text_present": bool(patch_text.strip()),
            "patch_diff_chars": len(str(record.get("patch_diff") or "")),
            "model_patch_chars": len(str(record.get("model_patch") or "")),
            "file_edits_present": bool(record.get("file_edits")),
            "file_edits_evidence_missing": bool(
                (record.get("evidence_missing") or {}).get("file_edits")
            ),
            "file_edits": [
                _clean_jsonable(edit, 500)
                for edit in (record.get("file_edits", []) or [])[:12]
            ],
            "intermediate_steps": [
                _clean_text(step, 300)
                for step in (record.get("intermediate_steps", []) or [])[:20]
            ],
        },
        "commands": [
            _command_evidence(result, index=index, text_limit=text_limit)
            for index, result in enumerate((record.get("command_results", []) or [])[:16])
        ],
        "official_harness": _clean_jsonable(official_harness, text_limit),
        "validation_guards": _validation_guard_evidence(record),
        "tests": [
            _clean_jsonable(result, 500)
            for result in (record.get("test_results", []) or [])[:16]
        ],
        "retrieved_context": [
            {
                "path": _clean_text(item.get("path", ""), 300),
                "kind": _clean_text(item.get("kind", ""), 120),
                "score": item.get("score"),
                "content": _clean_text(item.get("content", ""), text_limit),
            }
            for item in (record.get("retrieved_context", []) or [])[:8]
            if isinstance(item, dict)
        ],
    }
    if include_prompts:
        bundle["prompts"] = {
            str(key): _clean_text(value, text_limit)
            for key, value in (record.get("prompts", {}) or {}).items()
        }
    if include_llm_calls:
        bundle["llm_calls"] = [
            _llm_call_evidence(call, text_limit=text_limit)
            for call in (record.get("llm_calls", []) or [])[:8]
            if isinstance(call, dict)
        ]
    return bundle


def _command_evidence(
    result: dict[str, Any],
    *,
    index: int,
    text_limit: int,
) -> dict[str, Any]:
    return {
        "index": index,
        "phase": _clean_text(result.get("phase", ""), 80),
        "command": _clean_text(result.get("command", ""), 400),
        "exit_code": result.get("exit_code"),
        "timed_out": bool(result.get("timed_out", False)),
        "timeout_s": result.get("timeout_s"),
        "failure_type": _clean_text(result.get("failure_type", ""), 120),
        "failure_summary": _clean_text(result.get("failure_summary", ""), text_limit),
        "validation_status": _clean_text(result.get("validation_status", ""), 120),
        "selected_pass_to_pass_guards": _clean_jsonable(
            result.get("selected_pass_to_pass_guards", []),
            300,
        ),
        "guard_selection_reason": _clean_text(
            result.get("guard_selection_reason", ""),
            300,
        ),
        "guard_coverage_count": result.get("guard_coverage_count"),
        "stdout": _clean_text(result.get("stdout", ""), text_limit),
        "stderr": _clean_text(result.get("stderr", ""), text_limit),
    }


def _llm_call_evidence(call: dict[str, Any], *, text_limit: int) -> dict[str, Any]:
    allowed_keys = (
        "attempt",
        "finish_reason",
        "failure_type",
        "diff_extracted",
        "structured_edit_count",
        "structured_edit_apply_error",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost_usd",
        "latency_ms",
        "content_preview",
    )
    evidence: dict[str, Any] = {}
    for key in allowed_keys:
        if key not in call:
            continue
        value = call[key]
        evidence[key] = _clean_text(value, text_limit) if isinstance(value, str) else value
    return evidence


def _clean_jsonable(value: Any, text_limit: int) -> Any:
    if isinstance(value, str):
        return _clean_text(value, text_limit)
    if isinstance(value, dict):
        return {
            _clean_text(key, 160): _clean_jsonable(item, text_limit)
            for key, item in value.items()
            if not _looks_secret_key(str(key))
        }
    if isinstance(value, list):
        return [_clean_jsonable(item, text_limit) for item in value[:20]]
    if isinstance(value, tuple):
        return [_clean_jsonable(item, text_limit) for item in value[:20]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean_text(value, text_limit)


def _clean_text(value: Any, max_chars: int) -> str:
    text = _redact_secrets(str(value or ""))
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}...[truncated {omitted} chars]"


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in ("api_key", "apikey", "authorization", "password", "secret", "token"))


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("critic response did not contain a JSON object")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"critic response JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("critic response must be a JSON object")
    return payload


def _validate_llm_analysis(
    payload: dict[str, Any],
    record: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    labels = _normalize_string_list(payload.get("labels"))
    if not labels:
        raise ValueError("critic response missing labels")
    unknown_labels = [label for label in labels if label not in FAILURE_TAXONOMY]
    if unknown_labels:
        raise ValueError(f"critic response used unknown labels: {unknown_labels}")

    primary_label = str(payload.get("primary_label") or labels[0])
    if primary_label not in labels:
        raise ValueError("primary_label must be one of labels")

    subcategories = _normalize_string_list(payload.get("subcategories"))
    unknown_subcategories = [
        item for item in subcategories if item not in FAILURE_SUBCATEGORIES
    ]
    if unknown_subcategories:
        raise ValueError(
            f"critic response used unknown subcategories: {unknown_subcategories}"
        )

    failure_stage = str(
        payload.get("failure_stage") or _infer_failure_stage(labels, record)
    )
    if failure_stage not in FAILURE_STAGES:
        raise ValueError(f"critic response used unknown failure_stage: {failure_stage}")

    confidence = _coerce_confidence(payload.get("confidence"))
    evidence = _normalize_evidence(payload.get("evidence"))
    if not evidence:
        evidence = list(fallback.get("evidence", []))[:1]

    return {
        "task_id": _record_task_id(record),
        "category": _record_category(record),
        "execution_status": record.get("execution_status"),
        "labels": labels,
        "primary_label": primary_label,
        "subcategories": subcategories,
        "failure_stage": failure_stage,
        "evidence": evidence,
        "confidence": confidence,
        "optimizer_hint": _clean_text(payload.get("optimizer_hint", ""), 600),
        "notes": _clean_text(payload.get("notes", ""), 600),
    }


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [str(item) for item in value if item]
    else:
        raise ValueError("expected a string list")
    return _dedupe([item.strip() for item in items if item.strip()])


def _normalize_evidence(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("evidence must be a list")
    evidence: list[dict[str, str]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            raise ValueError("each evidence item must be an object")
        source = _clean_text(item.get("source") or item.get("field") or "evidence", 240)
        summary = _clean_text(item.get("summary") or item.get("reason") or "", 600)
        quote = _clean_text(item.get("quote", ""), 600)
        if not summary and not quote:
            continue
        evidence.append({"source": source, "summary": summary, "quote": quote})
    return evidence


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        raise ValueError("confidence must be numeric") from None
    return min(1.0, max(0.0, confidence))


def _llm_fallback(
    fallback: dict[str, Any],
    *,
    reason: str,
    error: str,
    response: Any | None = None,
) -> dict[str, Any]:
    analysis = dict(fallback)
    analysis["critic_source"] = "rule_fallback"
    analysis["fallback_reason"] = reason
    analysis["llm_error"] = _clean_text(error, 600)
    if response is not None:
        analysis["critic_model"] = getattr(response, "model", "")
        analysis["critic_usage"] = _usage_to_dict(getattr(response, "usage", None))
        analysis["llm_content_preview"] = _clean_text(getattr(response, "content", ""), 600)
    return analysis


def _usage_to_dict(usage: Any | None) -> dict[str, Any]:
    if usage is None:
        return {}
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        "cost_usd": float(getattr(usage, "cost_usd", 0.0) or 0.0),
    }


def _evidence_item(source: str, summary: str, quote: str) -> dict[str, str]:
    return {
        "source": source,
        "summary": summary,
        "quote": _clean_text(quote, 600),
    }


def _first_failing_command(
    command_results: list[dict[str, Any]],
    *,
    phase: str | None = None,
    phases: set[str] | None = None,
) -> tuple[dict[str, Any], int]:
    for index, result in enumerate(command_results):
        result_phase = result.get("phase")
        if phase is not None and result_phase != phase:
            continue
        if phases is not None and result_phase not in phases:
            continue
        failed = result.get("timed_out") or result.get("exit_code") not in (0, None)
        if failed:
            return result, index
    return {}, -1


def _command_output(result: dict[str, Any]) -> str:
    fields = (
        result.get("failure_summary"),
        result.get("stderr"),
        result.get("stdout"),
        result.get("command"),
    )
    for field in fields:
        if field:
            return str(field)
    return ""


def _command_text(command_results: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(result.get(field, ""))
        for result in command_results
        for field in ("command", "stdout", "stderr", "failure_summary", "failure_type")
    )


def _has_setup_failure(command_results: list[dict[str, Any]]) -> bool:
    for result in command_results:
        if result.get("phase") != "setup":
            continue
        if result.get("timed_out") or result.get("exit_code") not in (0, None):
            return True
    return False


def _has_validation_environment_failure(command_results: list[dict[str, Any]]) -> bool:
    for result in command_results:
        failure_type = str(result.get("failure_type", "")).lower()
        if "environment" in failure_type:
            return True
    return False


def _has_compilation_error(command_results: list[dict[str, Any]]) -> bool:
    for result in command_results:
        if result.get("phase") != "test":
            continue
        stderr = str(result.get("stderr", "")).lower()
        if any(marker in stderr for marker in _COMPILATION_ERROR_MARKERS):
            return True
    return False


def _regression_introduced(command_results: list[dict[str, Any]]) -> bool:
    """Heuristic: a previously-passing test command now fails after the edit.

    In the mock we approximate this with a command tagged as a regression/
    pass-to-pass check that failed. Real PASS_TO_PASS regression detection is
    wired in M2 via the official harness report.
    """
    for result in command_results:
        if result.get("phase") != "test":
            continue
        command = str(result.get("command", "")).lower()
        failed = result.get("timed_out") or result.get("exit_code") not in (0, None)
        if failed and ("pass_to_pass" in command or "regression" in command):
            return True
    return False


def _has_tool_misuse(command_results: list[dict[str, Any]]) -> bool:
    for result in command_results:
        if result.get("phase") not in {"setup", "test_patch"}:
            continue
        if result.get("timed_out") or result.get("exit_code") not in (0, None):
            return True
    return False


def _recommend(
    label_counts: dict[str, int],
    failed_categories: dict[str, int],
) -> list[str]:
    """Map taxonomy counts to optimizer-facing recommendations.

    Kept backward compatible with the optimizer, which consumes
    ``enable_security_review`` / ``verify_before_submit`` /
    ``tighten_task_prompt``.
    """
    recommendations: list[str] = []

    if label_counts.get("vulnerability_not_fixed") or failed_categories.get("security"):
        recommendations.append("enable_security_review")
        recommendations.append("verify_before_submit")

    if label_counts.get("test_failure") or label_counts.get("regression_introduced"):
        if "verify_before_submit" not in recommendations:
            recommendations.append("verify_before_submit")

    if (
        label_counts.get("wrong_file_localization")
        or label_counts.get("repository_understanding_failure")
    ) and not recommendations:
        recommendations.append("tighten_task_prompt")

    if any(label_counts.values()) and not recommendations:
        recommendations.append("tighten_task_prompt")

    return recommendations


def write_diagnosis(diagnosis: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(diagnosis, indent=2, sort_keys=True), encoding="utf-8")


def _summary(
    label_counts: dict[str, int],
    failed_categories: dict[str, int],
) -> str:
    active = {label: count for label, count in label_counts.items() if count}
    if not active:
        return "No failures found in the analyzed traces."
    top = max(active, key=active.get)
    readable = top.replace("_", " ")
    return f"Dominant failure mode: {readable} ({active[top]} case(s)); {len(active)} failure type(s) seen."
