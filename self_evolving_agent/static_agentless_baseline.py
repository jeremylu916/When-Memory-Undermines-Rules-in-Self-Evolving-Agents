"""B1 Static Agentless-Style no-memory baseline.

B1 is stronger than B0b because it performs deterministic static localization
before repair, but it remains free of memory, prompt evolution, cross-task
learning, optimizer feedback, root-cause summaries, and official feedback during
generation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Protocol

from self_evolving_agent.benchmarks import BenchmarkTask
from self_evolving_agent.direct_retry_baseline import (
    MockDirectRetryLLM,
    check_patch_applies,
    config_audit_view,
    extract_unified_diff,
    load_mock_response_jsonl,
    model_identity_from_config,
)
from self_evolving_agent.llm import LLMResponse, LLMUsage
from self_evolving_agent.patch_materializer import (
    StructuredEdit,
    apply_structured_edits,
    extract_structured_edits,
)


B1_RUN_CONFIG_SCHEMA_VERSION = "b1.run_config.v1"
B1_LOCALIZATION_SCHEMA_VERSION = "b1.localization.v1"
B1_ATTEMPT_SCHEMA_VERSION = "b1.attempt.v1"
B1_SUMMARY_SCHEMA_VERSION = "b1.baseline_summary.v1"
B1_COST_SUMMARY_SCHEMA_VERSION = "b1.cost_summary.v1"

PATCH_APPLY_CHECKS = ("empty", "malformed", "failed", "passed")
MATERIALIZERS = ("structured-edit", "unified-diff")

DEFAULT_LOCALIZATION_MAX_FILES = 8
DEFAULT_LOCALIZATION_MAX_FILE_CHARS = 5000
DEFAULT_LOCALIZATION_MAX_TOTAL_CHARS = 26000
DEFAULT_APPLY_TIMEOUT_S = 30

LEAKAGE_SENSITIVE_METADATA_KEYS = {
    "patch",
    "gold_patch",
    "model_patch",
    "test_patch",
    "hints_text",
    "fail_to_pass",
    "pass_to_pass",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "resolved",
    "resolved_ids",
    "unresolved_ids",
    "tests_status",
    "official_harness",
    "expected_solution",
}

BOILERPLATE_DIR_PARTS = {
    ".github",
    "docs",
    "doc",
    "documentation",
    "news",
    "changelog",
    "changes",
    "tutorial",
    "tutorials",
    "examples",
}

BOILERPLATE_FILE_MARKERS = {
    "issue_template",
    "pull_request_template",
    "bug_report",
    "changelog",
    "changes",
    "whats_new",
    "release",
    "install",
    "contributing",
    "readme",
}

SOURCE_SUFFIX_BONUS = {
    ".py": 28,
    ".pyx": 28,
    ".pxd": 24,
    ".js": 24,
    ".jsx": 24,
    ".ts": 24,
    ".tsx": 24,
    ".java": 24,
    ".c": 22,
    ".cc": 22,
    ".cpp": 22,
    ".h": 20,
    ".hpp": 20,
    ".rs": 24,
    ".go": 24,
    ".rb": 22,
    ".php": 22,
}

SKIP_SUFFIXES = {
    ".7z",
    ".bmp",
    ".class",
    ".dll",
    ".dylib",
    ".egg",
    ".exe",
    ".gif",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lock",
    ".mp4",
    ".o",
    ".pdf",
    ".png",
    ".pyc",
    ".pyo",
    ".so",
    ".sqlite",
    ".tar",
    ".whl",
    ".zip",
}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "vendor",
}

COMMON_TERMS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
    "expected",
    "actual",
    "behavior",
    "bug",
    "error",
    "issue",
}

SECRET_PATTERNS = (
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


class ChatClient(Protocol):
    model: str

    def chat(self, messages: list[dict[str, str]]) -> LLMResponse:
        """Return an LLM response for the provided messages."""


@dataclass(frozen=True)
class LocalizationPolicy:
    max_files: int = DEFAULT_LOCALIZATION_MAX_FILES
    max_file_chars: int = DEFAULT_LOCALIZATION_MAX_FILE_CHARS
    max_total_chars: int = DEFAULT_LOCALIZATION_MAX_TOTAL_CHARS
    include_tests: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_localization_context(
    task: BenchmarkTask,
    workspace: str | Path,
    *,
    policy: LocalizationPolicy | None = None,
) -> dict[str, Any]:
    """Build deterministic static localization context from issue and repo only."""

    policy = policy or LocalizationPolicy()
    workspace_path = Path(workspace)
    issue_text = task.issue_description or task.prompt
    issue_terms = _tokenize(issue_text)
    safe_metadata, leakage_audit = sanitize_task_metadata(task)
    scored_files = _score_files(
        workspace_path=workspace_path,
        issue_terms=issue_terms,
        policy=policy,
    )

    selected_files: list[dict[str, Any]] = []
    total_chars = 0
    for item in scored_files:
        if len(selected_files) >= policy.max_files:
            break
        remaining = max(0, policy.max_total_chars - total_chars)
        if remaining <= 0:
            break
        excerpt = str(item["excerpt"])[: min(policy.max_file_chars, remaining)]
        if not excerpt:
            continue
        total_chars += len(excerpt)
        selected_files.append(
            {
                "path": item["path"],
                "score": item["score"],
                "score_reasons": item["score_reasons"],
                "excerpt_chars": len(excerpt),
                "excerpt": excerpt,
            }
        )

    context_text = render_localization_context_text(
        task=task,
        safe_metadata=safe_metadata,
        selected_files=selected_files,
    )
    return {
        "schema_version": B1_LOCALIZATION_SCHEMA_VERSION,
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "repo": str(safe_metadata.get("repo") or ""),
        "base_commit": task.base_commit or "",
        "localized_context_sha256": sha256_text(context_text),
        "localization_policy": policy.to_dict(),
        "selected_files": selected_files,
        "omitted_file_count": max(0, len(scored_files) - len(selected_files)),
        "leakage_audit": leakage_audit,
        "context_chars": len(context_text),
        "context_text": context_text,
    }


def sanitize_task_metadata(task: BenchmarkTask) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = dict(task.metadata or {})
    blocked_metadata_fields: list[str] = []
    safe_metadata: dict[str, Any] = {}
    for key, value in sorted(metadata.items()):
        if key in LEAKAGE_SENSITIVE_METADATA_KEYS:
            blocked_metadata_fields.append(key)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe_metadata[key] = value
        elif isinstance(value, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None
            for item in value
        ):
            safe_metadata[key] = list(value)

    blocked_task_fields = []
    if task.test_patch:
        blocked_task_fields.append("test_patch")
    if task.expected_output and task.expected_output != "patch_applied":
        blocked_task_fields.append("expected_output")
    return safe_metadata, {
        "blocked_task_fields": sorted(blocked_task_fields),
        "blocked_metadata_fields": sorted(blocked_metadata_fields),
        "benchmark_test_patch_visible": False,
        "gold_patch_visible": False,
        "official_feedback_visible": False,
    }


def render_localization_context_text(
    *,
    task: BenchmarkTask,
    safe_metadata: dict[str, Any],
    selected_files: list[dict[str, Any]],
) -> str:
    issue_text = _redact_secrets(task.issue_description or task.prompt).strip()
    repo = str(safe_metadata.get("repo") or "")
    lines = [
        "B1 static localized repository context.",
        "This context was selected before any LLM attempt and contains no prior feedback.",
        "",
        "Problem statement:",
        issue_text,
        "",
    ]
    if repo:
        lines.extend(["Repository:", repo, ""])
    if task.base_commit:
        lines.extend(["Base commit:", task.base_commit, ""])
    lines.append("Localized repository excerpts:")
    if not selected_files:
        lines.append("(no repair-relevant text files selected)")
    for item in selected_files:
        lines.extend(["", f"### {item['path']}", str(item["excerpt"]).rstrip()])
    return "\n".join(lines).rstrip() + "\n"


def build_agentless_messages(
    task: BenchmarkTask,
    localization_context: dict[str, Any],
    *,
    materializer: str = "structured-edit",
) -> list[dict[str, str]]:
    if materializer not in MATERIALIZERS:
        raise ValueError(f"unsupported B1 materializer: {materializer}")
    system = (
        "You are a static agentless software repair baseline. Use only the "
        "provided issue and localized repository excerpts. Do not use memory, "
        "prompt evolution, prior task outcomes, hidden tests, gold patches, or "
        "official feedback. Produce one minimal repair."
    )
    if materializer == "structured-edit":
        user = (
            f"{localization_context['context_text']}\n"
            "Return JSON only, with this schema:\n"
            "{\"edits\":[{\"file\":\"relative/path.py\",\"find\":\"old text or guard\","
            "\"replace\":\"new text\",\"start_line\":1,\"end_line\":1}]}\n"
            "Use either find/replace or start_line/end_line with an optional guard. "
            "Do not include explanations outside JSON."
        )
    else:
        user = (
            f"{localization_context['context_text']}\n"
            "Return exactly one minimal git unified diff that can be applied with "
            "git apply. Do not include explanations outside the diff."
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def run_static_agentless_task(
    *,
    task: BenchmarkTask,
    workspace: str | Path,
    llm: ChatClient,
    attempts: int = 1,
    localization_policy: LocalizationPolicy | None = None,
    materializer: str = "structured-edit",
    apply_timeout_s: int = DEFAULT_APPLY_TIMEOUT_S,
    temperature: float | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if materializer not in MATERIALIZERS:
        raise ValueError(f"unsupported B1 materializer: {materializer}")

    localization_context = build_localization_context(
        task,
        workspace,
        policy=localization_policy or LocalizationPolicy(),
    )
    messages = build_agentless_messages(
        task,
        localization_context,
        materializer=materializer,
    )
    prompt_hash = sha256_json(messages)
    context_hash = str(localization_context["localized_context_sha256"])

    attempt_records: list[dict[str, Any]] = []
    for attempt_index in range(1, attempts + 1):
        attempt_seed = None if seed is None else int(seed) + attempt_index - 1
        try:
            response = llm.chat(messages)
            content = response.content
            finish_reason = _finish_reason(response)
            usage = response.usage
            model = response.model or getattr(llm, "model", "")
            error_tail = ""
        except Exception as exc:  # pragma: no cover - defensive for real API runs
            content = ""
            finish_reason = "error"
            usage = LLMUsage()
            model = str(getattr(llm, "model", ""))
            error_tail = _bounded_tail(str(exc))

        materialized = materialize_repair_response(
            response_text=content,
            workspace=workspace,
            materializer=materializer,
            timeout_s=apply_timeout_s,
        )
        apply_error_tail = error_tail or materialized["apply_error_tail"]
        attempt_records.append(
            {
                "schema_version": B1_ATTEMPT_SCHEMA_VERSION,
                "task_id": task.task_id,
                "attempt_index": attempt_index,
                "prompt_sha256": prompt_hash,
                "localized_context_sha256": context_hash,
                "model": model,
                "temperature": temperature,
                "seed": attempt_seed,
                "finish_reason": finish_reason,
                "response_chars": len(content),
                "patch_chars": len(materialized["patch"]),
                "patch": materialized["patch"],
                "patch_apply_check": materialized["patch_apply_check"],
                "apply_error_tail": apply_error_tail,
                "materializer": materializer,
                "structured_edit_count": materialized["structured_edit_count"],
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cost_usd": usage.cost_usd,
                "selected": False,
            }
        )

    selected_attempt = _first_selected_attempt(attempt_records)
    if selected_attempt is not None:
        selected_attempt["selected"] = True
        selected_patch = str(selected_attempt["patch"])
        selected_attempt_index = int(selected_attempt["attempt_index"])
    else:
        selected_patch = ""
        selected_attempt_index = None

    return {
        "task_id": task.task_id,
        "localization": localization_context,
        "messages": messages,
        "attempts": attempt_records,
        "prediction": {
            "instance_id": task.task_id,
            "model_patch": selected_patch,
        },
        "selected_attempt_index": selected_attempt_index,
    }


def materialize_repair_response(
    *,
    response_text: str,
    workspace: str | Path,
    materializer: str,
    timeout_s: int = DEFAULT_APPLY_TIMEOUT_S,
) -> dict[str, Any]:
    if materializer == "unified-diff":
        patch = extract_unified_diff(response_text)
        check = check_patch_applies(
            workspace=workspace,
            patch_text=patch,
            timeout_s=timeout_s,
        )
        return {
            "patch": patch,
            "patch_apply_check": check.status,
            "apply_error_tail": check.error_tail,
            "structured_edit_count": 0,
        }

    if not str(response_text or "").strip() or str(response_text).strip() == "NO_PATCH":
        return {
            "patch": "",
            "patch_apply_check": "empty",
            "apply_error_tail": "",
            "structured_edit_count": 0,
        }
    edits = extract_structured_edits(response_text)
    if not edits:
        return {
            "patch": "",
            "patch_apply_check": "malformed",
            "apply_error_tail": "no valid structured edits",
            "structured_edit_count": 0,
        }

    workspace_path = Path(workspace)
    with tempfile.TemporaryDirectory(prefix="b1-structured-") as tmpdir:
        scratch = Path(tmpdir) / "workspace"
        scratch.mkdir(parents=True)
        copy_error = _copy_structured_edit_targets(workspace_path, scratch, edits)
        if copy_error:
            return {
                "patch": "",
                "patch_apply_check": "failed",
                "apply_error_tail": copy_error,
                "structured_edit_count": len(edits),
            }
        result = apply_structured_edits(scratch, edits)
        if not result.success:
            return {
                "patch": "",
                "patch_apply_check": "failed",
                "apply_error_tail": _bounded_tail(result.error),
                "structured_edit_count": len(edits),
            }
        patch = _diff_structured_result(workspace_path, scratch, result.file_edits)
    if not patch.strip():
        return {
            "patch": "",
            "patch_apply_check": "empty",
            "apply_error_tail": "structured edits produced no diff",
            "structured_edit_count": len(edits),
        }
    check = check_patch_applies(
        workspace=workspace,
        patch_text=patch,
        timeout_s=timeout_s,
    )
    return {
        "patch": patch,
        "patch_apply_check": check.status,
        "apply_error_tail": check.error_tail,
        "structured_edit_count": len(edits),
    }


def build_predictions(
    task_results: list[dict[str, Any]],
    *,
    model_name_or_path: str,
) -> list[dict[str, str]]:
    return [
        {
            "instance_id": str(result["prediction"]["instance_id"]),
            "model_name_or_path": model_name_or_path,
            "model_patch": str(result["prediction"].get("model_patch") or ""),
        }
        for result in task_results
    ]


def build_b1_summary(
    *,
    run_id: str,
    task_results: list[dict[str, Any]],
    model_name_or_path: str,
    materializer: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    attempt_records = [
        attempt for result in task_results for attempt in result.get("attempts", [])
    ]
    check_counts = {status: 0 for status in PATCH_APPLY_CHECKS}
    for attempt in attempt_records:
        status = str(attempt.get("patch_apply_check") or "")
        if status in check_counts:
            check_counts[status] += 1
    selected_patch_count = sum(
        1
        for result in task_results
        if str(result["prediction"].get("model_patch") or "").strip()
    )
    return {
        "schema_version": B1_SUMMARY_SCHEMA_VERSION,
        "created_at": created_at or utc_now(),
        "run_id": run_id,
        "baseline": "B1 Static Agentless-Style",
        "model_name_or_path": model_name_or_path,
        "materializer": materializer,
        "task_count": len(task_results),
        "attempt_count": len(attempt_records),
        "selected_patch_count": selected_patch_count,
        "no_patch_task_count": len(task_results) - selected_patch_count,
        "patch_apply_check_counts": check_counts,
        "selected_attempts": {
            result["task_id"]: result.get("selected_attempt_index")
            for result in task_results
        },
        "official_harness_executed": False,
    }


def build_b1_cost_summary(
    *,
    run_id: str,
    task_results: list[dict[str, Any]],
    created_at: str | None = None,
) -> dict[str, Any]:
    per_task: dict[str, dict[str, float | int]] = {}
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }
    for result in task_results:
        task_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }
        for attempt in result.get("attempts", []):
            prompt_tokens = int(attempt.get("prompt_tokens", 0) or 0)
            completion_tokens = int(attempt.get("completion_tokens", 0) or 0)
            cost_usd = float(attempt.get("cost_usd", 0.0) or 0.0)
            task_totals["prompt_tokens"] += prompt_tokens
            task_totals["completion_tokens"] += completion_tokens
            task_totals["total_tokens"] += prompt_tokens + completion_tokens
            task_totals["cost_usd"] += cost_usd
        per_task[result["task_id"]] = task_totals
        for key in totals:
            totals[key] += task_totals[key]  # type: ignore[operator]
    return {
        "schema_version": B1_COST_SUMMARY_SCHEMA_VERSION,
        "created_at": created_at or utc_now(),
        "run_id": run_id,
        "totals": totals,
        "per_task": per_task,
    }


def build_b1_run_config_record(
    *,
    run_id: str,
    model_name_or_path: str,
    attempts: int,
    selected_task_ids: list[str],
    localization_policy: LocalizationPolicy,
    materializer: str,
    config_audit: dict[str, Any],
    workspace_source: str,
    dry_run: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": B1_RUN_CONFIG_SCHEMA_VERSION,
        "created_at": created_at or utc_now(),
        "run_id": run_id,
        "baseline": "B1 Static Agentless-Style",
        "baseline_role": "primary no-memory/no-evolution comparator",
        "model_name_or_path": model_name_or_path,
        "attempts": attempts,
        "selected_task_ids": list(selected_task_ids),
        "localization_policy": localization_policy.to_dict(),
        "materializer": materializer,
        "workspace_source": workspace_source,
        "dry_run": bool(dry_run),
        "llm_requests_executed": 0 if dry_run else None,
        "official_harness_executed": False,
        "config_audit": config_audit,
        "forbidden_generation_mechanisms": [
            "memory_retrieval",
            "active_memory_store",
            "scoped_memory_operations",
            "critic",
            "failure_aggregator",
            "optimizer",
            "candidate_builder",
            "evolution_memory",
            "root_cause_summaries",
            "promotion_gate",
            "attempt_feedback",
            "official_feedback_during_generation",
            "cross_task_learning",
        ],
    }


def write_b1_artifacts(
    *,
    run_dir: str | Path,
    run_config: dict[str, Any],
    task_results: list[dict[str, Any]],
    predictions: list[dict[str, str]],
    summary: dict[str, Any],
    cost_summary: dict[str, Any],
) -> dict[str, str]:
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "run_config": root / "run_config.json",
        "localization": root / "localization.jsonl",
        "attempts": root / "attempts.jsonl",
        "predictions": root / "predictions.jsonl",
        "summary": root / "baseline_summary.json",
        "cost_summary": root / "cost_summary.json",
    }
    _write_json(paths["run_config"], run_config)
    _write_jsonl(paths["localization"], [result["localization"] for result in task_results])
    _write_jsonl(
        paths["attempts"],
        [attempt for result in task_results for attempt in result["attempts"]],
    )
    _write_jsonl(paths["predictions"], predictions)
    _write_json(paths["summary"], summary)
    _write_json(paths["cost_summary"], cost_summary)
    return {key: str(path) for key, path in paths.items()}


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def sha256_json(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256_text(text)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _score_files(
    *,
    workspace_path: Path,
    issue_terms: set[str],
    policy: LocalizationPolicy,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    if not workspace_path.exists():
        return scored
    for path in sorted(workspace_path.rglob("*"), key=lambda item: _relative_text(workspace_path, item)):
        if not path.is_file():
            continue
        rel = _relative_text(workspace_path, path)
        if _should_skip_file(rel, path, include_tests=policy.include_tests):
            continue
        text = _read_text_file(path, max_chars=max(policy.max_file_chars * 2, 8192))
        if text is None:
            continue
        excerpt = _redact_secrets(text[: policy.max_file_chars])
        score, reasons = _score_file(
            rel=rel,
            path=path,
            text=text,
            issue_terms=issue_terms,
        )
        if score <= 0:
            continue
        scored.append(
            {
                "path": rel,
                "score": score,
                "score_reasons": reasons,
                "excerpt": excerpt,
            }
        )
    return sorted(scored, key=lambda item: (-item["score"], item["path"]))


def _score_file(
    *,
    rel: str,
    path: Path,
    text: str,
    issue_terms: set[str],
) -> tuple[int, list[str]]:
    rel_lower = rel.lower()
    path_terms = _tokenize(rel)
    content_terms = _tokenize(text[:12000])
    symbol_terms = _extract_symbol_terms(text[:20000])
    path_hits = sorted(issue_terms & path_terms)
    symbol_hits = sorted(issue_terms & symbol_terms)
    content_hits = sorted(issue_terms & content_terms)

    score = 0
    reasons: list[str] = []
    source_bonus = SOURCE_SUFFIX_BONUS.get(path.suffix.lower(), 0)
    if source_bonus:
        score += source_bonus
        reasons.append(f"source_suffix:{path.suffix.lower()}:+{source_bonus}")
    if _is_test_path(rel):
        score += 8
        reasons.append("public_test_path:+8")
    if path_hits:
        delta = len(path_hits) * 9
        score += delta
        reasons.append(f"path_overlap:{','.join(path_hits[:8])}:+{delta}")
    if symbol_hits:
        delta = len(symbol_hits) * 14
        score += delta
        reasons.append(f"symbol_overlap:{','.join(symbol_hits[:8])}:+{delta}")
    if content_hits:
        delta = min(24, len(content_hits) * 2)
        score += delta
        reasons.append(f"content_overlap:{','.join(content_hits[:10])}:+{delta}")

    penalty = _boilerplate_penalty(rel_lower)
    if penalty:
        score -= penalty
        reasons.append(f"boilerplate_penalty:-{penalty}")
    if not reasons:
        reasons.append("no_repair_signal")
    return score, reasons


def _boilerplate_penalty(rel_lower: str) -> int:
    parts = set(rel_lower.replace("\\", "/").split("/"))
    basename = Path(rel_lower).name
    penalty = 0
    if parts & BOILERPLATE_DIR_PARTS:
        penalty += 28
    if any(marker in basename for marker in BOILERPLATE_FILE_MARKERS):
        penalty += 45
    if rel_lower.endswith(".ipynb"):
        penalty += 55
    if rel_lower.endswith((".rst", ".md", ".txt", ".yml", ".yaml")):
        penalty += 12
    return penalty


def _should_skip_file(rel: str, path: Path, *, include_tests: bool) -> bool:
    parts = set(rel.replace("\\", "/").split("/"))
    if parts & SKIP_DIRS:
        return True
    suffix = path.suffix.lower()
    if suffix in SKIP_SUFFIXES:
        return True
    if not include_tests and _is_test_path(rel):
        return True
    try:
        if path.stat().st_size > 1_500_000:
            return True
    except OSError:
        return True
    return False


def _is_test_path(rel: str) -> bool:
    lowered = rel.lower().replace("\\", "/")
    return (
        "/test" in lowered
        or lowered.startswith("test")
        or "_test." in lowered
        or "tests/" in lowered
    )


def _read_text_file(path: Path, *, max_chars: int) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data[: max_chars * 4].decode("utf-8", errors="replace")[:max_chars]


def _tokenize(text: str) -> set[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", str(text).lower())
    tokens: set[str] = set()
    for token in raw:
        for part in token.split("_"):
            if len(part) >= 3 and part not in COMMON_TERMS:
                tokens.add(part)
        if token not in COMMON_TERMS:
            tokens.add(token)
    return tokens


def _extract_symbol_terms(text: str) -> set[str]:
    symbols = re.findall(
        r"(?m)^\s*(?:def|class|async\s+def)\s+([A-Za-z_][A-Za-z0-9_]*)",
        text,
    )
    imports = re.findall(
        r"(?m)^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_\.]*)",
        text,
    )
    terms: set[str] = set()
    for item in [*symbols, *imports]:
        terms.update(_tokenize(item.replace(".", "_")))
    return terms


def _relative_text(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _redact_secrets(text: str) -> str:
    redacted = str(text)
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _copy_structured_edit_targets(
    workspace: Path,
    scratch: Path,
    edits: list[StructuredEdit],
) -> str:
    copied: set[Path] = set()
    for edit in edits:
        rel = _safe_relative_path(edit.file_path)
        if rel is None:
            return f"unsafe file path: {edit.file_path}"
        if rel in copied:
            continue
        source = (workspace / rel).resolve()
        try:
            source.relative_to(workspace.resolve())
        except ValueError:
            return f"path escapes workspace: {edit.file_path}"
        if not source.exists() or not source.is_file():
            continue
        target = scratch / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.add(rel)
    return ""


def _safe_relative_path(path_text: str) -> Path | None:
    normalized = str(path_text).replace("\\", "/").strip()
    if not normalized:
        return None
    path = Path(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _diff_structured_result(
    workspace: Path,
    scratch: Path,
    file_edits: list[dict[str, Any]],
) -> str:
    rel_paths = sorted(
        {
            str(item.get("relative_path") or item.get("path") or "")
            for item in file_edits
            if item.get("relative_path") or item.get("path")
        }
    )
    patches: list[str] = []
    for rel_text in rel_paths:
        rel = _safe_relative_path(rel_text)
        if rel is None:
            continue
        original = workspace / rel
        modified = scratch / rel
        if not original.exists() or not modified.exists():
            continue
        before = original.read_text(encoding="utf-8", errors="replace").splitlines()
        after = modified.read_text(encoding="utf-8", errors="replace").splitlines()
        if before == after:
            continue
        body = "\n".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{rel.as_posix()}",
                tofile=f"b/{rel.as_posix()}",
                lineterm="",
            )
        )
        if body:
            patches.append(f"diff --git a/{rel.as_posix()} b/{rel.as_posix()}\n{body}\n")
    return "".join(patches)


def _first_selected_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for attempt in attempts:
        if attempt.get("patch_apply_check") == "passed" and str(
            attempt.get("patch") or ""
        ).strip():
            return attempt
    return None


def _finish_reason(response: LLMResponse) -> str:
    raw = response.raw_response or {}
    choices = raw.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def _bounded_tail(text: str, limit: int = 1200) -> str:
    cleaned = _redact_secrets(str(text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
