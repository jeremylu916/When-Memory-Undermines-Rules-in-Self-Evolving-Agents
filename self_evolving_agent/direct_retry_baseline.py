"""B0b StaticContext DirectRetry baseline.

This module is intentionally independent from the self-evolving agent loop.
It reuses only low-level infrastructure and keeps generation free of memory,
critic, optimizer, promotion, root-cause, and validation-feedback mechanisms.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Protocol

from self_evolving_agent.benchmarks import BenchmarkTask
from self_evolving_agent.llm import LLMResponse, LLMUsage


B0B_RUN_CONFIG_SCHEMA_VERSION = "b0b.run_config.v1"
B0B_STATIC_CONTEXT_SCHEMA_VERSION = "b0b.static_context.v1"
B0B_ATTEMPT_SCHEMA_VERSION = "b0b.attempt.v1"
B0B_SUMMARY_SCHEMA_VERSION = "b0b.direct_retry_summary.v1"
B0B_COST_SUMMARY_SCHEMA_VERSION = "b0b.cost_summary.v1"

PATCH_APPLY_CHECKS = ("empty", "malformed", "failed", "passed")

DEFAULT_CONTEXT_MAX_FILES = 8
DEFAULT_CONTEXT_MAX_FILE_CHARS = 4000
DEFAULT_CONTEXT_MAX_TOTAL_CHARS = 24000
DEFAULT_APPLY_TIMEOUT_S = 30

LEAKAGE_SENSITIVE_TASK_FIELDS = (
    "test_patch",
    "expected_output",
)
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

SKIP_DIR_NAMES = {
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

SKIP_FILE_SUFFIXES = {
    ".7z",
    ".a",
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

COMMON_ISSUE_TERMS = {
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
class StaticContextPolicy:
    max_files: int = DEFAULT_CONTEXT_MAX_FILES
    max_file_chars: int = DEFAULT_CONTEXT_MAX_FILE_CHARS
    max_total_chars: int = DEFAULT_CONTEXT_MAX_TOTAL_CHARS

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class PatchApplyCheck:
    status: str
    error_tail: str = ""
    exit_code: int | None = None
    duration_ms: int = 0


class MockDirectRetryLLM:
    """Deterministic fake chat client for no-API tests and smoke runs."""

    def __init__(self, responses: list[dict[str, Any]], *, model: str = "mock"):
        self.responses = list(responses)
        self.model = model
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]]) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        if not self.responses:
            raise RuntimeError("mock response queue is empty")
        payload = dict(self.responses.pop(0))
        usage = payload.get("usage") or {}
        if not usage:
            usage = {
                "prompt_tokens": int(payload.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(payload.get("completion_tokens", 0) or 0),
                "total_tokens": int(payload.get("total_tokens", 0) or 0),
                "cost_usd": float(payload.get("cost_usd", 0.0) or 0.0),
            }
        total_tokens = int(
            usage.get("total_tokens")
            or int(usage.get("prompt_tokens", 0) or 0)
            + int(usage.get("completion_tokens", 0) or 0)
        )
        llm_usage = LLMUsage(
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            total_tokens=total_tokens,
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
        )
        model = str(payload.get("model") or self.model)
        finish_reason = str(payload.get("finish_reason") or "stop")
        return LLMResponse(
            content=str(payload.get("content", "")),
            raw_response={
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": str(payload.get("content", ""))},
                    }
                ]
            },
            usage=llm_usage,
            model=model,
        )


def build_static_context(
    task: BenchmarkTask,
    workspace: str | Path,
    *,
    policy: StaticContextPolicy | None = None,
) -> dict[str, Any]:
    """Build a deterministic repository context from issue text and files only."""

    policy = policy or StaticContextPolicy()
    workspace_path = Path(workspace)
    issue_text = task.issue_description or task.prompt
    issue_terms = _tokenize(issue_text)
    safe_metadata, leakage_audit = sanitize_task_metadata_for_b0b(task)
    scored_files = _score_repository_files(
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

    context_text = render_static_context_text(
        task=task,
        safe_metadata=safe_metadata,
        selected_files=selected_files,
    )
    context_record = {
        "schema_version": B0B_STATIC_CONTEXT_SCHEMA_VERSION,
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "repo": str(safe_metadata.get("repo") or ""),
        "base_commit": task.base_commit or "",
        "static_context_sha256": sha256_text(context_text),
        "context_policy": policy.to_dict(),
        "selected_files": selected_files,
        "omitted_file_count": max(0, len(scored_files) - len(selected_files)),
        "leakage_audit": leakage_audit,
        "context_chars": len(context_text),
        "context_text": context_text,
    }
    return context_record


def sanitize_task_metadata_for_b0b(
    task: BenchmarkTask,
) -> tuple[dict[str, Any], dict[str, Any]]:
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

    leakage_audit = {
        "blocked_task_fields": sorted(blocked_task_fields),
        "blocked_metadata_fields": sorted(blocked_metadata_fields),
        "benchmark_test_patch_visible": False,
        "gold_patch_visible": False,
        "official_feedback_visible": False,
    }
    return safe_metadata, leakage_audit


def render_static_context_text(
    *,
    task: BenchmarkTask,
    safe_metadata: dict[str, Any],
    selected_files: list[dict[str, Any]],
) -> str:
    issue_text = _redact_secrets(task.issue_description or task.prompt).strip()
    repo = str(safe_metadata.get("repo") or "")
    lines = [
        "B0b static repository context.",
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
    lines.append("Selected repository excerpts:")
    if not selected_files:
        lines.append("(no text files selected)")
    for item in selected_files:
        lines.extend(
            [
                "",
                f"### {item['path']}",
                str(item["excerpt"]).rstrip(),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_direct_retry_messages(
    task: BenchmarkTask,
    static_context: dict[str, Any],
) -> list[dict[str, str]]:
    system = (
        "You are a direct software repair baseline. Use only the provided "
        "static issue and repository context. Do not assume hidden tests, gold "
        "patches, previous attempts, memory, or feedback. Return one minimal "
        "unified diff. If you cannot produce a patch, return NO_PATCH."
    )
    user = (
        f"{static_context['context_text']}\n"
        "Write a minimal unified diff that fixes the problem. "
        "Do not include explanations outside the diff."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def prompt_sha256(messages: list[dict[str, str]]) -> str:
    return sha256_json(messages)


def run_direct_retry_task(
    *,
    task: BenchmarkTask,
    workspace: str | Path,
    llm: ChatClient,
    attempts: int = 3,
    context_policy: StaticContextPolicy | None = None,
    apply_timeout_s: int = DEFAULT_APPLY_TIMEOUT_S,
    temperature: float | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    static_context = build_static_context(
        task,
        workspace,
        policy=context_policy or StaticContextPolicy(),
    )
    messages = build_direct_retry_messages(task, static_context)
    prompt_hash = prompt_sha256(messages)
    context_hash = str(static_context["static_context_sha256"])

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

        patch_text = extract_unified_diff(content)
        apply_check = check_patch_applies(
            workspace=workspace,
            patch_text=patch_text,
            timeout_s=apply_timeout_s,
        )
        if error_tail and not apply_check.error_tail:
            apply_error_tail = error_tail
        else:
            apply_error_tail = apply_check.error_tail
        attempt_records.append(
            {
                "schema_version": B0B_ATTEMPT_SCHEMA_VERSION,
                "task_id": task.task_id,
                "attempt_index": attempt_index,
                "prompt_sha256": prompt_hash,
                "static_context_sha256": context_hash,
                "model": model,
                "temperature": temperature,
                "seed": attempt_seed,
                "finish_reason": finish_reason,
                "response_chars": len(content),
                "patch_chars": len(patch_text),
                "patch": patch_text,
                "patch_apply_check": apply_check.status,
                "apply_error_tail": apply_error_tail,
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
    prediction = {
        "instance_id": task.task_id,
        "model_patch": selected_patch,
    }
    return {
        "task_id": task.task_id,
        "static_context": static_context,
        "messages": messages,
        "attempts": attempt_records,
        "prediction": prediction,
        "selected_attempt_index": selected_attempt_index,
    }


def extract_unified_diff(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped or stripped == "NO_PATCH":
        return ""
    if "```" in stripped:
        for match in re.finditer(r"```(?:diff|patch)?\s*\n([\s\S]*?)```", stripped):
            candidate = match.group(1).strip()
            if _looks_like_unified_diff(candidate):
                return _ensure_trailing_newline(_trim_to_diff_start(candidate))
    if _looks_like_unified_diff(stripped):
        return _ensure_trailing_newline(_trim_to_diff_start(stripped))
    return ""


def check_patch_applies(
    *,
    workspace: str | Path,
    patch_text: str,
    timeout_s: int = DEFAULT_APPLY_TIMEOUT_S,
) -> PatchApplyCheck:
    if not str(patch_text or "").strip():
        return PatchApplyCheck(status="empty")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=str(Path(workspace)),
            input=_ensure_trailing_newline(patch_text),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        duration_ms = max(1, int((time.perf_counter() - started) * 1000))
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_timeout_stream(exc.stdout)
        stderr = _decode_timeout_stream(exc.stderr)
        return PatchApplyCheck(
            status="failed",
            error_tail=_bounded_tail(stdout + "\n" + stderr),
            exit_code=None,
            duration_ms=max(1, int((time.perf_counter() - started) * 1000)),
        )

    if completed.returncode == 0:
        return PatchApplyCheck(
            status="passed",
            exit_code=0,
            duration_ms=duration_ms,
        )
    combined = f"{completed.stdout}\n{completed.stderr}"
    status = "malformed" if _is_malformed_patch_error(combined) else "failed"
    return PatchApplyCheck(
        status=status,
        error_tail=_bounded_tail(combined),
        exit_code=completed.returncode,
        duration_ms=duration_ms,
    )


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


def build_direct_retry_summary(
    *,
    run_id: str,
    task_results: list[dict[str, Any]],
    model_name_or_path: str,
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
        "schema_version": B0B_SUMMARY_SCHEMA_VERSION,
        "created_at": created_at or utc_now(),
        "run_id": run_id,
        "model_name_or_path": model_name_or_path,
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


def build_cost_summary(
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
        "schema_version": B0B_COST_SUMMARY_SCHEMA_VERSION,
        "created_at": created_at or utc_now(),
        "run_id": run_id,
        "totals": totals,
        "per_task": per_task,
    }


def write_direct_retry_artifacts(
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
        "static_context": root / "static_context.jsonl",
        "attempts": root / "attempts.jsonl",
        "predictions": root / "predictions.jsonl",
        "summary": root / "direct_retry_summary.json",
        "cost_summary": root / "cost_summary.json",
    }
    _write_json(paths["run_config"], run_config)
    _write_jsonl(
        paths["static_context"],
        [result["static_context"] for result in task_results],
    )
    _write_jsonl(
        paths["attempts"],
        [attempt for result in task_results for attempt in result["attempts"]],
    )
    _write_jsonl(paths["predictions"], predictions)
    _write_json(paths["summary"], summary)
    _write_json(paths["cost_summary"], cost_summary)
    return {key: str(path) for key, path in paths.items()}


def load_mock_response_jsonl(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"Mock response line {line_number} must be an object")
            task_id = str(record.get("task_id") or "")
            if not task_id:
                raise ValueError(f"Mock response line {line_number} missing task_id")
            grouped.setdefault(task_id, []).append(record)
    return grouped


def build_run_config_record(
    *,
    run_id: str,
    model_name_or_path: str,
    attempts: int,
    selected_task_ids: list[str],
    context_policy: StaticContextPolicy,
    config_audit: dict[str, Any],
    workspace_source: str,
    dry_run: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": B0B_RUN_CONFIG_SCHEMA_VERSION,
        "created_at": created_at or utc_now(),
        "run_id": run_id,
        "baseline": "B0b StaticContext DirectRetry",
        "model_name_or_path": model_name_or_path,
        "attempts": attempts,
        "selected_task_ids": list(selected_task_ids),
        "context_policy": context_policy.to_dict(),
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
            "validation_repair",
            "attempt_feedback",
        ],
    }


def config_audit_view(config: dict[str, Any]) -> dict[str, Any]:
    llm = dict((config.get("llm") or {}))
    return {
        "llm": {
            "enabled": bool(llm.get("enabled", False)),
            "provider": str(llm.get("provider", "")),
            "base_url_configured": bool(llm.get("base_url")),
            "api_key_env": str(llm.get("api_key_env") or "OPENAI_API_KEY"),
            "api_key_inline_configured": bool(llm.get("api_key")),
            "model": str(llm.get("model", "")),
            "requested_model": str(
                llm.get("requested_model") or llm.get("model_family") or llm.get("model") or ""
            ),
            "deployment_configured": bool(llm.get("deployment") or llm.get("deployment_id")),
            "temperature": llm.get("temperature"),
            "seed": llm.get("seed"),
            "max_completion_tokens": llm.get("max_completion_tokens"),
        }
    }


def model_identity_from_config(
    config: dict[str, Any],
    override: str | None = None,
) -> str:
    if override:
        return str(override)
    llm = config.get("llm") or {}
    return str(
        llm.get("requested_model")
        or llm.get("model_family")
        or llm.get("model")
        or "direct-retry-baseline"
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def sha256_json(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256_text(text)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _score_repository_files(
    *,
    workspace_path: Path,
    issue_terms: set[str],
    policy: StaticContextPolicy,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    if not workspace_path.exists():
        return scored
    for path in sorted(workspace_path.rglob("*"), key=lambda item: _relative_text(workspace_path, item)):
        if not path.is_file():
            continue
        rel = _relative_text(workspace_path, path)
        if _should_skip_file(rel, path):
            continue
        text = _read_text_file(path, max_chars=max(policy.max_file_chars * 2, 8192))
        if text is None:
            continue
        excerpt = _redact_secrets(text[: policy.max_file_chars])
        path_terms = _tokenize(rel)
        content_terms = _tokenize(text[: max(policy.max_file_chars, 1)])
        path_hits = sorted(issue_terms & path_terms)
        content_hits = sorted(issue_terms & content_terms)
        score = len(path_hits) * 5 + len(content_hits)
        score_reasons: list[str] = []
        if path_hits:
            score_reasons.append(f"path_term_overlap:{','.join(path_hits[:8])}")
        if content_hits:
            score_reasons.append(f"content_term_overlap:{','.join(content_hits[:8])}")
        if not score_reasons:
            score_reasons.append("deterministic_fallback")
        scored.append(
            {
                "path": rel,
                "score": score,
                "score_reasons": score_reasons,
                "excerpt": excerpt,
            }
        )
    return sorted(scored, key=lambda item: (-int(item["score"]), str(item["path"])))


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", str(text).lower())
        if token not in COMMON_ISSUE_TERMS
    }


def _should_skip_file(rel: str, path: Path) -> bool:
    parts = set(Path(rel).parts)
    if parts & SKIP_DIR_NAMES:
        return True
    if path.suffix.lower() in SKIP_FILE_SUFFIXES:
        return True
    try:
        if path.stat().st_size > 512_000:
            return True
    except OSError:
        return True
    return False


def _read_text_file(path: Path, *, max_chars: int) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    try:
        return data[: max_chars * 4].decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data[: max_chars * 4].decode("latin-1")
        except UnicodeDecodeError:
            return None


def _relative_text(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _looks_like_unified_diff(text: str) -> bool:
    candidate = str(text or "")
    return (
        "diff --git " in candidate
        and re.search(r"(?m)^---\s+", candidate) is not None
        and re.search(r"(?m)^\+\+\+\s+", candidate) is not None
    )


def _trim_to_diff_start(text: str) -> str:
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- "):
            return "\n".join(lines[index:])
    return str(text or "")


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _is_malformed_patch_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(
        marker in text
        for marker in (
            "corrupt patch",
            "no valid patches",
            "unrecognized input",
            "patch fragment without header",
            "patch with only garbage",
            "malformed",
        )
    )


def _finish_reason(response: LLMResponse) -> str:
    raw = response.raw_response or {}
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict) and first.get("finish_reason") is not None:
            return str(first["finish_reason"])
    return ""


def _first_selected_attempt(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        if record.get("patch_apply_check") == "passed" and str(record.get("patch") or "").strip():
            return record
    return None


def _bounded_tail(text: str, *, max_chars: int = 1200) -> str:
    redacted = _redact_secrets(str(text or ""))
    if len(redacted) <= max_chars:
        return redacted
    return redacted[-max_chars:]


def _redact_secrets(text: str) -> str:
    redacted = str(text or "")
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _decode_timeout_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


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
