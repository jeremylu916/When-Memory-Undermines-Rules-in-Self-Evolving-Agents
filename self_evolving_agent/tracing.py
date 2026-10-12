"""Trace records and JSONL trace writing."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from self_evolving_agent.agent import AgentResult
from self_evolving_agent.config import (
    PROMPT_MODULES,
    config_fingerprint,
    get_prompt_modules,
)

TRACE_VERSION = 1

REQUIRED_TRACE_KEYS = {
    "trace_version",
    "run_id",
    "timestamp",
    "task",
    "config",
    "prompts",
    "model",
    "workflow",
    "tool_policy",
    "workspace",
    "retrieved_context",
    "tool_calls",
    "llm_calls",
    "file_edits",
    "patch_diff",
    "test_commands",
    "command_results",
    "test_results",
    "output",
    "success",
    "execution_status",
    "metrics",
    "cost",
    "latency_ms",
}


def build_trace_record(
    *,
    run_id: str,
    config: dict[str, Any],
    config_path: str,
    result: AgentResult,
) -> dict[str, Any]:
    task = result.task
    security_valid = result.success if task.category == "security" else True
    raw_models = sorted(
        {
            str(call.get("raw_model"))
            for call in result.llm_calls
            if call.get("raw_model")
        }
    )
    llm_config = config.get("llm", {}) or {}
    record = {
        "trace_version": TRACE_VERSION,
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task": {
            "id": task.task_id,
            "split": task.split,
            "benchmark": task.benchmark,
            "category": task.category,
            "prompt": task.prompt,
            "issue_description": task.issue_description,
            "repo_path": task.repo_path,
            "repo_url": task.repo_url,
            "base_commit": task.base_commit,
            "setup_commands": list(task.setup_commands),
            "test_commands": list(task.test_commands),
            "has_test_patch": bool(task.test_patch),
            "metadata": task.metadata or {},
        },
        "config": {
            "name": config.get("name", "unnamed"),
            "version": config.get("version", 0),
            "path": config_path,
            "fingerprint": config_fingerprint(config),
        },
        "prompts": _render_prompt_modules(config, task.prompt),
        "model": {
            "provider": llm_config.get("provider", "mock"),
            "name": llm_config.get(
                "model", config.get("model", {}).get("name", "mock-rule-agent")
            ),
            "requested_model": llm_config.get("requested_model")
            or llm_config.get("model_family")
            or llm_config.get("model")
            or config.get("model", {}).get("name", "mock-rule-agent"),
            "deployment": llm_config.get("deployment")
            or llm_config.get("deployment_id")
            or llm_config.get("model")
            or config.get("model", {}).get("name", "mock-rule-agent"),
            "reasoning_effort": llm_config.get("reasoning_effort"),
            "returned_raw_model": raw_models[0] if len(raw_models) == 1 else None,
            "returned_raw_models": raw_models,
            "request_budget": {
                "max_completion_tokens": llm_config.get(
                    "max_completion_tokens"
                ),
                "timeout_s": llm_config.get("timeout_s"),
                "timeout_retries": llm_config.get("timeout_retries"),
            },
        },
        "workflow": config.get("workflow", {}),
        "tool_policy": config.get("tool_policy", {}),
        "workspace": result.workspace,
        "retrieved_context": result.retrieved_context,
        "tool_calls": result.tool_calls,
        "llm_calls": result.llm_calls,
        "file_edits": result.file_edits,
        "patch_diff": result.patch_diff,
        "test_commands": result.test_commands,
        "command_results": result.command_results,
        "test_results": result.test_results,
        "output": result.output,
        "success": result.success,
        "execution_status": result.execution_status
        or ("success" if result.success else "patch_failed"),
        "metrics": {
            "correct": 1.0 if result.success else 0.0,
            "security_valid": 1.0 if security_valid else 0.0,
            "test_pass": 1.0 if result.success else 0.0,
            "environment_failed": 1.0
            if result.execution_status == "environment_failed"
            else 0.0,
            "test_failed": 1.0 if result.execution_status == "test_failed" else 0.0,
            "patch_failed": 1.0 if result.execution_status == "patch_failed" else 0.0,
        },
        "cost": result.cost,
        "latency_ms": result.latency_ms,
        "intermediate_steps": result.intermediate_steps,
    }
    validate_trace_record(record)
    return record


def _render_prompt_modules(config: dict[str, Any], task_prompt: str) -> dict[str, str]:
    """Render the six prompt modules, substituting the task prompt placeholder."""
    rendered: dict[str, str] = {}
    for module, text in get_prompt_modules(config).items():
        if "{task_prompt}" in text:
            text = text.replace("{task_prompt}", task_prompt)
        rendered[module] = text
    return rendered


def validate_trace_record(record: dict[str, Any]) -> None:
    missing = REQUIRED_TRACE_KEYS - set(record)
    if missing:
        raise ValueError(f"Trace record missing required keys: {sorted(missing)}")
    if record["trace_version"] != TRACE_VERSION:
        raise ValueError(f"Unsupported trace version: {record['trace_version']}")
    if not isinstance(record["success"], bool):
        raise ValueError("Trace 'success' must be a boolean")
    if not isinstance(record["task"], dict) or "id" not in record["task"]:
        raise ValueError("Trace 'task' must include an id")
    if not isinstance(record["metrics"], dict):
        raise ValueError("Trace 'metrics' must be a mapping")
    if not isinstance(record["command_results"], list):
        raise ValueError("Trace 'command_results' must be a list")
    if not isinstance(record["llm_calls"], list):
        raise ValueError("Trace 'llm_calls' must be a list")
    if not isinstance(record["prompts"], dict):
        raise ValueError("Trace 'prompts' must be a mapping")
    missing_modules = set(PROMPT_MODULES) - set(record["prompts"])
    if missing_modules:
        raise ValueError(f"Trace 'prompts' missing modules: {sorted(missing_modules)}")
    if record["execution_status"] not in {
        "success",
        "environment_failed",
        "test_failed",
        "patch_failed",
    }:
        raise ValueError(f"Unknown execution status: {record['execution_status']}")


class TraceLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_records(self, records: list[dict[str, Any]]) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            for record in records:
                validate_trace_record(record)
                handle.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                validate_trace_record(record)
                records.append(record)
    return records
