"""M6.7 citation-gated optimizer memory contract.

This module is the pure contract layer between prompt-safe M6.6 memory blocks
and the schema-constrained optimizer. It intentionally does not retrieve memory,
persist memory, run harnesses, or mutate prompts. Memory may influence prompt
evolution only through cited, verified optimizer updates.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from self_evolving_agent.config import PROMPT_MODULES
from self_evolving_agent.memory_candidate import EVIDENCE_SOURCE_STRENGTHS
from self_evolving_agent.memory_retrieval import (
    MEMORY_BLOCK_SCHEMA_VERSION,
    MEMORY_BLOCK_ITEM_SCHEMA_VERSION,
    SECTION_ORDER,
    validate_memory_block,
)


OPTIMIZER_MEMORY_INPUT_SCHEMA_VERSION = "m6.7.optimizer_memory_input.v1"
OPTIMIZER_MEMORY_VERIFIER_REPORT_SCHEMA_VERSION = (
    "m6.7.optimizer_memory_verifier_report.v1"
)

OPTIMIZER_MEMORY_SECTIONS = tuple(
    section for section in SECTION_ORDER if section != "memory_use_rules"
)
POSITIVE_MEMORY_SECTIONS = (
    "global_invariants",
    "benchmark_lessons",
    "repo_lessons",
    "task_lessons",
)
NEGATIVE_MEMORY_SECTIONS = ("critical_negative",)
CAUTION_MEMORY_SECTIONS = ("cautions",)
SCOPE_ORDER = {"task": 0, "repo": 1, "benchmark": 2, "global": 3}
MEMORY_PROMPT_MODULES = (*PROMPT_MODULES, "optimizer")
EVIDENCE_ROLES = ("support", "negative_guard", "caution", "not_used")
DECISION_TYPES = ("add", "modify", "suppress", "preserve")
VERIFIER_ACTIONS = (
    "accept",
    "reject",
    "downgrade_to_hypothesis",
    "require_retry",
)
INFRASTRUCTURE_ROOT_CAUSES = {
    "validation_environment_failure",
    "conda_env_missing",
    "infrastructure_failure",
    "api_error",
    "unknown_apply_failure",
    "unknown_failure",
}
MEMORY_OPTIONAL_UPDATE_KEYS = (
    "memory_citations",
    "evidence_role",
    "decision_type",
    "scope_assumption",
    "anti_overfit_rationale",
    "risk",
    "do_not_repeat",
)

_EVIDENCE_HIERARCHY = (
    "critical_negative",
    "official_harness",
    "promotion_decision",
    "root_cause_event",
    "materializer_trace",
    "optimizer_history_only",
)
_FORBIDDEN_FIELD_MARKERS = (
    "retrieval_audit",
    "audit_view",
    "raw_traceback",
    "private_traceback",
    "traceback",
    "raw_stdout",
    "raw_stderr",
    "stdout",
    "stderr",
    "official_stdout",
    "official_stderr",
    "gold_patch",
    "gold_diff",
    "reference_patch",
    "test_patch",
    "hidden_test",
    "hidden_tests",
    "hidden_source",
    "task_id",
    "task_ids",
    "repo_id",
    "repo_ids",
    "repo_path",
    "file_path",
    "target_files",
    "line_number",
    "line_numbers",
)
_SAFE_BOUNDARY_FIELD_NAMES = {
    "audit_view_visible",
    "patch_generation_visible",
    "optimizer_only",
}
_FORBIDDEN_CONTENT_MARKERS = (
    "retrieval_audit",
    "audit_view",
    "raw stdout",
    "raw stderr",
    "official stdout",
    "official stderr",
    "traceback",
    "gold patch",
    "gold_patch",
    "reference patch",
    "reference_patch",
    "test_patch",
    "hidden test",
    "hidden_test",
    "api key",
    "api_key",
)
_GLOBAL_LANGUAGE_MARKERS = (
    "always",
    "all repos",
    "all repositories",
    "all tasks",
    "every task",
    "every repo",
    "every repository",
    "globally",
    "universal",
)
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|authorization|bearer|token|password|secret)"
        r"(\s*[:=]\s*)([^\s,'\"]+)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"),
)


class OptimizerMemoryContractError(ValueError):
    """Raised when M6.7 memory contract data is malformed or unsafe."""


def build_optimizer_memory_input(memory_block: dict[str, Any]) -> dict[str, Any]:
    """Convert an M6.6 block into a compact optimizer-facing memory input."""

    validate_memory_block(memory_block)
    memory_items: list[dict[str, Any]] = []
    for section in OPTIMIZER_MEMORY_SECTIONS:
        for item in memory_block["prompt_sections"].get(section, []):
            memory_items.append(_optimizer_memory_item(item, section))

    payload = {
        "schema_version": OPTIMIZER_MEMORY_INPUT_SCHEMA_VERSION,
        "source_memory_block_schema_version": memory_block["schema_version"],
        "selected_memory_ids": [
            str(memory_id) for memory_id in memory_block.get("selected_memory_ids", [])
        ],
        "memory_items": memory_items,
        "evidence_hierarchy": list(_EVIDENCE_HIERARCHY),
        "retrieval_boundary": {
            "audit_view_visible": False,
            "patch_generation_visible": False,
            "optimizer_only": True,
        },
        "prompt_safe_sha256": "",
    }
    payload["prompt_safe_sha256"] = _prompt_safe_hash(payload)
    validate_optimizer_memory_input(payload)
    return payload


def validate_optimizer_memory_input(memory_input: dict[str, Any]) -> None:
    """Validate an M6.7 optimizer memory input."""

    if not isinstance(memory_input, dict):
        raise OptimizerMemoryContractError("optimizer memory input must be a mapping")
    for key in (
        "schema_version",
        "source_memory_block_schema_version",
        "selected_memory_ids",
        "memory_items",
        "evidence_hierarchy",
        "retrieval_boundary",
        "prompt_safe_sha256",
    ):
        if key not in memory_input:
            raise OptimizerMemoryContractError(f"missing optimizer memory field: {key}")
    if memory_input["schema_version"] != OPTIMIZER_MEMORY_INPUT_SCHEMA_VERSION:
        raise OptimizerMemoryContractError("unsupported optimizer memory schema_version")
    if memory_input["source_memory_block_schema_version"] != MEMORY_BLOCK_SCHEMA_VERSION:
        raise OptimizerMemoryContractError("unsupported source memory block schema")
    selected_ids = _string_list(memory_input["selected_memory_ids"])
    if selected_ids != list(memory_input["selected_memory_ids"]):
        raise OptimizerMemoryContractError("selected_memory_ids must be strings")
    if not isinstance(memory_input["memory_items"], list):
        raise OptimizerMemoryContractError("memory_items must be a list")
    item_ids: list[str] = []
    for item in memory_input["memory_items"]:
        _validate_optimizer_memory_item(item)
        item_ids.append(item["memory_id"])
    if sorted(item_ids) != sorted(selected_ids):
        raise OptimizerMemoryContractError("selected_memory_ids mismatch memory_items")
    if _string_list(memory_input["evidence_hierarchy"]) != list(
        memory_input["evidence_hierarchy"]
    ):
        raise OptimizerMemoryContractError("evidence_hierarchy must be strings")
    boundary = memory_input["retrieval_boundary"]
    if not isinstance(boundary, dict):
        raise OptimizerMemoryContractError("retrieval_boundary must be a mapping")
    if boundary != {
        "audit_view_visible": False,
        "patch_generation_visible": False,
        "optimizer_only": True,
    }:
        raise OptimizerMemoryContractError("invalid retrieval_boundary")
    if memory_input["prompt_safe_sha256"] != _prompt_safe_hash(memory_input):
        raise OptimizerMemoryContractError("prompt_safe_sha256 mismatch")
    _assert_prompt_safe(memory_input)


def render_optimizer_memory_prompt(memory_input: dict[str, Any]) -> str:
    """Render optimizer-visible memory without retrieval audit or audit views."""

    validate_optimizer_memory_input(memory_input)
    grouped = _group_memory_items(memory_input)
    lines = ["<trace2prompt_optimizer_memory>"]
    lines.append("[memory_use_rules]")
    lines.append("- Memory may influence prompt updates only, never direct patch generation.")
    lines.append("- Every memory-supported decision must cite memory_id.")
    lines.append("- Separate evidence, hypothesis, and decision.")
    lines.append("- A citation proves retrieved evidence existed, not that an update is correct.")
    lines.append("- Do not generalize task or repo memory into global prompt policy.")
    lines.append("- Treat critical negative memory as guardrails.")
    lines.append("- Cautions can block false lessons, but cannot become positive patch rules.")
    for section in OPTIMIZER_MEMORY_SECTIONS:
        items = grouped.get(section, [])
        if not items:
            continue
        lines.append(f"[{section}]")
        for item in items:
            lines.append(
                "- "
                + json.dumps(
                    _renderable_memory_item(item),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            )
    lines.append("</trace2prompt_optimizer_memory>")
    rendered = "\n".join(lines)
    _assert_prompt_safe(rendered)
    return rendered


def verify_optimizer_memory_use(
    updates: list[dict[str, Any]],
    memory_input: dict[str, Any],
) -> dict[str, Any]:
    """Verify optimizer updates against retrieved memory citations and scope."""

    validate_optimizer_memory_input(memory_input)
    if not isinstance(updates, list):
        raise OptimizerMemoryContractError("updates must be a list")
    item_by_id = {
        item["memory_id"]: item for item in memory_input.get("memory_items", [])
    }
    update_reports = []
    accepted: list[int] = []
    downgraded: list[int] = []
    rejected: list[int] = []
    retry: list[int] = []
    for index, update in enumerate(updates):
        if not isinstance(update, dict):
            report = _update_report(
                index,
                "reject",
                [],
                [],
                [],
                [_finding("invalid_update_type", "error", "Update must be a mapping.")],
            )
        else:
            report = _verify_one_update(index, update, item_by_id)
        update_reports.append(report)
        if report["action"] == "accept":
            accepted.append(index)
        elif report["action"] == "downgrade_to_hypothesis":
            downgraded.append(index)
        elif report["action"] == "require_retry":
            retry.append(index)
        else:
            rejected.append(index)

    if retry:
        overall = "require_retry"
    elif accepted and not downgraded and not rejected:
        overall = "accept"
    elif accepted or downgraded:
        overall = "downgrade_to_hypothesis" if downgraded else "accept"
    else:
        overall = "reject"
    report = {
        "schema_version": OPTIMIZER_MEMORY_VERIFIER_REPORT_SCHEMA_VERSION,
        "overall_action": overall,
        "update_reports": update_reports,
        "accepted_update_indices": accepted,
        "downgraded_update_indices": downgraded,
        "rejected_update_indices": rejected,
        "require_retry_update_indices": retry,
    }
    validate_optimizer_memory_verifier_report(report)
    return report


def validate_optimizer_memory_verifier_report(report: dict[str, Any]) -> None:
    """Validate a deterministic M6.7 verifier report."""

    if not isinstance(report, dict):
        raise OptimizerMemoryContractError("verifier report must be a mapping")
    for key in (
        "schema_version",
        "overall_action",
        "update_reports",
        "accepted_update_indices",
        "downgraded_update_indices",
        "rejected_update_indices",
        "require_retry_update_indices",
    ):
        if key not in report:
            raise OptimizerMemoryContractError(f"missing verifier report field: {key}")
    if report["schema_version"] != OPTIMIZER_MEMORY_VERIFIER_REPORT_SCHEMA_VERSION:
        raise OptimizerMemoryContractError("unsupported verifier report schema")
    if report["overall_action"] not in VERIFIER_ACTIONS:
        raise OptimizerMemoryContractError("invalid overall verifier action")
    if not isinstance(report["update_reports"], list):
        raise OptimizerMemoryContractError("update_reports must be a list")
    for index_key in (
        "accepted_update_indices",
        "downgraded_update_indices",
        "rejected_update_indices",
        "require_retry_update_indices",
    ):
        values = report[index_key]
        if not isinstance(values, list) or any(not isinstance(v, int) for v in values):
            raise OptimizerMemoryContractError(f"{index_key} must be a list of ints")
    for item in report["update_reports"]:
        _validate_update_report(item)
    _assert_prompt_safe(report)


def apply_memory_verifier_actions(
    updates: list[dict[str, Any]],
    verifier_report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return only accepted/downgraded updates with audit-only M6.7 metadata."""

    validate_optimizer_memory_verifier_report(verifier_report)
    if not isinstance(updates, list):
        raise OptimizerMemoryContractError("updates must be a list")
    reports = {
        report["update_index"]: report
        for report in verifier_report.get("update_reports", [])
    }
    filtered: list[dict[str, Any]] = []
    for index, update in enumerate(updates):
        if not isinstance(update, dict):
            continue
        report = reports.get(index)
        if not report or report["action"] in {"reject", "require_retry"}:
            continue
        next_update = deepcopy(update)
        next_update["memory_supported"] = (
            report["action"] == "accept"
            and bool(report["valid_citations"])
            and str(update.get("evidence_role") or "not_used") != "not_used"
        )
        next_update["memory_verifier_action"] = report["action"]
        next_update["valid_memory_citations"] = list(report["valid_citations"])
        next_update["invalid_memory_citations"] = list(report["invalid_citations"])
        next_update["memory_verifier_findings"] = deepcopy(report["findings"])
        if report["action"] == "downgrade_to_hypothesis":
            next_update["evidence_role"] = "not_used"
            next_update["memory_citations"] = list(report["valid_citations"])
        filtered.append(next_update)
    return filtered


def build_memory_grounded_optimizer_result(
    diagnosis: dict[str, Any],
    *,
    llm_client: Any,
    memory_block: dict[str, Any] | None = None,
    fallback_updates: list[dict[str, Any]] | None = None,
    max_updates: int = 3,
    max_completion_tokens: int = 900,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Build optimizer updates plus M6.7 audit data via ``LLMOptimizer``.

    This is a convenience entrypoint for callers that need the verifier report.
    It lazy-imports the optimizer to keep this contract module side-effect free.
    """

    from self_evolving_agent.llm_optimizer import LLMOptimizer

    optimizer = LLMOptimizer(
        llm_client,
        max_updates=max_updates,
        max_completion_tokens=max_completion_tokens,
        timeout_s=timeout_s,
    )
    return optimizer.build_updates(
        diagnosis,
        fallback_updates=fallback_updates,
        memory_block=memory_block,
        return_metadata=True,
    )


def _optimizer_memory_item(item: dict[str, Any], section: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise OptimizerMemoryContractError("memory block item must be a mapping")
    if item.get("schema_version") != MEMORY_BLOCK_ITEM_SCHEMA_VERSION:
        raise OptimizerMemoryContractError("unsupported memory block item schema")
    memory_item = {
        "memory_id": str(item.get("memory_id") or ""),
        "section": section,
        "scope": str(item.get("scope") or ""),
        "root_cause": str(item.get("root_cause") or ""),
        "prompt_module": str(item.get("prompt_module") or ""),
        "lesson": str(item.get("lesson") or ""),
        "expected_effect": str(item.get("expected_effect") or ""),
        "status": str(item.get("status") or ""),
        "confidence": _float_or_default(item.get("confidence"), 0.0),
        "support_count": _int_or_zero(item.get("support_count")),
        "source_types": _string_list(item.get("source_types") or []),
    }
    _validate_optimizer_memory_item(memory_item)
    return memory_item


def _validate_optimizer_memory_item(item: Any) -> None:
    if not isinstance(item, dict):
        raise OptimizerMemoryContractError("memory item must be a mapping")
    for key in (
        "memory_id",
        "section",
        "scope",
        "root_cause",
        "prompt_module",
        "lesson",
        "expected_effect",
        "status",
        "confidence",
        "support_count",
        "source_types",
    ):
        if key not in item:
            raise OptimizerMemoryContractError(f"missing memory item field: {key}")
    for key in (
        "memory_id",
        "section",
        "scope",
        "root_cause",
        "prompt_module",
        "lesson",
        "expected_effect",
        "status",
    ):
        if not isinstance(item[key], str):
            raise OptimizerMemoryContractError(f"{key} must be a string")
    if item["section"] not in OPTIMIZER_MEMORY_SECTIONS:
        raise OptimizerMemoryContractError("invalid memory item section")
    if item["scope"] not in SCOPE_ORDER:
        raise OptimizerMemoryContractError("invalid memory item scope")
    if item["prompt_module"] and item["prompt_module"] not in MEMORY_PROMPT_MODULES:
        raise OptimizerMemoryContractError("invalid memory item prompt_module")
    if not isinstance(item["confidence"], (int, float)):
        raise OptimizerMemoryContractError("confidence must be numeric")
    if not isinstance(item["support_count"], int) or item["support_count"] < 0:
        raise OptimizerMemoryContractError("support_count must be non-negative")
    source_types = _string_list(item["source_types"])
    if source_types != list(item["source_types"]):
        raise OptimizerMemoryContractError("source_types must be strings")
    invalid_sources = [
        source for source in source_types if source not in EVIDENCE_SOURCE_STRENGTHS
    ]
    if invalid_sources:
        raise OptimizerMemoryContractError("invalid memory item source_type")
    _assert_prompt_safe(item)


def _verify_one_update(
    index: int,
    update: dict[str, Any],
    item_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    citations = _string_list(update.get("memory_citations") or [])
    valid_citations = [citation for citation in citations if citation in item_by_id]
    invalid_citations = [citation for citation in citations if citation not in item_by_id]
    cited_items = [item_by_id[citation] for citation in valid_citations]
    findings: list[dict[str, str]] = []
    action = "accept"

    try:
        _assert_prompt_safe(update)
    except OptimizerMemoryContractError as exc:
        findings.append(_finding("audit_only_field_leak", "error", str(exc)))
        return _update_report(
            index,
            "reject",
            citations,
            valid_citations,
            invalid_citations,
            findings,
        )

    evidence_role = str(update.get("evidence_role") or "not_used")
    if evidence_role not in EVIDENCE_ROLES:
        findings.append(
            _finding(
                "invalid_evidence_role",
                "warning",
                "Unknown evidence_role downgraded to hypothesis.",
            )
        )
        action = "downgrade_to_hypothesis"
    if invalid_citations:
        findings.append(
            _finding(
                "invalid_memory_citation",
                "warning",
                "Memory citation was not in selected_memory_ids.",
            )
        )
        action = _max_action(action, "downgrade_to_hypothesis")
    if evidence_role != "not_used" and not valid_citations:
        findings.append(
            _finding(
                "missing_memory_citation",
                "warning",
                "Memory-supported update did not cite retrieved memory.",
            )
        )
        action = _max_action(action, "downgrade_to_hypothesis")

    if _scope_overgeneralizes(update, cited_items):
        findings.append(
            _finding(
                "scope_overgeneralization",
                "error",
                "Task/repo memory was generalized beyond its safe scope.",
            )
        )
        action = "reject"
    if _infra_caution_as_positive(update, cited_items):
        findings.append(
            _finding(
                "infra_caution_as_positive_lesson",
                "error",
                "Infrastructure or caution memory cannot become a positive patch rule.",
            )
        )
        action = "reject"
    if _critical_negative_repeated(update, cited_items):
        findings.append(
            _finding(
                "critical_negative_repeated",
                "error",
                "Update repeats or positively uses critical negative memory.",
            )
        )
        action = "reject"
    if action not in {"reject", "require_retry"} and _critical_negative_ignored(
        update,
        item_by_id,
        valid_citations,
    ):
        findings.append(
            _finding(
                "critical_negative_ignored",
                "warning",
                "Relevant critical negative memory was not cited as a guardrail.",
            )
        )
        action = "require_retry"
    if action == "accept" and not valid_citations and evidence_role == "not_used":
        findings.append(
            _finding(
                "uncited_safe_update",
                "info",
                "Safe uncited update preserved as hypothesis, not memory evidence.",
            )
        )
        action = "downgrade_to_hypothesis"
    if action == "accept":
        findings.append(
            _finding(
                "valid_memory_supported_update",
                "info",
                "Valid citations and compatible scope.",
            )
        )
    return _update_report(
        index,
        action,
        citations,
        valid_citations,
        invalid_citations,
        findings,
    )


def _validate_update_report(report: Any) -> None:
    if not isinstance(report, dict):
        raise OptimizerMemoryContractError("update report must be a mapping")
    for key in (
        "update_index",
        "action",
        "memory_citations",
        "valid_citations",
        "invalid_citations",
        "findings",
    ):
        if key not in report:
            raise OptimizerMemoryContractError(f"missing update report field: {key}")
    if not isinstance(report["update_index"], int) or report["update_index"] < 0:
        raise OptimizerMemoryContractError("update_index must be non-negative")
    if report["action"] not in VERIFIER_ACTIONS:
        raise OptimizerMemoryContractError("invalid update report action")
    for key in ("memory_citations", "valid_citations", "invalid_citations"):
        if _string_list(report[key]) != list(report[key]):
            raise OptimizerMemoryContractError(f"{key} must be strings")
    if not isinstance(report["findings"], list):
        raise OptimizerMemoryContractError("findings must be a list")
    for finding in report["findings"]:
        if not isinstance(finding, dict):
            raise OptimizerMemoryContractError("finding must be a mapping")
        for key in ("rule_id", "severity", "message"):
            if not isinstance(finding.get(key), str) or not finding[key]:
                raise OptimizerMemoryContractError(f"invalid finding {key}")


def _scope_overgeneralizes(
    update: dict[str, Any],
    cited_items: list[dict[str, Any]],
) -> bool:
    if not cited_items:
        return False
    scope_assumption = str(update.get("scope_assumption") or "").strip()
    update_text = _normalized_update_text(update)
    for item in cited_items:
        scope = item.get("scope")
        if scope not in {"task", "repo"}:
            continue
        if scope_assumption and SCOPE_ORDER.get(scope_assumption, 99) > SCOPE_ORDER[scope]:
            return True
        if any(marker in update_text for marker in _GLOBAL_LANGUAGE_MARKERS):
            return True
    return False


def _infra_caution_as_positive(
    update: dict[str, Any],
    cited_items: list[dict[str, Any]],
) -> bool:
    if not cited_items:
        return False
    evidence_role = str(update.get("evidence_role") or "not_used")
    target_module = str(update.get("target_module") or "")
    if evidence_role != "support":
        return False
    for item in cited_items:
        if (
            item.get("section") in CAUTION_MEMORY_SECTIONS
            or item.get("root_cause") in INFRASTRUCTURE_ROOT_CAUSES
        ) and target_module == "patch_generation":
            return True
    return False


def _critical_negative_repeated(
    update: dict[str, Any],
    cited_items: list[dict[str, Any]],
) -> bool:
    evidence_role = str(update.get("evidence_role") or "not_used")
    if evidence_role == "support" and any(
        item.get("section") in NEGATIVE_MEMORY_SECTIONS for item in cited_items
    ):
        return True
    delta = _normalize_text(update.get("instruction_delta") or "")
    if not delta:
        return False
    for item in cited_items:
        if item.get("section") not in NEGATIVE_MEMORY_SECTIONS:
            continue
        lesson = _normalize_text(item.get("lesson") or "")
        if lesson and (lesson in delta or delta in lesson):
            return True
    return False


def _critical_negative_ignored(
    update: dict[str, Any],
    item_by_id: dict[str, dict[str, Any]],
    valid_citations: list[str],
) -> bool:
    if not valid_citations:
        return False
    cited_sections = {item_by_id[citation]["section"] for citation in valid_citations}
    if "critical_negative" in cited_sections:
        return False
    if not cited_sections.intersection(POSITIVE_MEMORY_SECTIONS):
        return False
    target_module = str(update.get("target_module") or "")
    failure_pattern = str(update.get("failure_pattern") or "")
    for item in item_by_id.values():
        if item.get("section") != "critical_negative":
            continue
        module_matches = bool(target_module and target_module == item.get("prompt_module"))
        pattern_matches = bool(
            failure_pattern
            and failure_pattern in {item.get("root_cause"), item.get("status")}
        )
        if module_matches or pattern_matches:
            return True
    return False


def _max_action(current: str, next_action: str) -> str:
    rank = {
        "accept": 0,
        "downgrade_to_hypothesis": 1,
        "require_retry": 2,
        "reject": 3,
    }
    return next_action if rank[next_action] > rank[current] else current


def _update_report(
    index: int,
    action: str,
    memory_citations: list[str],
    valid_citations: list[str],
    invalid_citations: list[str],
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "update_index": index,
        "action": action,
        "memory_citations": list(memory_citations),
        "valid_citations": list(valid_citations),
        "invalid_citations": list(invalid_citations),
        "findings": findings,
    }


def _finding(rule_id: str, severity: str, message: str) -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
    }


def _group_memory_items(memory_input: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped = {section: [] for section in OPTIMIZER_MEMORY_SECTIONS}
    for item in memory_input["memory_items"]:
        grouped[item["section"]].append(item)
    return grouped


def _renderable_memory_item(item: dict[str, Any]) -> dict[str, Any]:
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
    }


def _prompt_safe_hash(memory_input: dict[str, Any]) -> str:
    payload = {
        key: deepcopy(value)
        for key, value in memory_input.items()
        if key != "prompt_safe_sha256"
    }
    return hashlib.sha256(
        json.dumps(
            _jsonable(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _assert_prompt_safe(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered_key = str(key).lower()
            if lowered_key not in _SAFE_BOUNDARY_FIELD_NAMES and any(
                marker in lowered_key for marker in _FORBIDDEN_FIELD_MARKERS
            ):
                raise OptimizerMemoryContractError(
                    f"prompt-unsafe field is not allowed: {key}"
                )
            _assert_prompt_safe(item)
        return
    if isinstance(value, list):
        for item in value:
            _assert_prompt_safe(item)
        return
    if isinstance(value, tuple):
        for item in value:
            _assert_prompt_safe(item)
        return
    if isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in _FORBIDDEN_CONTENT_MARKERS):
            raise OptimizerMemoryContractError("prompt-unsafe content marker detected")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise OptimizerMemoryContractError("secret-like prompt content detected")


def _normalized_update_text(update: dict[str, Any]) -> str:
    text = " ".join(
        str(update.get(key) or "")
        for key in (
            "failure_pattern",
            "instruction_delta",
            "expected_effect",
            "anti_overfit_rationale",
            "risk",
        )
    )
    return _normalize_text(text)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
