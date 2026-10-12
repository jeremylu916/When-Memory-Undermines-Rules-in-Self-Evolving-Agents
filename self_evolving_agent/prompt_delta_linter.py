"""Deterministic quality linter for v2 prompt-update proposals."""

from __future__ import annotations

import re
from typing import Any

from self_evolving_agent.prompt_update_contract import (
    PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION,
    PromptUpdateContractError,
    legacy_update_to_prompt_update_proposal,
    normalize_prompt_update_proposal,
)


PROMPT_DELTA_LINTER_SCHEMA_VERSION = "prompt_delta_linter.v1"
MAX_COMPILED_PROMPT_DELTA_CHARS = 600

_GENERIC_PHRASES = (
    "verify the change",
    "verify thoroughly",
    "test thoroughly",
    "run tests",
    "make sure",
    "be careful",
    "best practices",
    "robust",
    "ensure the patch",
    "avoid empty patch",
    "avoid empty patches",
    "check your work",
    "before submitting the patch",
    "before accepting a patch",
)
_CONCRETE_OBJECT_HINTS = (
    "assertion",
    "anchor",
    "symbol",
    "function",
    "class",
    "import",
    "call site",
    "traceback",
    "exception",
    "column",
    "query",
    "schema",
    "materialization",
    "fail-to-pass",
    "pass-to-pass",
    "exact",
    "observed",
)
_ANCHOR_MISMATCH_MARKERS = (
    "anchor mismatch",
    "anchor mismatches",
    "structured-edit anchor",
    "structured edit anchor",
    "exact structured-edit anchors",
    "exact structured edit anchors",
)
_EXECUTION_REQUIRED_PATTERNS = (
    re.compile(r"\brun\b[^.\n]{0,48}\btests?\b", re.I),
    re.compile(r"\bexecute\b[^.\n]{0,48}\btests?\b", re.I),
    re.compile(r"\bre-?run\b[^.\n]{0,48}\btests?\b", re.I),
    re.compile(r"\binvoke\s+(?:pytest|tox|unittest|test suite)\b", re.I),
)
_POST_GENERATION_APPLICABILITY_PATTERNS = (
    re.compile(r"\bstructured[- ]edit\b[^.\n]{0,64}\b(?:fail|fails|failed|failure|mismatch)\b", re.I),
    re.compile(r"\b(?:anchor mismatch|materialization (?:fail|fails|failed|failure))\b", re.I),
    re.compile(r"\b(?:generated|resulting|candidate) patch\b[^.\n]{0,64}\b(?:fail|fails|failed|empty|missing)\b", re.I),
    re.compile(r"\bno patch (?:is|was|has been )?generated\b", re.I),
    re.compile(r"\b(?:official )?harness (?:evidence|result|results|failure|failures)\b", re.I),
    re.compile(r"\b(?:fail[- ]to[- ]pass|pass[- ]to[- ]pass)\b[^.\n]{0,64}\b(?:fail|fails|failed|failure|regress|regression|unchanged)\b", re.I),
)


def lint_prompt_update_proposal(
    proposal: dict[str, Any],
    *,
    existing_updates: list[dict[str, Any]] | None = None,
    diagnosis: dict[str, Any] | None = None,
    solver_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lint a v2 proposal and return an auditable decision."""

    findings: list[dict[str, str]] = []
    try:
        normalized = normalize_prompt_update_proposal(proposal)
    except PromptUpdateContractError as exc:
        return _lint_result(
            proposal,
            accepted=False,
            findings=[
                _finding("error", "contract_violation", str(exc)),
            ],
        )

    operation = normalized["operation"]
    delta_text = _operation_delta_text(normalized)
    expected = normalized["expected_behavior_change"]
    applicability = normalized["applicability_condition"]
    acceptance_test = normalized["acceptance_test"]

    if not _is_condition_specific(applicability):
        findings.append(
            _finding(
                "error",
                "missing_specific_applicability_condition",
                "Applicability condition must be conditional and evidence-bound.",
            )
        )

    if normalized.get("_legacy_missing_acceptance_test") or not _acceptance_test_targets_delta(
        acceptance_test,
        delta_text,
        operation,
    ):
        findings.append(
            _finding(
                "error",
                "weak_acceptance_test",
                "Acceptance test must assert concrete prompt behavior.",
            )
        )
    fixture_alignment_failures = _acceptance_test_alignment_failures(
        acceptance_test,
        delta_text,
        operation,
    )
    if fixture_alignment_failures:
        findings.append(
            _finding(
                "error",
                "acceptance_test_not_in_delta",
                "Acceptance test should_contain strings must appear in the proposed prompt delta: "
                + ", ".join(fixture_alignment_failures[:3]),
            )
        )

    if operation == "add_append" and _looks_generic(delta_text, expected):
        findings.append(
            _finding(
                "error",
                "generic_prompt_delta",
                "Delta reads like process prose rather than a task-solving policy.",
            )
        )

    if _requires_unavailable_execution(delta_text, solver_capabilities or {}):
        findings.append(
            _finding(
                "error",
                "non_executable_instruction",
                "Prompt delta requires test execution, but solver-side validation is disabled.",
            )
        )

    if operation in {"add_append", "replace_existing_update"} and _requires_unavailable_runtime_evidence(
        applicability,
        solver_capabilities or {},
    ):
        findings.append(
            _finding(
                "error",
                "unobservable_applicability_condition",
                "Applicability depends on patch, materialization, or harness results "
                "that are unavailable when the solver prompt is rendered.",
            )
        )

    if operation in {"add_append", "replace_existing_update"} and len(
        _compiled_delta_text(applicability, delta_text)
    ) > MAX_COMPILED_PROMPT_DELTA_CHARS:
        findings.append(
            _finding(
                "error",
                "compiled_prompt_delta_too_long",
                "Applicability condition plus instruction exceeds the candidate builder's 600-character limit.",
            )
        )

    duplicate_id = _duplicate_update_id(normalized, existing_updates or [])
    if duplicate_id:
        findings.append(
            _finding(
                "error",
                "duplicate_prompt_delta",
                f"Delta duplicates existing update {duplicate_id}.",
            )
        )

    if _unscoped_anchor_mismatch_rule(normalized, diagnosis or {}):
        findings.append(
            _finding(
                "error",
                "unscoped_anchor_mismatch_rule",
                "Anchor-mismatch guidance requires matching failure evidence.",
            )
        )

    if operation == "add_append" and existing_updates:
        active_same_module = [
            item
            for item in existing_updates
            if str(item.get("target_module") or "") == normalized["target_module"]
            and str(item.get("status") or "active") == "active"
        ]
        if active_same_module:
            findings.append(
                _finding(
                    "warning",
                    "append_when_existing_update_available",
                    "Consider replace/suppress/preserve before appending another block.",
                )
            )

    return _lint_result(
        normalized,
        accepted=not any(item["severity"] == "error" for item in findings),
        findings=findings,
    )


def lint_prompt_update_proposals(
    proposals: list[dict[str, Any]],
    *,
    existing_updates: list[dict[str, Any]] | None = None,
    diagnosis: dict[str, Any] | None = None,
    solver_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lint a batch and include aggregate quality metrics."""

    seen: list[dict[str, Any]] = list(existing_updates or [])
    results = []
    for proposal in proposals:
        result = lint_prompt_update_proposal(
            proposal,
            existing_updates=seen,
            diagnosis=diagnosis,
            solver_capabilities=solver_capabilities,
        )
        results.append(result)
        if result["accepted"] and isinstance(result.get("proposal"), dict):
            seen.append(_proposal_as_existing_update(result["proposal"]))
    return {
        "schema_version": PROMPT_DELTA_LINTER_SCHEMA_VERSION,
        "proposal_count": len(proposals),
        "accepted_count": sum(1 for item in results if item["accepted"]),
        "rejected_count": sum(1 for item in results if not item["accepted"]),
        "metrics": prompt_update_quality_metrics(results),
        "results": results,
    }


def lint_legacy_prompt_updates(
    updates: list[dict[str, Any]],
    *,
    existing_updates: list[dict[str, Any]] | None = None,
    diagnosis: dict[str, Any] | None = None,
    solver_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit legacy structured updates through the v2 quality lens."""

    proposals = [
        legacy_update_to_prompt_update_proposal(update)
        for update in updates
        if isinstance(update, dict)
    ]
    return lint_prompt_update_proposals(
        proposals,
        existing_updates=existing_updates,
        diagnosis=diagnosis,
        solver_capabilities=solver_capabilities,
    )


def prompt_update_quality_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {
            "generic_update_rate": 0.0,
            "duplicate_update_rate": 0.0,
            "append_only_rate": 0.0,
            "missing_applicability_condition_rate": 0.0,
            "missing_acceptance_test_rate": 0.0,
            "accepted_update_rate": 0.0,
            "non_executable_instruction_rate": 0.0,
            "unobservable_applicability_condition_rate": 0.0,
            "compiled_prompt_delta_too_long_rate": 0.0,
            "operation_distribution": {},
        }
    operation_counts: dict[str, int] = {}
    for result in results:
        proposal = result.get("proposal") if isinstance(result.get("proposal"), dict) else {}
        operation = str(proposal.get("operation") or "invalid")
        operation_counts[operation] = operation_counts.get(operation, 0) + 1

    def rate(rule_ids: set[str]) -> float:
        count = 0
        for result in results:
            ids = {
                str(finding.get("rule_id") or "")
                for finding in result.get("findings", [])
                if isinstance(finding, dict)
            }
            if ids & rule_ids:
                count += 1
        return count / total

    return {
        "generic_update_rate": rate({"generic_prompt_delta"}),
        "duplicate_update_rate": rate({"duplicate_prompt_delta"}),
        "append_only_rate": operation_counts.get("add_append", 0) / total,
        "missing_applicability_condition_rate": rate(
            {
                "missing_specific_applicability_condition",
                "contract_violation",
            }
        ),
        "missing_acceptance_test_rate": rate(
            {
                "weak_acceptance_test",
                "acceptance_test_not_in_delta",
                "contract_violation",
            }
        ),
        "accepted_update_rate": sum(1 for item in results if item["accepted"]) / total,
        "non_executable_instruction_rate": rate({"non_executable_instruction"}),
        "unobservable_applicability_condition_rate": rate(
            {"unobservable_applicability_condition"}
        ),
        "compiled_prompt_delta_too_long_rate": rate(
            {"compiled_prompt_delta_too_long"}
        ),
        "operation_distribution": dict(sorted(operation_counts.items())),
    }


def _requires_unavailable_execution(
    text: str,
    solver_capabilities: dict[str, Any],
) -> bool:
    if bool(solver_capabilities.get("validation_enabled", False)):
        return False
    return any(pattern.search(text) for pattern in _EXECUTION_REQUIRED_PATTERNS)


def _requires_unavailable_runtime_evidence(
    applicability: str,
    solver_capabilities: dict[str, Any],
) -> bool:
    visible = {
        str(item).strip()
        for item in solver_capabilities.get("prompt_visible_evidence", [])
        if str(item).strip()
    }
    if "post_generation_results" in visible:
        return False
    return any(
        pattern.search(applicability)
        for pattern in _POST_GENERATION_APPLICABILITY_PATTERNS
    )


def _compiled_delta_text(applicability: str, delta_text: str) -> str:
    return (
        f"Applicability condition: {applicability.strip()}\n"
        f"Instruction: {delta_text.strip()}"
    )


def _lint_result(
    proposal: dict[str, Any],
    *,
    accepted: bool,
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": PROMPT_DELTA_LINTER_SCHEMA_VERSION,
        "proposal_schema_version": str(
            proposal.get("schema_version") or PROMPT_UPDATE_PROPOSAL_SCHEMA_VERSION
        )
        if isinstance(proposal, dict)
        else "",
        "accepted": accepted,
        "proposal": proposal,
        "findings": findings,
    }


def _finding(severity: str, rule_id: str, message: str) -> dict[str, str]:
    return {
        "severity": severity,
        "rule_id": rule_id,
        "message": message,
    }


def _operation_delta_text(proposal: dict[str, Any]) -> str:
    if proposal["operation"] == "replace_existing_update":
        return str(proposal.get("replacement_delta") or "")
    return str(proposal.get("instruction_delta") or "")


def _is_condition_specific(text: str) -> bool:
    lowered = text.lower()
    if len(lowered.split()) < 6:
        return False
    condition_marker = re.search(
        r"\b(only when|only if|when|if|for tasks where|given)\b",
        lowered,
    )
    evidence_marker = re.search(
        r"\b("
        r"evidence|failure|failures|fail|fails|failing|observed|diagnos|"
        r"trace|harness|pattern|materialization|anchor|structured[- ]edit|"
        r"fail[-_ ]to[-_ ]pass|pass[-_ ]to[-_ ]pass|repository|context|symbol|"
        r"definition|file|function|class|failing[_ ]test[_ ]names|test names?"
        r")\b",
        lowered,
    )
    return bool(
        condition_marker
        and evidence_marker
    )


def _acceptance_test_targets_delta(
    acceptance_test: dict[str, Any],
    delta_text: str,
    operation: str,
) -> bool:
    contains = acceptance_test.get("should_contain") or []
    not_contains = acceptance_test.get("should_not_contain") or []
    if operation == "suppress_existing_update":
        return bool(not_contains)
    if not contains:
        return False
    delta_norm = _normalize_text(delta_text)
    if not delta_norm:
        return False
    for item in contains:
        item_norm = _normalize_text(str(item))
        if item_norm and (item_norm in delta_norm or delta_norm in item_norm):
            return True
    return any(_has_concrete_hint(str(item)) for item in contains)


def _acceptance_test_alignment_failures(
    acceptance_test: dict[str, Any],
    delta_text: str,
    operation: str,
) -> list[str]:
    if operation == "suppress_existing_update":
        return []
    contains = [
        str(item).strip()
        for item in acceptance_test.get("should_contain", [])
        if str(item).strip()
    ]
    if not contains:
        return []
    delta_norm = _normalize_text(delta_text)
    failures = []
    for item in contains:
        if _normalize_text(item) not in delta_norm:
            failures.append(item)
    return failures


def _looks_generic(delta_text: str, expected: str) -> bool:
    text = _normalize_text(f"{delta_text} {expected}")
    if not text:
        return True
    has_generic_phrase = any(phrase in text for phrase in _GENERIC_PHRASES)
    if has_generic_phrase and not _has_concrete_hint(text):
        return True
    words = [word for word in re.findall(r"[a-z0-9_-]+", text) if len(word) > 2]
    if len(words) < 7:
        return True
    vague_words = {
        "verify",
        "change",
        "patch",
        "test",
        "tests",
        "robust",
        "careful",
        "ensure",
        "before",
        "submit",
        "submitting",
    }
    return len(set(words) - vague_words) < 4


def _duplicate_update_id(
    proposal: dict[str, Any],
    existing_updates: list[dict[str, Any]],
) -> str:
    delta = _normalize_text(_operation_delta_text(proposal))
    if not delta:
        return ""
    for update in existing_updates:
        existing_delta = _normalize_text(
            str(
                update.get("replacement_delta")
                or update.get("instruction_delta")
                or update.get("body_preview")
                or update.get("body")
                or ""
            )
        )
        if not existing_delta:
            continue
        if delta == existing_delta:
            return str(update.get("update_id") or update.get("target_update_id") or "existing")
    return ""


def _unscoped_anchor_mismatch_rule(
    proposal: dict[str, Any],
    diagnosis: dict[str, Any],
) -> bool:
    delta_text = _normalize_text(_operation_delta_text(proposal))
    evidence_text = _normalize_text(
        " ".join(
            [
                str(proposal.get("failure_pattern") or ""),
                str(proposal.get("applicability_condition") or ""),
                " ".join(str(item) for item in proposal.get("evidence_refs", [])),
            ]
        )
    )
    text = _normalize_text(f"{evidence_text} {delta_text}")
    if not any(marker in text for marker in _ANCHOR_MISMATCH_MARKERS):
        return False
    proposal_evidence = (
        "structured_edit_anchor_mismatch" in evidence_text
        or "structured edit anchor mismatch" in evidence_text
        or "anchor mismatch" in evidence_text
    )
    if proposal_evidence:
        return False
    diagnosis_text = _normalize_text(str(diagnosis))
    return "structured_edit_anchor_mismatch" not in diagnosis_text


def _proposal_as_existing_update(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_module": proposal.get("target_module", ""),
        "instruction_delta": _operation_delta_text(proposal),
        "operation": proposal.get("operation", ""),
        "status": "active",
        "update_id": proposal.get("target_update_id", "") or "accepted_batch_update",
    }


def _has_concrete_hint(text: str) -> bool:
    lowered = _normalize_text(text)
    return any(hint in lowered for hint in _CONCRETE_OBJECT_HINTS)


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").lower().split())
