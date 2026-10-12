"""Deterministic candidate prompt materialization with Trace2Prompt operations."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from self_evolving_agent.config import PROMPT_MODULES, clone_config, get_prompt_modules


CANDIDATE_BUILDER_SCHEMA_VERSION = "m5.4.candidate_builder.v2"
TRACE2PROMPT_LEGACY_SEPARATOR = "\n\n[Trace2Prompt update]\n"
TRACE2PROMPT_SEPARATOR = "\n\n[Trace2Prompt update"
TRACE2PROMPT_BLOCK_END = "[/Trace2Prompt update]"
PROMPT_EDIT_OPERATIONS = (
    "add_append",
    "preserve_existing_update",
    "suppress_existing_update",
    "replace_existing_update",
)
MAX_ACTIVE_TRACE2PROMPT_UPDATES_PER_MODULE = 3
REQUIRED_UPDATE_KEYS = (
    "target_module",
    "failure_pattern",
    "instruction_delta",
    "expected_effect",
)
OPTIMIZER_MEMORY_AUDIT_KEYS = (
    "memory_citations",
    "valid_memory_citations",
    "invalid_memory_citations",
    "evidence_role",
    "decision_type",
    "scope_assumption",
    "anti_overfit_rationale",
    "risk",
    "do_not_repeat",
    "memory_supported",
    "memory_verifier_action",
    "memory_verifier_findings",
)

_UNSAFE_DELTA_MARKERS = (
    "ignore previous instructions",
    "disable verification",
    "skip tests",
    "do not run tests",
    "turn off security",
    "bypass safety",
    "reveal api key",
    "print token",
    "exfiltrate",
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
_START_RE = re.compile(
    r"(?m)^[ \t]*\[Trace2Prompt update(?P<meta>[^\]]*)\][ \t]*(?:\r?\n)?"
)
_HARD_GUARD_MARKERS = {
    "verification": (
        "verify",
        "test",
        "regression",
        "fail-to-pass",
        "pass-to-pass",
        "before submit",
    ),
    "security_review": (
        "security",
        "injection",
        "secret",
        "vulnerability",
        "unsafe",
    ),
}


@dataclass
class Trace2PromptBlock:
    update_id: str
    operation: str
    status: str
    module: str
    body: str
    replaces: str = ""
    legacy: bool = False
    raw_text: str = ""


@dataclass
class ParsedPrompt:
    module: str
    base_text: str
    blocks: list[Trace2PromptBlock]
    original_text: str


def inspect_trace2prompt_blocks(base_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic summaries of existing Trace2Prompt update blocks.

    This is read-only helper surface for the LLM operation planner. It exposes
    exactly the IDs that candidate_builder will later validate, without adding
    any LLM dependency to candidate materialization.
    """

    base_prompts = get_prompt_modules(base_config)
    parsed = {
        module: _parse_trace2prompt_prompt(module, base_prompts.get(module, ""))
        for module in PROMPT_MODULES
    }
    return _existing_update_blocks(parsed)


def build_candidate_prompt_config(
    base_config: dict[str, Any],
    structured_updates: list[dict[str, Any]],
    *,
    diagnosis: dict[str, Any] | None = None,
    candidate_prefix: str = "candidate",
    max_updates: int = 3,
    max_delta_chars: int = 600,
    allow_workflow_mutations: bool = True,
) -> dict[str, Any]:
    candidate = clone_config(base_config)
    candidate["name"] = f"{base_config.get('name', 'agent')}_{candidate_prefix}"
    candidate["version"] = int(base_config.get("version", 0)) + 1

    base_prompts = get_prompt_modules(base_config)
    prompts = get_prompt_modules(candidate)
    candidate["prompts"] = prompts
    workflow = candidate.setdefault("workflow", {})
    parsed = {
        module: _parse_trace2prompt_prompt(module, base_prompts.get(module, ""))
        for module in PROMPT_MODULES
    }
    existing_update_blocks = _existing_update_blocks(parsed)

    applied: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    replaced: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    dedupe_seen: dict[str, list[str]] = {}
    touched: set[str] = set()

    for update in structured_updates or []:
        sanitized = _sanitize_update(update)
        if not isinstance(update, dict):
            rejected.append(_rejected_update(sanitized, "invalid_update_type"))
            continue

        operation = _prompt_edit_operation(update)
        if operation not in PROMPT_EDIT_OPERATIONS:
            rejected.append(_rejected_update(sanitized, "invalid_operation", operation))
            continue

        module = str(update.get("target_module", "")).strip()
        if not module:
            rejected.append(
                _rejected_update(
                    sanitized,
                    "missing_required_fields:target_module",
                    operation,
                )
            )
            continue
        if module not in PROMPT_MODULES:
            rejected.append(_rejected_update(sanitized, "illegal_target_module", operation))
            continue

        if operation == "add_append":
            _handle_add_append(
                update,
                sanitized,
                module,
                parsed,
                applied,
                preserved,
                rejected,
                dedupe_seen,
                touched,
                max_updates=max_updates,
                max_delta_chars=max_delta_chars,
            )
        elif operation == "preserve_existing_update":
            _handle_preserve(update, sanitized, module, parsed, preserved, rejected)
        elif operation == "suppress_existing_update":
            _handle_suppress(update, sanitized, module, parsed, suppressed, rejected, touched)
        elif operation == "replace_existing_update":
            _handle_replace(
                update,
                sanitized,
                module,
                parsed,
                applied,
                replaced,
                rejected,
                dedupe_seen,
                touched,
                max_updates=max_updates,
                max_delta_chars=max_delta_chars,
            )

    blocked_verification_only = _block_verification_only_incremental(
        parsed,
        applied,
        preserved,
        touched,
    )

    for module in PROMPT_MODULES:
        prompts[module] = (
            _render_prompt(parsed[module]) if module in touched else base_prompts.get(module, "")
        )
    updated_modules = sorted(
        module
        for module in PROMPT_MODULES
        if base_prompts.get(module, "") != prompts.get(module, "")
    )

    if allow_workflow_mutations:
        _apply_workflow_mutations(workflow, applied)

    _drop_internal_audit_keys(applied)
    source_patterns = _source_patterns(diagnosis)
    base_hash = _stable_hash(base_config)
    candidate_id = _candidate_id(
        candidate_prefix,
        base_hash,
        applied,
        preserved,
        suppressed,
        replaced,
        rejected,
        source_patterns,
    )

    audit = {
        "schema_version": CANDIDATE_BUILDER_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "base_config_hash": base_hash,
        "candidate_config_hash": "",
        "applied_updates": applied,
        "preserved_updates": preserved,
        "suppressed_updates": suppressed,
        "replaced_updates": replaced,
        "rejected_updates": rejected,
        "existing_update_blocks": existing_update_blocks,
        "updated_modules": updated_modules,
        "prompt_diff": _prompt_diff(base_prompts, prompts, updated_modules),
        "source_patterns": source_patterns,
        "deduplication": {
            "normalization": "lowercase_whitespace_collapse",
            "seen_by_module": {
                module: list(values) for module, values in sorted(dedupe_seen.items())
            },
        },
        "allow_workflow_mutations": allow_workflow_mutations,
        "candidate_guardrails": _candidate_guardrails(
            applied,
            preserved,
            rejected,
            blocked_verification_only,
        ),
    }
    candidate["candidate_builder"] = audit
    audit["candidate_config_hash"] = _stable_hash(candidate)
    return candidate


def _handle_add_append(
    update: dict[str, Any],
    sanitized: dict[str, Any],
    module: str,
    parsed: dict[str, ParsedPrompt],
    applied: list[dict[str, Any]],
    preserved: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    dedupe_seen: dict[str, list[str]],
    touched: set[str],
    *,
    max_updates: int,
    max_delta_chars: int,
) -> None:
    reason = _add_append_rejection_reason(
        update,
        applied_count=len(applied),
        max_updates=max_updates,
        max_delta_chars=max_delta_chars,
    )
    if reason:
        rejected.append(_rejected_update(sanitized, reason, "add_append"))
        return

    delta = str(update.get("instruction_delta", "")).strip()
    normalized = _normalize_delta(delta)
    if normalized in dedupe_seen.get(module, []):
        preserved.append(
            _preserved_update(
                update,
                sanitized,
                "duplicate_delta",
                "add_append",
                module,
            )
        )
        return

    existing = _find_existing_update_by_delta(parsed, module, normalized)
    if existing or _already_present_in_prompt(parsed[module], normalized):
        preserved.append(
            _preserved_update(
                update,
                sanitized,
                "already_present",
                "add_append",
                module,
                target_update_id=existing.update_id if existing else "",
                old_delta_hash=_hash_text(existing.body) if existing else "",
            )
        )
        return

    if len(_active_blocks(parsed[module].blocks)) >= MAX_ACTIVE_TRACE2PROMPT_UPDATES_PER_MODULE:
        rejected.append(
            _rejected_update(
                sanitized,
                "active_update_limit_exceeded_requires_replace",
                "add_append",
            )
        )
        return

    update_id = _prompt_update_id(
        {
            "target_module": module,
            "operation": "add_append",
            "instruction_delta": delta,
        }
    )
    parsed[module].blocks.append(
        Trace2PromptBlock(
            update_id=update_id,
            operation="add_append",
            status="active",
            module=module,
            body=delta,
        )
    )
    applied.append(
        _applied_update(
            update,
            sanitized,
            module,
            delta,
            "add_append",
            update_id,
        )
    )
    dedupe_seen.setdefault(module, []).append(normalized)
    touched.add(module)


def _handle_preserve(
    update: dict[str, Any],
    sanitized: dict[str, Any],
    module: str,
    parsed: dict[str, ParsedPrompt],
    preserved: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> None:
    target_id = str(update.get("target_update_id", "")).strip()
    if not target_id:
        rejected.append(
            _rejected_update(sanitized, "missing_target_update_id", "preserve_existing_update")
        )
        return
    _, block = _find_active_block(parsed, module, target_id)
    if block is None:
        rejected.append(
            _rejected_update(sanitized, "unknown_target_update_id", "preserve_existing_update")
        )
        return
    preserved.append(
        _preserved_update(
            update,
            sanitized,
            "explicit_preserve",
            "preserve_existing_update",
            module,
            target_update_id=target_id,
            old_delta_hash=_hash_text(block.body),
        )
    )


def _handle_suppress(
    update: dict[str, Any],
    sanitized: dict[str, Any],
    module: str,
    parsed: dict[str, ParsedPrompt],
    suppressed: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    touched: set[str],
) -> None:
    target_id = str(update.get("target_update_id", "")).strip()
    if not target_id:
        rejected.append(
            _rejected_update(sanitized, "missing_target_update_id", "suppress_existing_update")
        )
        return
    index, block = _find_active_block(parsed, module, target_id)
    if block is None or index < 0:
        rejected.append(
            _rejected_update(sanitized, "unknown_target_update_id", "suppress_existing_update")
        )
        return

    remaining = [item for item in parsed[module].blocks if item.update_id != target_id]
    final_text = _final_active_text(parsed[module].base_text, remaining)
    if _suppression_violates_hard_guard(module, block.body, final_text):
        rejected.append(
            _rejected_update(
                sanitized,
                "hard_guard_preservation_required",
                "suppress_existing_update",
            )
        )
        return

    parsed[module].blocks = remaining
    suppressed.append(
        {
            "target_update_id": target_id,
            "target_module": module,
            "old_delta_hash": _hash_text(block.body),
            "old_status": block.status,
            "new_status": "suppressed",
            "operation": "suppress_existing_update",
            "reason": _operation_reason(update),
            "update": sanitized,
        }
    )
    touched.add(module)


def _handle_replace(
    update: dict[str, Any],
    sanitized: dict[str, Any],
    module: str,
    parsed: dict[str, ParsedPrompt],
    applied: list[dict[str, Any]],
    replaced: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    dedupe_seen: dict[str, list[str]],
    touched: set[str],
    *,
    max_updates: int,
    max_delta_chars: int,
) -> None:
    if len(applied) >= max_updates:
        rejected.append(_rejected_update(sanitized, "max_updates_exceeded", "replace_existing_update"))
        return
    target_id = str(update.get("target_update_id", "")).strip()
    if not target_id:
        rejected.append(
            _rejected_update(sanitized, "missing_target_update_id", "replace_existing_update")
        )
        return
    replacement = str(update.get("replacement_delta", "")).strip()
    if not replacement:
        rejected.append(
            _rejected_update(sanitized, "missing_replacement_delta", "replace_existing_update")
        )
        return
    reason = _delta_rejection_reason(replacement, max_delta_chars=max_delta_chars)
    if reason:
        rejected.append(_rejected_update(sanitized, reason, "replace_existing_update"))
        return
    index, old = _find_active_block(parsed, module, target_id)
    if old is None or index < 0:
        rejected.append(
            _rejected_update(sanitized, "unknown_target_update_id", "replace_existing_update")
        )
        return
    if _replacement_violates_hard_guard(module, old_body=old.body, new_body=replacement):
        rejected.append(
            _rejected_update(sanitized, "hard_guard_marker_required", "replace_existing_update")
        )
        return

    new_id = _prompt_update_id(
        {
            "target_module": module,
            "operation": "replace_existing_update",
            "target_update_id": target_id,
            "replacement_delta": replacement,
        }
    )
    parsed[module].blocks = (
        parsed[module].blocks[:index]
        + [
            Trace2PromptBlock(
                update_id=new_id,
                operation="replace_existing_update",
                status="active",
                module=module,
                body=replacement,
                replaces=target_id,
            )
        ]
        + parsed[module].blocks[index + 1 :]
    )
    applied.append(
        _applied_update(
            update,
            sanitized,
            module,
            replacement,
            "replace_existing_update",
            new_id,
            replaces=target_id,
        )
    )
    replaced.append(
        {
            "target_update_id": target_id,
            "replacement_update_id": new_id,
            "target_module": module,
            "old_delta_hash": _hash_text(old.body),
            "replacement_delta_hash": _hash_text(replacement),
            "operation": "replace_existing_update",
            "reason": _operation_reason(update),
            "update": sanitized,
        }
    )
    dedupe_seen.setdefault(module, []).append(_normalize_delta(replacement))
    touched.add(module)


def _block_verification_only_incremental(
    parsed: dict[str, ParsedPrompt],
    applied: list[dict[str, Any]],
    preserved: list[dict[str, Any]],
    touched: set[str],
) -> bool:
    prompt_changing = [
        item
        for item in applied
        if item.get("operation") in {"add_append", "replace_existing_update"}
    ]
    if len(prompt_changing) != 1:
        return False
    only = prompt_changing[0]
    if only.get("operation") != "add_append" or only.get("target_module") != "verification":
        return False
    preserved_modules = {
        str(item.get("target_module")) for item in preserved if isinstance(item, dict)
    }
    if not {"patch_generation", "localization"} <= preserved_modules:
        return False

    update_id = str(only.get("update_id", ""))
    parsed["verification"].blocks = [
        block for block in parsed["verification"].blocks if block.update_id != update_id
    ]
    applied.remove(only)
    preserved.append(
        {
            "target_module": "verification",
            "operation": "add_append",
            "status": "preserved",
            "reason": "verification_only_incremental_requires_explicit_replace",
            "update_id": update_id,
            "instruction_delta_hash": _hash_text(str(only.get("instruction_delta", ""))),
            "update": only.get("_input_update", {}),
        }
    )
    touched.add("verification")
    return True


def _candidate_guardrails(
    applied: list[dict[str, Any]],
    preserved: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    blocked_verification_only: bool,
) -> dict[str, Any]:
    flags = []
    notes = []
    if blocked_verification_only:
        flags.append("verification_only_incremental_append_blocked")
        notes.append(
            "A verification-only add_append was preserved because patch_generation "
            "and localization were already present; use replace_existing_update for "
            "intentional verification rewrites."
        )
    return {
        "flags": flags,
        "requires_targeted_regression_check": False,
        "blocked_verification_only_incremental_append": blocked_verification_only,
        "notes": notes,
        "applied_update_count": len(applied),
        "preserved_update_count": len(preserved),
        "rejected_update_count": len(rejected),
    }


def _apply_workflow_mutations(workflow: dict[str, Any], applied: list[dict[str, Any]]) -> None:
    if not applied:
        workflow.setdefault("verify_before_submit", True)
        return
    for item in applied:
        module = str(item.get("target_module", ""))
        if module == "security_review":
            workflow["security_review"] = True
            workflow["strategy"] = "keyword_plus_security"
        elif module == "verification":
            workflow["verify_before_submit"] = True


def _add_append_rejection_reason(
    update: dict[str, Any],
    *,
    applied_count: int,
    max_updates: int,
    max_delta_chars: int,
) -> str:
    if applied_count >= max_updates:
        return "max_updates_exceeded"
    missing = [key for key in REQUIRED_UPDATE_KEYS if not str(update.get(key, "")).strip()]
    if missing:
        return f"missing_required_fields:{','.join(missing)}"
    return _delta_rejection_reason(
        str(update.get("instruction_delta", "")).strip(),
        max_delta_chars=max_delta_chars,
    )


def _delta_rejection_reason(delta: str, *, max_delta_chars: int) -> str:
    if len(delta) > max_delta_chars:
        return "instruction_delta_too_long"
    if _contains_secret_like(delta):
        return "secret_like_delta"
    if _contains_unsafe_override(delta):
        return "unsafe_override"
    return ""


def _prompt_edit_operation(update: dict[str, Any]) -> str:
    return str(update.get("operation") or "add_append").strip() or "add_append"


def _operation_reason(update: dict[str, Any]) -> str:
    return _redact_secrets(str(update.get("operation_reason") or "").strip())


def _applied_update(
    update: dict[str, Any],
    sanitized: dict[str, Any],
    module: str,
    body: str,
    operation: str,
    update_id: str,
    *,
    replaces: str = "",
) -> dict[str, Any]:
    record = {
        "target_module": module,
        "failure_pattern": _redact_secrets(str(update.get("failure_pattern", "")).strip()),
        "instruction_delta": _redact_secrets(body),
        "expected_effect": _redact_secrets(str(update.get("expected_effect", "")).strip()),
        "operation": operation,
        "update_id": update_id,
        "status": "active",
        "_input_update": sanitized,
    }
    if replaces:
        record["target_update_id"] = replaces
        record["replaces"] = replaces
    if update.get("source_pattern_id"):
        record["source_pattern_id"] = _redact_secrets(str(update.get("source_pattern_id")))
    operation_reason = _operation_reason(update)
    if operation_reason:
        record["operation_reason"] = operation_reason
    optimizer_memory = _optimizer_memory_audit(update)
    if optimizer_memory:
        record["optimizer_memory"] = optimizer_memory
    return record


def _preserved_update(
    update: dict[str, Any],
    sanitized: dict[str, Any],
    reason: str,
    operation: str,
    module: str,
    *,
    target_update_id: str = "",
    old_delta_hash: str = "",
) -> dict[str, Any]:
    record = {
        "target_module": module,
        "operation": operation,
        "status": "preserved",
        "reason": reason,
        "update": sanitized,
    }
    if target_update_id:
        record["target_update_id"] = target_update_id
    if old_delta_hash:
        record["old_delta_hash"] = old_delta_hash
    operation_reason = _operation_reason(update)
    if operation_reason:
        record["operation_reason"] = operation_reason
    return record


def _rejected_update(sanitized: dict[str, Any], reason: str, operation: str = "") -> dict[str, Any]:
    record = {"update": sanitized, "reason": reason}
    if operation:
        record["operation"] = operation
    return record


def _drop_internal_audit_keys(records: list[dict[str, Any]]) -> None:
    for record in records:
        record.pop("_input_update", None)


def _parse_trace2prompt_prompt(module: str, prompt_text: str) -> ParsedPrompt:
    text = str(prompt_text or "")
    matches = list(_START_RE.finditer(text))
    if not matches:
        return ParsedPrompt(module, text, [], text)

    base_text = text[: matches[0].start()].rstrip()
    blocks: list[Trace2PromptBlock] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        close_index = text.find(TRACE2PROMPT_BLOCK_END, match.end(), next_start)
        has_end = close_index >= 0
        body_end = close_index if has_end else next_start
        raw_end = close_index + len(TRACE2PROMPT_BLOCK_END) if has_end else next_start
        body = text[match.end() : body_end].strip()
        metadata = _parse_block_metadata(match.group("meta") or "")
        legacy = not metadata and not has_end
        blocks.append(
            Trace2PromptBlock(
                update_id=metadata.get("id") or _legacy_update_id(module, body),
                operation=metadata.get("operation")
                or ("legacy_append" if legacy else "add_append"),
                status=metadata.get("status") or "active",
                module=module,
                body=body,
                replaces=metadata.get("replaces") or "",
                legacy=legacy,
                raw_text=text[match.start() : raw_end].strip(),
            )
        )
    return ParsedPrompt(module, base_text, blocks, text)


def _parse_trace2prompt_blocks(module: str, prompt_text: str) -> list[Trace2PromptBlock]:
    return _parse_trace2prompt_prompt(module, prompt_text).blocks


def _parse_block_metadata(line: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r"([a-zA-Z_][a-zA-Z0-9_]*)=([^\s\]]+)", line)
    }


def _legacy_update_id(module: str, body: str) -> str:
    return f"legacy_{module}_{_stable_hash(_normalize_delta(body))[:16]}"


def _prompt_update_id(payload: dict[str, Any]) -> str:
    return f"upd_{_stable_hash(payload)[:12]}"


def _render_trace2prompt_block(block: Trace2PromptBlock) -> str:
    if block.legacy and block.status == "active" and block.operation == "legacy_append":
        return block.raw_text.strip()
    metadata = [
        f"id={block.update_id}",
        f"operation={block.operation}",
        f"status={block.status}",
        f"module={block.module}",
    ]
    if block.replaces:
        metadata.append(f"replaces={block.replaces}")
    return (
        "[Trace2Prompt update "
        + " ".join(metadata)
        + f"]\n{block.body.strip()}\n{TRACE2PROMPT_BLOCK_END}"
    )


def _render_prompt(parsed: ParsedPrompt) -> str:
    parts: list[str] = []
    if parsed.base_text.strip():
        parts.append(parsed.base_text.rstrip())
    parts.extend(_render_trace2prompt_block(block).strip() for block in _active_blocks(parsed.blocks))
    return "\n\n".join(parts).strip()


def _active_blocks(blocks: list[Trace2PromptBlock]) -> list[Trace2PromptBlock]:
    return [block for block in blocks if str(block.status or "active") == "active"]


def _find_active_block(
    parsed: dict[str, ParsedPrompt],
    module: str,
    target_update_id: str,
) -> tuple[int, Trace2PromptBlock | None]:
    for index, block in enumerate(parsed.get(module, ParsedPrompt(module, "", [], "")).blocks):
        if block.update_id == target_update_id and block.status == "active":
            return index, block
    return -1, None


def _find_existing_update_by_delta(
    parsed: dict[str, ParsedPrompt],
    module: str,
    normalized_delta: str,
) -> Trace2PromptBlock | None:
    if not normalized_delta:
        return None
    for block in _active_blocks(parsed[module].blocks):
        if _normalize_delta(block.body) == normalized_delta:
            return block
    return None


def _already_present_in_prompt(parsed: ParsedPrompt, normalized_delta: str) -> bool:
    return bool(normalized_delta) and normalized_delta in _normalize_delta(parsed.original_text)


def _existing_update_blocks(parsed: dict[str, ParsedPrompt]) -> list[dict[str, Any]]:
    rows = []
    for module in sorted(parsed):
        for block in parsed[module].blocks:
            rows.append(
                {
                    "target_module": module,
                    "update_id": block.update_id,
                    "operation": block.operation,
                    "status": block.status,
                    "delta_hash": _hash_text(block.body),
                    "body_preview": _redact_secrets(block.body)[:240],
                    "legacy": block.legacy,
                    "replaces": block.replaces,
                }
            )
    return rows


def _final_active_text(base_text: str, blocks: list[Trace2PromptBlock]) -> str:
    return "\n".join([base_text] + [block.body for block in _active_blocks(blocks)])


def _replacement_violates_hard_guard(module: str, *, old_body: str, new_body: str) -> bool:
    return (
        module in _HARD_GUARD_MARKERS
        and _contains_hard_guard_marker(module, old_body)
        and not _contains_hard_guard_marker(module, new_body)
    )


def _suppression_violates_hard_guard(
    module: str,
    old_body: str,
    final_active_text: str,
) -> bool:
    return (
        module in _HARD_GUARD_MARKERS
        and _contains_hard_guard_marker(module, old_body)
        and not _contains_hard_guard_marker(module, final_active_text)
    )


def _contains_hard_guard_marker(module: str, text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _HARD_GUARD_MARKERS.get(module, ()))


def _sanitize_update(update: Any) -> dict[str, Any]:
    if not isinstance(update, dict):
        return {"value": _redact_secrets(str(update))}
    sanitized = {
        str(key): _redact_secrets(str(update[key]))
        for key in sorted(update)
    }
    optimizer_memory = _optimizer_memory_audit(update)
    if optimizer_memory:
        sanitized["optimizer_memory"] = optimizer_memory
    return sanitized


def _optimizer_memory_audit(update: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _redact_jsonable(update[key])
        for key in OPTIMIZER_MEMORY_AUDIT_KEYS
        if key in update
    }


def _redact_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_secrets(value)
    if isinstance(value, dict):
        return {
            str(key): _redact_jsonable(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_redact_jsonable(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_secrets(str(value))


def _source_patterns(diagnosis: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(diagnosis, dict):
        return []
    optimizer_input = diagnosis.get("optimizer_input")
    patterns = (
        optimizer_input.get("dominant_patterns")
        if isinstance(optimizer_input, dict)
        else []
    ) or diagnosis.get("top_failure_patterns") or []
    compact = []
    for pattern in patterns[:5]:
        if not isinstance(pattern, dict):
            continue
        compact.append(
            {
                "pattern_id": _redact_secrets(str(pattern.get("pattern_id", ""))),
                "primary_label": _redact_secrets(str(pattern.get("primary_label", ""))),
                "support_count": pattern.get("support_count", 0),
                "optimizer_targets": [
                    str(module)
                    for module in pattern.get("optimizer_targets", [])
                    if module in PROMPT_MODULES
                ],
            }
        )
    return compact


def _prompt_diff(
    base_prompts: dict[str, str],
    candidate_prompts: dict[str, str],
    updated_modules: list[str],
) -> str:
    chunks: list[str] = []
    for module in updated_modules:
        chunks.extend(
            difflib.unified_diff(
                _redact_secrets(base_prompts.get(module, "")).splitlines(),
                _redact_secrets(candidate_prompts.get(module, "")).splitlines(),
                fromfile=f"prompts/{module}.base",
                tofile=f"prompts/{module}.candidate",
                lineterm="",
            )
        )
    return "\n".join(chunks)


def _candidate_id(
    candidate_prefix: str,
    base_hash: str,
    applied: list[dict[str, Any]],
    preserved: list[dict[str, Any]],
    suppressed: list[dict[str, Any]],
    replaced: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    source_patterns: list[dict[str, Any]],
) -> str:
    digest = _stable_hash(
        {
            "base_config_hash": base_hash,
            "applied_updates": applied,
            "preserved_updates": preserved,
            "suppressed_updates": suppressed,
            "replaced_updates": replaced,
            "rejected_updates": rejected,
            "source_patterns": source_patterns,
        }
    )[:12]
    return f"{_slug(candidate_prefix)}-{digest}"


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _normalize_delta(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _contains_unsafe_override(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _UNSAFE_DELTA_MARKERS)


def _contains_secret_like(text: str) -> bool:
    return _redact_secrets(text) != str(text or "")


def _redact_secrets(text: str) -> str:
    redacted = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "candidate")).strip("-")
    return text or "candidate"
