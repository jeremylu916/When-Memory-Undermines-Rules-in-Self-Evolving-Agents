"""Deterministic mock execution agent."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from self_evolving_agent.benchmarks import BenchmarkTask
from self_evolving_agent.config import PROMPT_MODULES, get_prompt_modules
from self_evolving_agent.execution import apply_patch_text, collect_patch_diff
from self_evolving_agent.llm import LLMClient, LLMRequestError, LLMResponse
from self_evolving_agent.patch_materializer import (
    StructuredEdit,
    apply_structured_edits,
    extract_structured_edits,
)
from self_evolving_agent.validation import ValidationRun, run_image_validation

LLM_CONTENT_PREVIEW_CHARS = 4000
APPLY_FAILURE_SCHEMA_VERSION = "m6.3d.apply_failure.v1"
LOCALIZER_DEFAULT_CONTEXT_BUDGET_CHARS = 10000
LOCALIZER_TEST_PATCH_BUDGET_CHARS = 1600
LOCALIZER_TEST_FILE_BUDGET_CHARS = 2400
LOCALIZER_SOURCE_FILE_BUDGET_CHARS = 3600
LOCALIZER_LINE_ANCHOR_BUDGET_CHARS = 4000
LOCALIZER_LOW_SIGNAL_TERMS = {
    "api",
    "app",
    "config",
    "file",
    "flask",
    "from",
    "issue",
    "load",
    "mode",
    "object",
    "python",
    "resolve",
    "support",
    "test",
    "tests",
    "true",
    "false",
    "value",
}

_APPLY_FAILURE_TYPES = {
    "structured_edit_anchor_mismatch",
    "structured_edit_parse_failed",
    "git_apply_failure",
    "validation_rejected",
    "unknown_apply_failure",
}

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


@dataclass(frozen=True)
class AgentResult:
    task: BenchmarkTask
    output: st
    success: bool
    intermediate_steps: list[str]
    retrieved_context: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    file_edits: list[dict[str, Any]]
    test_commands: list[str]
    test_results: list[dict[str, Any]]
    latency_ms: int
    cost: float
    patch_diff: str = ""
    command_results: list[dict[str, Any]] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)
    execution_status: str | None = None
    environment_error: str | None = None
    llm_calls: list[dict[str, Any]] = field(default_factory=list)


class ExecutionAgent:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.workflow = config.get("workflow", {})
        self.agent_mode = self.workflow.get("agent_mode", "gold-edit")
        self.prompt_modules = get_prompt_modules(config)
        self.llm = LLMClient(config)

    def solve(self, task: BenchmarkTask, workspace_path: Path | None = None) -> AgentResult:
        started = time.perf_counter()
        steps = [
            "read_task",
            f"apply_strategy:{self.workflow.get('strategy', 'keyword')}",
        ]
        self._record_prompt_module_use(task, steps)
        file_edits: list[dict[str, Any]] = []
        llm_calls: list[dict[str, Any]] = []
        retrieved_context: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        command_results: list[dict[str, Any]] = []
        validation_test_results: list[dict[str, Any]] = []
        validation_test_commands: list[str] = []

        if task.category == "security":
            output = self._solve_security(task, steps)
        else:
            output = self._solve_software(
                task,
                steps,
                workspace_path,
                file_edits,
                llm_calls,
                retrieved_context,
                tool_calls,
            )

        if (
            output == "patch_applied"
            and workspace_path is not None
            and task.category == "software"
        ):
            self._validate_and_repair(
                task=task,
                workspace_path=workspace_path,
                steps=steps,
                file_edits=file_edits,
                llm_calls=llm_calls,
                retrieved_context=retrieved_context,
                tool_calls=tool_calls,
                command_results=command_results,
                test_results=validation_test_results,
                test_commands=validation_test_commands,
            )

        success = output == task.expected_output
        if self.workflow.get("verify_before_submit", False):
            steps.append("verify_output_against_mock_oracle")

        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        mock_test_results = []
        mock_test_commands = []
        if not task.test_commands:
            mock_test_commands = ["mock_oracle_check"]
            mock_test_results = [
                {
                    "name": "mock_oracle_check",
                    "passed": success,
                    "expected": task.expected_output,
                    "actual": output,
                }
            ]

        return AgentResult(
            task=task,
            output=output,
            success=success,
            intermediate_steps=steps,
            retrieved_context=retrieved_context,
            tool_calls=tool_calls,
            file_edits=file_edits,
            test_commands=validation_test_commands or mock_test_commands,
            test_results=validation_test_results or mock_test_results,
            latency_ms=latency_ms,
            cost=sum(float(call.get("cost_usd", 0.0)) for call in llm_calls),
            command_results=command_results,
            llm_calls=llm_calls,
        )

    def _record_prompt_module_use(self, task: BenchmarkTask, steps: list[str]) -> None:
        """Record, in module order, which prompt modules the agent consults.

        The mock agent does not call an LLM, but consuming the modules in orde
        keeps the trace faithful to the six-module prompt structure so the
        critic/optimizer can attribute behavior to specific modules later.
        """
        for module in PROMPT_MODULES:
            if module == "security_review" and task.category != "security":
                continue
            steps.append(f"use_prompt:{module}")

    def _solve_software(
        self,
        task: BenchmarkTask,
        steps: list[str],
        workspace_path: Path | None,
        file_edits: list[dict[str, Any]],
        llm_calls: list[dict[str, Any]],
        retrieved_context: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> str:
        if workspace_path is not None and self.agent_mode == "self-edit":
            return self._self_edit_software(
                task,
                steps,
                workspace_path,
                file_edits,
                llm_calls,
                retrieved_context,
                tool_calls,
            )

        metadata = task.metadata or {}
        if workspace_path is not None and metadata.get("gold_patch"):
            return self._apply_gold_patch(task, steps, workspace_path, file_edits)
        if workspace_path is not None and metadata.get("patch_file"):
            return self._apply_metadata_patch(task, steps, workspace_path, file_edits)

        steps.append("generate_mock_patch")
        return task.expected_output

    def _apply_gold_patch(
        self,
        task: BenchmarkTask,
        steps: list[str],
        workspace_path: Path,
        file_edits: list[dict[str, Any]],
    ) -> str:
        metadata = task.metadata or {}
        patch_text = str(metadata.get("gold_patch", ""))
        steps.append("apply_gold_patch")
        result = apply_patch_text(
            workspace=workspace_path,
            patch_text=patch_text,
            timeout_s=task.command_timeout_s or 120,
            phase="gold_patch",
        )
        if result.exit_code != 0 or result.timed_out:
            steps.append("gold_patch_apply_failed")
            return "gold_patch_apply_failed"
        file_edits.append(
            {
                "path": str(workspace_path),
                "relative_path": ".",
                "action": "apply_patch",
                "patch_type": "gold_patch",
            }
        )
        steps.append("gold_patch_applied")
        return "patch_applied"

    def _self_edit_software(
        self,
        task: BenchmarkTask,
        steps: list[str],
        workspace_path: Path,
        file_edits: list[dict[str, Any]],
        llm_calls: list[dict[str, Any]],
        retrieved_context: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> str:
        if self.llm.is_configured():
            llm_result = self._self_edit_software_with_llm(
                task,
                steps,
                workspace_path,
                file_edits,
                llm_calls,
                retrieved_context,
                tool_calls,
            )
            if llm_result is not None:
                return llm_result
            if self.workflow.get("disable_rule_fallback", False):
                steps.append("self_edit:rule_fallback_disabled")
                return "llm_patch_failed"

        steps.append("self_edit:inspect_issue")
        issue_text = f"{task.prompt}\n{task.issue_description}".lower()

        if "from_file" in issue_text and ("text=false" in issue_text or "binary" in issue_text):
            return self._self_edit_flask_config_from_file(steps, workspace_path, file_edits)

        if "add_one" in issue_text:
            return self._replace_in_first_matching_file(
                steps=steps,
                workspace_path=workspace_path,
                file_edits=file_edits,
                relative_paths=["math_utils.py"],
                find_text="return x - 1",
                replace_text="return x + 1",
            )

        if "double" in issue_text:
            return self._replace_in_first_matching_file(
                steps=steps,
                workspace_path=workspace_path,
                file_edits=file_edits,
                relative_paths=["math_utils.py"],
                find_text="return x + 2",
                replace_text="return x * 2",
            )

        steps.append("self_edit:no_strategy_matched")
        return "self_edit_no_strategy"

    def _self_edit_software_with_llm(
        self,
        task: BenchmarkTask,
        steps: list[str],
        workspace_path: Path,
        file_edits: list[dict[str, Any]],
        llm_calls: list[dict[str, Any]],
        retrieved_context: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> str | None:
        steps.append("self_edit:agentless_localize")
        context = _localize_agentless_context(task, workspace_path)
        retrieved_context.extend(context)
        tool_calls.append(
            {
                "name": "agentless_localize",
                "arguments": {
                    "workspace": str(workspace_path),
                    "context_count": len(context),
                    "context_chars": sum(
                        len(str(item.get("content", ""))) for item in context
                    ),
                },
                "result": [
                    {
                        "path": item["path"],
                        "kind": item.get("kind"),
                        "score": item.get("score"),
                        "content_chars": len(str(item.get("content", ""))),
                        "high_signal_terms": item.get("high_signal_terms", []),
                        "score_reasons": item.get("score_reasons", []),
                        "context_policy": item.get("context_policy"),
                    }
                    for item in context
                ],
            }
        )
        if self._patch_materializer_mode() == "structured-edit":
            return self._self_edit_software_with_structured_edits(
                task,
                steps,
                workspace_path,
                file_edits,
                llm_calls,
                context,
                retrieved_context,
                tool_calls,
            )

        steps.append("self_edit:llm_plan_patch")
        messages = self._build_patch_messages(task, workspace_path, context)
        request_budget = self._initial_completion_budget()
        try:
            response = self.llm.chat(
                messages,
                max_completion_tokens=request_budget,
            )
        except Exception as exc:
            steps.append("self_edit:llm_error")
            llm_calls.append(
                {
                    "provider": self.llm.provider,
                    "model": self.llm.model,
                    "error": str(exc),
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                }
            )
            return None

        patch_text = _extract_unified_diff(response.content)
        llm_calls.append(
            self._llm_call_record(response, attempt="initial", patch_text=patch_text)
        )
        length_retry_count = 0
        while (
            not patch_text.strip()
            and llm_calls[-1].get("failure_type") == "length_exhausted"
            and length_retry_count < self.llm.length_retries
        ):
            next_budget = self.llm.next_completion_budget(request_budget)
            if next_budget is None:
                break
            length_retry_count += 1
            request_budget = next_budget
            steps.append("self_edit:llm_retry_larger_budget")
            retry_messages = messages + [
                {
                    "role": "user",
                    "content": (
                        "The previous response exhausted its completion budget before "
                        "producing a visible patch. Use the additional budget only to "
                        "return a concise git unified diff starting with 'diff --git'. "
                        "Do not include analysis, markdown fences, or prose."
                    ),
                }
            ]
            try:
                response = self.llm.chat(
                    retry_messages,
                    max_completion_tokens=request_budget,
                )
            except Exception:
                steps.append("self_edit:llm_length_retry_failed")
                return None
            patch_text = _extract_unified_diff(response.content)
            llm_calls.append(
                self._llm_call_record(
                    response,
                    attempt="length_retry",
                    patch_text=patch_text,
                )
            )
        if not patch_text.strip():
            steps.append("self_edit:llm_retry_patch_only")
            retry_messages = messages + [
                {"role": "assistant", "content": response.content or ""},
                {
                    "role": "user",
                    "content": (
                        "Your previous answer did not contain an applyable unified diff.\n"
                        "Return only a valid git unified diff starting with 'diff --git'.\n"
                        "Do not include markdown fences, explanations, or prose."
                    ),
                },
            ]
            try:
                retry_response = self.llm.chat(
                    retry_messages,
                    max_completion_tokens=request_budget,
                )
            except Exception:
                steps.append("self_edit:llm_no_patch")
                return None
            patch_text = _extract_unified_diff(retry_response.content)
            llm_calls.append(
                self._llm_call_record(
                    retry_response,
                    attempt="patch_only_retry",
                    patch_text=patch_text,
                )
            )
            if not patch_text.strip():
                steps.append("self_edit:llm_no_patch")
                return None

        result = apply_patch_text(
            workspace=workspace_path,
            patch_text=patch_text,
            timeout_s=task.command_timeout_s or 120,
            phase="llm_patch",
        )
        if result.exit_code != 0 or result.timed_out:
            steps.append("self_edit:llm_patch_apply_failed")
            failed_apply_index = len(llm_calls) - 1
            llm_calls[failed_apply_index]["apply_failure"] = (
                _git_apply_failure_envelope(
                    result,
                    patch_text=patch_text,
                    retry_attempted=True,
                    retry_failed=False,
                )
            )
            retry_messages = messages + [
                {"role": "assistant", "content": patch_text},
                {
                    "role": "user",
                    "content": (
                        "The unified diff failed to apply with git apply.\n"
                        f"Exit code: {result.exit_code}; timed out: {result.timed_out}\n"
                        f"stdout:\n{result.stdout[-2000:]}\n"
                        f"stderr:\n{result.stderr[-4000:]}\n\n"
                        "Return only a corrected git unified diff starting with 'diff --git'."
                    ),
                },
            ]
            try:
                retry_response = self.llm.chat(retry_messages)
            except Exception:
                steps.append("self_edit:llm_patch_apply_retry_failed")
                _mark_apply_failure_retry_failed(llm_calls[failed_apply_index])
                return None
            patch_text = _extract_unified_diff(retry_response.content)
            llm_calls.append(
                self._llm_call_record(
                    retry_response,
                    attempt="apply_failure_retry",
                    patch_text=patch_text,
                )
            )
            if not patch_text.strip():
                steps.append("self_edit:llm_patch_apply_retry_no_patch")
                _mark_apply_failure_retry_failed(llm_calls[failed_apply_index])
                return None
            result = apply_patch_text(
                workspace=workspace_path,
                patch_text=patch_text,
                timeout_s=task.command_timeout_s or 120,
                phase="llm_patch_retry",
            )
            if result.exit_code != 0 or result.timed_out:
                steps.append("self_edit:llm_patch_apply_retry_failed")
                _mark_apply_failure_retry_failed(llm_calls[failed_apply_index])
                llm_calls[-1]["apply_failure"] = _git_apply_failure_envelope(
                    result,
                    patch_text=patch_text,
                    retry_attempted=True,
                    retry_failed=True,
                )
                return None

        file_edits.append(
            {
                "path": str(workspace_path),
                "relative_path": ".",
                "action": "apply_patch",
                "patch_type": "llm_patch",
                "source": "llm",
            }
        )
        steps.append("self_edit:llm_patch_applied")
        return "patch_applied"

    def _self_edit_software_with_structured_edits(
        self,
        task: BenchmarkTask,
        steps: list[str],
        workspace_path: Path,
        file_edits: list[dict[str, Any]],
        llm_calls: list[dict[str, Any]],
        context: list[dict[str, Any]],
        retrieved_context: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> str | None:
        steps.append("self_edit:llm_plan_structured_edit")
        messages = self._build_structured_edit_messages(task, workspace_path, context)
        request_budget = self._initial_completion_budget()
        try:
            response = self.llm.chat(
                messages,
                max_completion_tokens=request_budget,
            )
        except Exception as exc:
            steps.append("self_edit:llm_error")
            llm_calls.append(
                self._llm_error_record(
                    exc,
                    attempt="initial",
                    request_max_completion_tokens=request_budget,
                )
            )
            return None

        edits = extract_structured_edits(response.content)
        llm_calls.append(
            self._structured_edit_call_record(
                response,
                attempt="initial",
                edits=edits,
            )
        )
        length_retry_count = 0
        while (
            not edits
            and llm_calls[-1].get("failure_type") == "length_exhausted"
            and length_retry_count < self.llm.length_retries
        ):
            next_budget = self.llm.next_completion_budget(request_budget)
            if next_budget is None:
                break
            length_retry_count += 1
            request_budget = next_budget
            steps.append("self_edit:structured_edit_retry_larger_budget")
            retry_messages = messages + [
                {
                    "role": "user",
                    "content": (
                        "The previous response exhausted its completion budget before "
                        "producing visible output. Use the additional budget efficiently. "
                        "Return only concise JSON matching the edits schema, with no analysis."
                    ),
                }
            ]
            try:
                response = self.llm.chat(
                    retry_messages,
                    max_completion_tokens=request_budget,
                )
            except Exception as exc:
                steps.append("self_edit:structured_edit_budget_retry_failed")
                llm_calls.append(
                    self._llm_error_record(
                        exc,
                        attempt="structured_length_retry",
                        request_max_completion_tokens=request_budget,
                    )
                )
                return None
            edits = extract_structured_edits(response.content)
            llm_calls.append(
                self._structured_edit_call_record(
                    response,
                    attempt="structured_length_retry",
                    edits=edits,
                )
            )

        if not edits:
            if llm_calls[-1].get("failure_type") == "length_exhausted":
                steps.append("self_edit:structured_edit_budget_exhausted")
                return None
            steps.append("self_edit:structured_edit_retry_json_only")
            retry_messages = messages + [
                {"role": "assistant", "content": response.content or ""},
                {
                    "role": "user",
                    "content": (
                        "Your previous answer did not contain valid structured edits.\n"
                        "Return only JSON with this schema:\n"
                        '{"edits":[{"file":"relative/path.py","find":"exact existing text",'
                        '"replace":"replacement text"}]}'
                    ),
                },
            ]
            try:
                response = self.llm.chat(
                    retry_messages,
                    max_completion_tokens=request_budget,
                )
            except Exception as exc:
                steps.append("self_edit:structured_edit_no_json")
                llm_calls.append(
                    self._llm_error_record(
                        exc,
                        attempt="structured_json_retry",
                        request_max_completion_tokens=request_budget,
                    )
                )
                return None
            edits = extract_structured_edits(response.content)
            llm_calls.append(
                self._structured_edit_call_record(
                    response,
                    attempt="structured_json_retry",
                    edits=edits,
                )
            )
            if not edits:
                steps.append("self_edit:structured_edit_no_json")
                return None

        result = apply_structured_edits(workspace_path, edits)
        tool_calls.append(_structured_edit_tool_call(edits, result))
        if not result.success:
            steps.append("self_edit:structured_edit_apply_failed")
            failed_apply_index = len(llm_calls) - 1
            llm_calls[-1]["structured_edit_apply_error"] = result.error
            llm_calls[-1]["apply_failure"] = _structured_apply_failure_envelope(
                error=result.error,
                edits=edits,
                retry_attempted=True,
                retry_failed=False,
            )
            expanded_context = _expand_agentless_context_for_edits(
                task,
                workspace_path,
                edits,
                existing_context=context,
            )
            if expanded_context:
                steps.append("self_edit:agentless_expand_context")
                context.extend(expanded_context)
                retrieved_context.extend(expanded_context)
                expansion_paths = [item["path"] for item in expanded_context]
                llm_calls[-1]["retrieval_expansion_paths"] = expansion_paths
                tool_calls.append(
                    {
                        "name": "agentless_expand_context",
                        "arguments": {
                            "reason": "structured_edit_apply_failed",
                            "apply_error": result.error,
                            "requested_files": [
                                edit.file_path for edit in edits[:20]
                            ],
                        },
                        "result": [
                            {
                                "path": item["path"],
                                "kind": item["kind"],
                                "chars": len(str(item.get("content", ""))),
                                "resolution": item.get("resolution"),
                            }
                            for item in expanded_context
                        ],
                    }
                )
            retry_base_messages = self._build_structured_edit_messages(
                task,
                workspace_path,
                context,
            )
            retry_messages = retry_base_messages + [
                {"role": "assistant", "content": response.content or ""},
                {
                    "role": "user",
                    "content": (
                        "The structured edits failed to apply to the workspace.\n"
                        f"Error: {result.error}\n"
                        "Return only corrected JSON. "
                        "Use start_line/end_line for the corrected edit when "
                        "line anchors or closest-snippet line labels are available. "
                        "Do not include L-prefixed line labels in replacement text. "
                        "Use exact find/replace only when no line numbers identify "
                        "the target safely. "
                        "Additional context was retrieved from the files named in "
                        "your failed edits when those files existed."
                    ),
                },
            ]
            try:
                response = self.llm.chat(
                    retry_messages,
                    max_completion_tokens=request_budget,
                )
            except Exception as exc:
                steps.append("self_edit:structured_edit_apply_retry_failed")
                _mark_apply_failure_retry_failed(llm_calls[failed_apply_index])
                llm_calls.append(
                    self._llm_error_record(
                        exc,
                        attempt="structured_apply_retry",
                        request_max_completion_tokens=request_budget,
                    )
                )
                return None
            edits = extract_structured_edits(response.content)
            llm_calls.append(
                self._structured_edit_call_record(
                    response,
                    attempt="structured_apply_retry",
                    edits=edits,
                )
            )
            if not edits:
                steps.append("self_edit:structured_edit_apply_retry_no_json")
                _mark_apply_failure_retry_failed(llm_calls[failed_apply_index])
                return None
            result = apply_structured_edits(workspace_path, edits)
            tool_calls.append(_structured_edit_tool_call(edits, result))
            if not result.success:
                steps.append("self_edit:structured_edit_apply_retry_failed")
                _mark_apply_failure_retry_failed(llm_calls[failed_apply_index])
                llm_calls[-1]["structured_edit_apply_error"] = result.error
                llm_calls[-1]["apply_failure"] = _structured_apply_failure_envelope(
                    error=result.error,
                    edits=edits,
                    retry_attempted=True,
                    retry_failed=True,
                )
                return None

        file_edits.extend(
            {
                **edit,
                "patch_type": "llm_patch",
                "source": "llm",
            }
            for edit in result.file_edits
        )
        steps.append("self_edit:structured_edit_applied")
        return "patch_applied"

    def _build_patch_messages(
        self,
        task: BenchmarkTask,
        workspace_path: Path | None,
        context: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        prompt = self.prompt_modules
        fail_to_pass = list((task.metadata or {}).get("fail_to_pass", []))
        repo_hint = _build_repo_hint(workspace_path)
        system = "\n".join(
            [
                prompt["system"],
                f"Planning: {prompt['planning']}",
                f"Localization: {prompt['localization']}",
                f"Patch generation: {prompt['patch_generation'].replace('{task_prompt}', task.prompt)}",
                f"Verification: {prompt['verification']}",
                "Output requirements:",
                "- Return exactly one git unified diff that can be applied with git apply.",
                "- Start the response with 'diff --git'.",
                "- Do not include markdown fences, explanations, bullet points, or analysis.",
                "- You do not have shell, grep, sed, cat, python, pytest, or any other tools.",
                "- Do not output JSON, tool calls, command objects, or {'cmd': ...} requests.",
                "- Use only the issue text and localized repository context already provided.",
                "- If multiple files must change, include all needed hunks in the same diff.",
                "- Prefer a minimal patch that addresses the failing tests first.",
            ]
        )
        if task.category == "security":
            system += f"\nSecurity review: {prompt['security_review']}"

        user_lines = [
            "Solve the repository issue below.",
            "Return only a unified diff patch. Do not include prose or markdown fences.",
            "Do not request commands or tools; no command you output will be executed.",
            f"Issue:\n{task.prompt}",
            f"Repository: {task.repo_url or 'local checkout'}",
            f"Base commit: {task.base_commit or 'unknown'}",
        ]
        if fail_to_pass:
            user_lines.append("Failing tests to target first:")
            user_lines.extend(f"- {target}" for target in fail_to_pass[:10])
        if repo_hint:
            user_lines.append("Repository file hints:")
            user_lines.extend(f"- {path}" for path in repo_hint)
        if context:
            user_lines.append(
                "Localized repository context from the fixed Agentless localizer:"
            )
            for item in context:
                user_lines.append(
                    f"<file path=\"{item['path']}\" kind=\"{item['kind']}\" "
                    f"score=\"{item['score']}\">"
                )
                user_lines.append(str(item["content"]))
                user_lines.append("</file>")
        user = "\n".join(user_lines)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_structured_edit_messages(
        self,
        task: BenchmarkTask,
        workspace_path: Path | None,
        context: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        prompt = self.prompt_modules
        fail_to_pass = list((task.metadata or {}).get("fail_to_pass", []))
        repo_hint = _build_repo_hint(workspace_path)
        system = "\n".join(
            [
                prompt["system"],
                f"Planning: {prompt['planning']}",
                f"Localization: {prompt['localization']}",
                f"Patch generation: {prompt['patch_generation'].replace('{task_prompt}', task.prompt)}",
                f"Verification: {prompt['verification']}",
                "Output requirements:",
                "- Return JSON only. Do not return a unified diff.",
                (
                    '- Preferred JSON schema: {"edits":[{"file":"relative/path.py",'
                    '"start_line":10,"end_line":12,"replace":"replacement text"}]}'
                ),
                (
                    '- Exact text fallback schema: {"edits":[{"file":"relative/path.py",'
                    '"find":"exact existing text","replace":"replacement text"}]}'
                ),
                "- Each file path must be relative to the repository root.",
                "- Prefer start_line/end_line when line anchors are available; line numbers are 1-based and inclusive.",
                "- Do not include L-prefixed line labels such as L0010: in replacement text.",
                "- If you use find, it must be an exact existing text block from the localized context.",
                "- The materializer will perform one bounded replacement per edit and then capture git diff.",
                "- Do not include markdown fences, explanations, bullet points, or analysis.",
                "- You do not have shell, grep, sed, cat, python, pytest, or any other tools.",
                "- Do not output tool calls, command objects, or {'cmd': ...} requests.",
            ]
        )
        if task.category == "security":
            system += f"\nSecurity review: {prompt['security_review']}"

        user_lines = [
            "Solve the repository issue below using structured edits.",
            "Return only JSON. Do not request commands or tools; no command you output will be executed.",
            f"Issue:\n{task.prompt}",
            f"Repository: {task.repo_url or 'local checkout'}",
            f"Base commit: {task.base_commit or 'unknown'}",
        ]
        if fail_to_pass:
            user_lines.append("Failing tests to target first:")
            user_lines.extend(f"- {target}" for target in fail_to_pass[:10])
        if repo_hint:
            user_lines.append("Repository file hints:")
            user_lines.extend(f"- {path}" for path in repo_hint)
        if context:
            user_lines.append("Localized repository context:")
            for item in context:
                content = str(item["content"])
                line_numbered = ""
                if content and str(item.get("path", "")).endswith(".py"):
                    line_numbered = _line_numbered_localized_content(
                        content,
                        max_chars=LOCALIZER_LINE_ANCHOR_BUDGET_CHARS,
                    )
                line_numbered_attr = ' line_numbers="1"' if line_numbered else ""
                user_lines.append(
                    f"<file path=\"{item['path']}\" kind=\"{item['kind']}\" "
                    f"score=\"{item['score']}\"{line_numbered_attr}>"
                )
                user_lines.append(line_numbered or content)
                user_lines.append("</file>")
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(user_lines)},
        ]

    def _llm_call_record(
        self,
        response: LLMResponse,
        *,
        attempt: str,
        patch_text: str,
    ) -> dict[str, Any]:
        content = response.content or ""
        raw = response.raw_response or {}
        choice = _first_dict(raw.get("choices"))
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        message_content = message.get("content")
        return {
            "provider": self.llm.provider,
            "model": response.model,
            "requested_model": response.requested_model or self.llm.requested_model,
            "deployment": response.deployment or self.llm.deployment,
            "reasoning_effort": (
                response.reasoning_effort or self.llm.reasoning_effort
            ),
            "attempt": attempt,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
            "cost_usd": response.usage.cost_usd,
            "content_chars": len(content),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content_preview": content[:LLM_CONTENT_PREVIEW_CHARS],
            "content_truncated": len(content) > LLM_CONTENT_PREVIEW_CHARS,
            "command_request_detected": _looks_like_command_request(content),
            "diff_extracted": bool(patch_text.strip()),
            "extracted_patch_chars": len(patch_text),
            "failure_type": _classify_llm_output(
                response,
                output_extracted=bool(patch_text.strip()),
            ),
            "raw_response_top_keys": sorted(str(key) for key in raw.keys()),
            "raw_model": raw.get("model"),
            "finish_reason": choice.get("finish_reason"),
            "choice_keys": sorted(str(key) for key in choice.keys()),
            "message_keys": sorted(str(key) for key in message.keys()),
            "message_role": message.get("role"),
            "message_content_present": "content" in message,
            "message_content_type": type(message_content).__name__,
            "message_content_is_none": message_content is None,
            "tool_call_count": len(message.get("tool_calls") or []),
            "refusal_present": bool(message.get("refusal")),
            "prompt_tokens_details": _numeric_details(usage.get("prompt_tokens_details")),
            "completion_tokens_details": _numeric_details(
                usage.get("completion_tokens_details")
            ),
            "request_max_completion_tokens": response.request_max_completion_tokens,
            "request_timeout_s": response.request_timeout_s,
            "request_attempts": list(response.request_attempts),
            "latency_ms": response.request_latency_ms,
        }

    def _structured_edit_call_record(
        self,
        response: LLMResponse,
        *,
        attempt: str,
        edits: list[StructuredEdit],
    ) -> dict[str, Any]:
        record = self._llm_call_record(response, attempt=attempt, patch_text="")
        record["structured_edit_extracted"] = bool(edits)
        record["structured_edit_count"] = len(edits)
        record["structured_edit_files"] = [edit.file_path for edit in edits[:20]]
        record["failure_type"] = _classify_llm_output(
            response,
            output_extracted=bool(edits),
        )
        return record

    def _llm_error_record(
        self,
        exc: Exception,
        *,
        attempt: str | None = None,
        request_max_completion_tokens: int | None = None,
    ) -> dict[str, Any]:
        record = {
            "provider": self.llm.provider,
            "model": self.llm.model,
            "requested_model": self.llm.requested_model,
            "deployment": self.llm.deployment,
            "reasoning_effort": self.llm.reasoning_effort,
            "attempt": attempt,
            "error": str(exc),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "request_max_completion_tokens": request_max_completion_tokens,
        }
        if isinstance(exc, LLMRequestError):
            record["failure_type"] = exc.category
            record["retryable"] = exc.retryable
            record["request_attempts"] = list(exc.request_attempts)
        else:
            record["failure_type"] = _classify_exception(exc)
            record["retryable"] = record["failure_type"] == "timeout"
            record["request_attempts"] = []
        return record

    def _patch_materializer_mode(self) -> str:
        value = self.workflow.get("patch_materializer") or self.workflow.get(
            "patch_materialization"
        )
        normalized = str(value or "unified-diff").strip().lower().replace("_", "-")
        if normalized in {"structured", "structured-edit", "structured-edits"}:
            return "structured-edit"
        return "unified-diff"

    def _initial_completion_budget(self) -> int | None:
        if self.llm.max_completion_tokens is None:
            return None
        return int(self.llm.max_completion_tokens)

    def _validate_and_repair(
        self,
        *,
        task: BenchmarkTask,
        workspace_path: Path,
        steps: list[str],
        file_edits: list[dict[str, Any]],
        llm_calls: list[dict[str, Any]],
        retrieved_context: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        command_results: list[dict[str, Any]],
        test_results: list[dict[str, Any]],
        test_commands: list[str],
    ) -> None:
        if not bool(self.workflow.get("validation_enabled", False)):
            return
        if not task.test_commands:
            steps.append("validation:skipped_no_test_commands")
            return

        image = str((task.metadata or {}).get("validation_image") or "").strip()
        if not image:
            steps.append("validation:tool_failure_missing_image")
            tool_calls.append(
                {
                    "name": "image_testbed_validator",
                    "arguments": {"attempt": "initial", "image": None},
                    "result": {
                        "passed": False,
                        "failure_type": "tool_failure",
                        "failure_summary": (
                            "Validation was enabled but task metadata did not include "
                            "a validation_image."
                        ),
                    },
                }
            )
            return

        patch_before = collect_patch_diff(
            workspace_path,
            task.command_timeout_s or self._validation_timeout_s(),
        )
        if not patch_before.strip():
            steps.append("validation:tool_failure_empty_patch")
            return

        steps.append("validation:run_initial")
        initial = run_image_validation(
            task=task,
            image=image,
            patch_text=patch_before,
            timeout_s=self._validation_timeout_s(task),
            attempt="initial",
            max_commands=self._validation_max_commands(),
            log_chars=self._validation_log_chars(),
        )
        self._record_validation_run(
            run=initial,
            patch_before=patch_before,
            patch_after=None,
            tool_calls=tool_calls,
            command_results=command_results,
            test_results=test_results,
            test_commands=test_commands,
        )
        if initial.passed:
            steps.append("validation:initial_passed")
            return
        steps.append(f"validation:initial_{initial.failure_type or 'failed'}")
        if not initial.feedback_eligible:
            steps.append("validation:retry_skipped_non_patch_failure")
            return
        if self._validation_retries() <= 0:
            steps.append("validation:retry_budget_exhausted")
            return

        retry_context = _validation_retry_context(
            task,
            workspace_path,
            retrieved_context=retrieved_context,
            file_edits=file_edits,
        )
        refreshed = [
            item
            for item in retry_context
            if item.get("retrieval_reason") == "validation_failure"
        ]
        if refreshed:
            retrieved_context.extend(refreshed)

        failed_command = (
            initial.command_results[-1].command if initial.command_results else ""
        )
        messages = self._build_structured_edit_messages(
            task,
            workspace_path,
            retry_context,
        )
        messages.append(
            {
                "role": "user",
                "content": "\n".join(
                    [
                        "The current patch applied, but focused tests failed in the "
                        "isolated SWE-bench image.",
                        f"Validation command: {failed_command}",
                        f"Failure type: {initial.failure_type}",
                        "Failure summary:",
                        initial.failure_summary,
                        "Current patch:",
                        _bounded_prompt_text(
                            patch_before,
                            self._validation_patch_prompt_chars(),
                        ),
                        (
                            "The workspace already contains the current patch. Return "
                            "only one incremental corrected structured-edit JSON object "
                            "against the current file contents. Do not return a diff, "
                            "commands, prose, or markdown."
                        ),
                    ]
                ),
            }
        )

        steps.append("validation:llm_feedback_retry")
        request_budget = self._initial_completion_budget()
        retry_started = time.perf_counter()
        try:
            response = self.llm.chat(
                messages,
                max_completion_tokens=request_budget,
            )
        except Exception as exc:
            record = self._llm_error_record(
                exc,
                attempt="validation_retry",
                request_max_completion_tokens=request_budget,
            )
            record["latency_ms"] = max(
                1,
                int((time.perf_counter() - retry_started) * 1000),
            )
            record["validation_failure_type"] = initial.failure_type
            record["validation_failure_summary"] = initial.failure_summary
            record["validation_command"] = failed_command
            record["validation_patch_before"] = patch_before
            llm_calls.append(record)
            steps.append("validation:llm_feedback_retry_failed")
            return

        edits = extract_structured_edits(response.content)
        record = self._structured_edit_call_record(
            response,
            attempt="validation_retry",
            edits=edits,
        )
        record["latency_ms"] = max(
            1,
            int((time.perf_counter() - retry_started) * 1000),
        )
        record["validation_failure_type"] = initial.failure_type
        record["validation_failure_summary"] = initial.failure_summary
        record["validation_command"] = failed_command
        record["validation_patch_before"] = patch_before
        llm_calls.append(record)
        if not edits:
            steps.append("validation:retry_no_structured_edits")
            return

        materialized = apply_structured_edits(workspace_path, edits)
        tool_calls.append(_structured_edit_tool_call(edits, materialized))
        if not materialized.success:
            llm_calls[-1]["structured_edit_apply_error"] = materialized.error
            llm_calls[-1]["apply_failure"] = _structured_apply_failure_envelope(
                error=materialized.error,
                edits=edits,
                phase="validation",
                retry_attempted=True,
                retry_failed=True,
            )
            steps.append("validation:retry_apply_failed")
            return

        file_edits.extend(
            {
                **edit,
                "patch_type": "llm_patch",
                "source": "llm",
                "validation_retry": True,
            }
            for edit in materialized.file_edits
        )
        patch_after = collect_patch_diff(
            workspace_path,
            task.command_timeout_s or self._validation_timeout_s(),
        )
        llm_calls[-1]["validation_patch_after"] = patch_after
        steps.append("validation:retry_patch_applied")

        final = run_image_validation(
            task=task,
            image=image,
            patch_text=patch_after,
            timeout_s=self._validation_timeout_s(task),
            attempt="retry",
            max_commands=self._validation_max_commands(),
            log_chars=self._validation_log_chars(),
        )
        self._record_validation_run(
            run=final,
            patch_before=patch_before,
            patch_after=patch_after,
            tool_calls=tool_calls,
            command_results=command_results,
            test_results=test_results,
            test_commands=test_commands,
        )
        steps.append(
            "validation:retry_passed"
            if final.passed
            else f"validation:retry_{final.failure_type or 'failed'}"
        )

    def _record_validation_run(
        self,
        *,
        run: ValidationRun,
        patch_before: str,
        patch_after: str | None,
        tool_calls: list[dict[str, Any]],
        command_results: list[dict[str, Any]],
        test_results: list[dict[str, Any]],
        test_commands: list[str],
    ) -> None:
        trace_results = [record.to_trace() for record in run.command_results]
        command_results.extend(trace_results)
        for record in trace_results:
            test_commands.append(str(record["command"]))
            test_results.append(
                {
                    "name": record["command"],
                    "attempt": record["attempt"],
                    "passed": record["validation_status"] == "passed",
                    "exit_code": record["exit_code"],
                    "timed_out": record["timed_out"],
                    "failure_type": record["failure_type"],
                    "failure_reason": record.get("failure_reason", ""),
                    "not_patch_failure": bool(record.get("not_patch_failure", False)),
                    "failure_summary": record["failure_summary"],
                }
            )
        tool_calls.append(
            {
                "name": "image_testbed_validator",
                "arguments": {
                    "attempt": (
                        trace_results[0]["attempt"] if trace_results else "unknown"
                    ),
                    "image": trace_results[0]["image"] if trace_results else None,
                    "commands": [record["command"] for record in trace_results],
                },
                "result": {
                    "passed": run.passed,
                    "failure_type": run.failure_type,
                    "failure_summary": run.failure_summary,
                    "patch_before": patch_before,
                    "patch_after": patch_after,
                    "command_results": [
                        {
                            "command": record["command"],
                            "exit_code": record["exit_code"],
                            "timed_out": record["timed_out"],
                            "duration_ms": record["duration_ms"],
                            "validation_status": record["validation_status"],
                            "failure_type": record["failure_type"],
                            "failure_reason": record.get("failure_reason", ""),
                            "not_patch_failure": bool(
                                record.get("not_patch_failure", False)
                            ),
                        }
                        for record in trace_results
                    ],
                },
            }
        )

    def _validation_retries(self) -> int:
        return min(1, max(0, int(self.workflow.get("validation_retries", 1))))

    def _validation_timeout_s(self, task: BenchmarkTask | None = None) -> int:
        configured = self.workflow.get("validation_timeout_s")
        if configured is not None:
            return max(1, int(configured))
        if task is not None and task.command_timeout_s is not None:
            return max(1, int(task.command_timeout_s))
        return 300

    def _validation_max_commands(self) -> int:
        return max(1, int(self.workflow.get("validation_max_commands", 10)))

    def _validation_log_chars(self) -> int:
        return max(1000, int(self.workflow.get("validation_log_chars", 12000)))

    def _validation_patch_prompt_chars(self) -> int:
        return max(
            2000,
            int(self.workflow.get("validation_patch_prompt_chars", 24000)),
        )

    def _self_edit_flask_config_from_file(
        self,
        steps: list[str],
        workspace_path: Path,
        file_edits: list[dict[str, Any]],
    ) -> str:
        return self._replace_in_first_matching_file(
            steps=steps,
            workspace_path=workspace_path,
            file_edits=file_edits,
            relative_paths=["src/flask/config.py", "flask/config.py"],
            find_text="        silent: bool = False,\n    ) -> bool:",
            replace_text="        silent: bool = False,\n        text: bool = True,\n    ) -> bool:",
            extra_edits=[
                {
                    "find": "            with open(filename) as f:",
                    "replace": '            with open(filename, "r" if text else "rb") as f:',
                }
            ],
        )

    def _apply_metadata_patch(
        self,
        task: BenchmarkTask,
        steps: list[str],
        workspace_path: Path,
        file_edits: list[dict[str, Any]],
    ) -> str:
        metadata = task.metadata or {}
        target = workspace_path / str(metadata["patch_file"])
        edits = metadata.get("edits") or [
            {
                "find": metadata.get("find", ""),
                "replace": metadata.get("replace", ""),
            }
        ]

        steps.append(f"edit_file:{metadata['patch_file']}")
        if not target.exists():
            steps.append("patch_file_missing")
            return "patch_file_missing"

        with target.open("r", encoding="utf-8", newline="") as handle:
            updated = handle.read()

        for edit in edits:
            find_text = str(edit.get("find", ""))
            replace_text = str(edit.get("replace", ""))
            if find_text not in updated and "\r\n" in updated:
                find_text = find_text.replace("\n", "\r\n")
                replace_text = replace_text.replace("\n", "\r\n")
            if find_text not in updated:
                steps.append("patch_target_not_found")
                return "patch_target_not_found"
            updated = updated.replace(find_text, replace_text, 1)
            file_edits.append(
                {
                    "path": str(target),
                    "relative_path": str(metadata["patch_file"]),
                    "action": "replace_text",
                    "find": find_text,
                    "replace": replace_text,
                }
            )

        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        steps.append("patch_applied")
        return task.expected_output

    def _replace_in_first_matching_file(
        self,
        *,
        steps: list[str],
        workspace_path: Path,
        file_edits: list[dict[str, Any]],
        relative_paths: list[str],
        find_text: str,
        replace_text: str,
        extra_edits: list[dict[str, str]] | None = None,
    ) -> str:
        edits = [{"find": find_text, "replace": replace_text}]
        edits.extend(extra_edits or [])

        for relative_path in relative_paths:
            target = workspace_path / relative_path
            if target.exists():
                steps.append(f"self_edit:edit_file:{relative_path}")
                return self._replace_text_edits(target, relative_path, edits, steps, file_edits)

        steps.append("self_edit:target_file_missing")
        return "self_edit_target_file_missing"

    def _replace_text_edits(
        self,
        target: Path,
        relative_path: str,
        edits: list[dict[str, str]],
        steps: list[str],
        file_edits: list[dict[str, Any]],
    ) -> str:
        with target.open("r", encoding="utf-8", newline="") as handle:
            updated = handle.read()

        for edit in edits:
            find_text = str(edit.get("find", ""))
            replace_text = str(edit.get("replace", ""))
            if find_text not in updated and "\r\n" in updated:
                find_text = find_text.replace("\n", "\r\n")
                replace_text = replace_text.replace("\n", "\r\n")
            if find_text not in updated:
                steps.append("self_edit:target_text_not_found")
                return "self_edit_target_text_not_found"
            updated = updated.replace(find_text, replace_text, 1)
            file_edits.append(
                {
                    "path": str(target),
                    "relative_path": relative_path,
                    "action": "replace_text",
                    "find": find_text,
                    "replace": replace_text,
                    "source": "self-edit",
                }
            )

        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        steps.append("self_edit:patch_applied")
        return "patch_applied"

    def _solve_security(self, task: BenchmarkTask, steps: list[str]) -> str:
        if self.workflow.get("security_review", False):
            steps.append("apply_security_review_policy")
            return task.expected_output
        steps.append("skip_security_review_policy")
        return "concatenate user input directly"


def _extract_unified_diff(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped == "NO_PATCH":
        return ""

    if "```" in stripped:
        fence_matches = re.finditer(r"```(?:diff|patch)?\n([\s\S]*?)```", stripped)
        for match in fence_matches:
            candidate = match.group(1).strip()
            if _looks_like_unified_diff(candidate):
                return candidate

    if _looks_like_unified_diff(stripped):
        return _trim_to_diff_start(stripped)
    return ""


def _structured_apply_failure_envelope(
    *,
    error: str,
    edits: list[StructuredEdit],
    phase: str = "structured_edit",
    retry_attempted: bool,
    retry_failed: bool,
) -> dict[str, Any]:
    failure_type = _classify_structured_apply_failure(error, edits)
    signature = [
        f"structured_edit_extracted={_bool_text(bool(edits))}",
        f"structured_edit_count={len(edits)}",
        "patch_chars_after_materialization=0",
        f"retry_attempted={_bool_text(retry_attempted)}",
        f"retry_failed={_bool_text(retry_failed)}",
    ]
    normalized_error = str(error or "").lower()
    if "target text not found" in normalized_error:
        signature.append("target_text_not_found")
    if "line range guard did not match" in normalized_error:
        signature.append("line_range_guard_mismatch")
    if "find text is empty" in normalized_error:
        signature.append("find_text_empty")
    if "no structured edits" in normalized_error or not edits:
        signature.append("structured_edit_extracted=false")

    return _apply_failure_envelope(
        phase=phase,
        failure_type=failure_type,
        known_failure_type="empty_patch",
        known_reason=(
            failure_type if failure_type != "unknown_apply_failure" else "unknown"
        ),
        is_weakly_classified=failure_type == "unknown_apply_failure",
        observable_signature=signature,
        message_tail=error,
        target_files=[edit.file_path for edit in edits[:20]],
        structured_edit_count=len(edits),
        structured_edit_extracted=bool(edits),
        edit_intent_present=bool(edits),
        retry_attempted=retry_attempted,
        retry_failed=retry_failed,
        patch_chars_before_materialization=0,
        patch_chars_after_materialization=0,
    )


def _git_apply_failure_envelope(
    result: Any,
    *,
    patch_text: str,
    retry_attempted: bool,
    retry_failed: bool,
) -> dict[str, Any]:
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    exit_code = getattr(result, "exit_code", None)
    timed_out = bool(getattr(result, "timed_out", False))
    combined = f"{stdout}\n{stderr}".lower()
    signature = [
        "patch_chars_before_materialization>0"
        if patch_text.strip()
        else "patch_chars_before_materialization=0",
        "patch_chars_after_materialization=0",
        f"retry_attempted={_bool_text(retry_attempted)}",
        f"retry_failed={_bool_text(retry_failed)}",
    ]
    if exit_code is not None:
        signature.append(f"git_apply_exit_code={int(exit_code)}")
    if timed_out:
        signature.append("git_apply_error=timeout")
    elif "corrupt patch" in combined:
        signature.append("git_apply_error=corrupt_patch")
    elif "does not apply" in combined or "patch failed" in combined:
        signature.append("git_apply_error=context_mismatch")
    elif "no such file" in combined or "does not exist" in combined:
        signature.append("git_apply_error=path_missing")

    return _apply_failure_envelope(
        phase="unified_diff",
        failure_type="git_apply_failure",
        known_failure_type="empty_patch",
        known_reason="git_apply_failure",
        is_weakly_classified=False,
        observable_signature=signature,
        message_tail=stderr or stdout,
        target_files=_target_files_from_unified_diff(patch_text),
        structured_edit_count=0,
        structured_edit_extracted=False,
        edit_intent_present=bool(patch_text.strip()),
        retry_attempted=retry_attempted,
        retry_failed=retry_failed,
        patch_chars_before_materialization=len(patch_text),
        patch_chars_after_materialization=0,
    )


def _apply_failure_envelope(
    *,
    phase: str,
    failure_type: str,
    known_failure_type: str,
    known_reason: str,
    is_weakly_classified: bool,
    observable_signature: list[str],
    message_tail: str,
    target_files: list[str],
    structured_edit_count: int,
    structured_edit_extracted: bool,
    edit_intent_present: bool,
    retry_attempted: bool,
    retry_failed: bool,
    patch_chars_before_materialization: int,
    patch_chars_after_materialization: int,
) -> dict[str, Any]:
    canonical_type = (
        failure_type
        if failure_type in _APPLY_FAILURE_TYPES
        else "unknown_apply_failure"
    )
    return {
        "schema_version": APPLY_FAILURE_SCHEMA_VERSION,
        "phase": str(phase or "unknown"),
        "type": canonical_type,
        "known_failure_type": str(known_failure_type or "empty_patch"),
        "known_reason": str(known_reason or "unknown"),
        "is_weakly_classified": bool(is_weakly_classified),
        "observable_signature": _dedupe_nonempty_strings(observable_signature),
        "message_tail": _redacted_tail(message_tail, max_chars=1000),
        "target_files": _dedupe_nonempty_strings(target_files)[:20],
        "structured_edit_count": max(0, int(structured_edit_count or 0)),
        "structured_edit_extracted": bool(structured_edit_extracted),
        "edit_intent_present": bool(edit_intent_present),
        "retry_attempted": bool(retry_attempted),
        "retry_failed": bool(retry_failed),
        "patch_chars_before_materialization": max(
            0,
            int(patch_chars_before_materialization or 0),
        ),
        "patch_chars_after_materialization": max(
            0,
            int(patch_chars_after_materialization or 0),
        ),
    }


def _classify_structured_apply_failure(
    error: str,
    edits: list[StructuredEdit],
) -> str:
    text = str(error or "").lower()
    if (
        "target text not found" in text
        or "line range guard did not match" in text
        or "find text is empty" in text
        or "anchor mismatch" in text
    ):
        return "structured_edit_anchor_mismatch"
    if (
        "no structured edits" in text
        or "malformed json" in text
        or "invalid json" in text
        or "parse failed" in text
        or not edits
    ):
        return "structured_edit_parse_failed"
    return "unknown_apply_failure"


def _mark_apply_failure_retry_failed(record: dict[str, Any]) -> None:
    envelope = record.get("apply_failure")
    if not isinstance(envelope, dict):
        return
    envelope["retry_attempted"] = True
    envelope["retry_failed"] = True
    signatures = [
        item
        for item in envelope.get("observable_signature", [])
        if not str(item).startswith("retry_attempted=")
        and not str(item).startswith("retry_failed=")
    ]
    signatures.extend(["retry_attempted=true", "retry_failed=true"])
    envelope["observable_signature"] = _dedupe_nonempty_strings(signatures)


def _target_files_from_unified_diff(patch_text: str) -> list[str]:
    files: list[str] = []
    for line in str(patch_text or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.append(parts[3].removeprefix("b/"))
        elif line.startswith("+++ "):
            target = line.split(maxsplit=1)[1].strip()
            if target != "/dev/null":
                files.append(target.removeprefix("b/"))
    return _dedupe_nonempty_strings(files)[:20]


def _redacted_tail(text: str, *, max_chars: int) -> str:
    value = str(text or "")
    if len(value) > max_chars:
        value = value[-max_chars:]
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _dedupe_nonempty_strings(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _looks_like_command_request(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if re.search(r'["\']cmd["\']\s*:', stripped):
        return True
    if re.search(r'["\']command["\']\s*:', stripped):
        return True
    return bool(
        re.search(
            r"\b(grep|sed|cat|tail|head|python|pytest|rg)\b.+\b(src|tests?|\.py)\b",
            stripped,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )


def _first_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _numeric_details(value: Any) -> dict[str, int | float | bool | None]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, (int, float, bool)) or item is None
    }


def _classify_llm_output(
    response: LLMResponse,
    *,
    output_extracted: bool,
) -> str | None:
    if output_extracted:
        return None
    raw = response.raw_response or {}
    choice = _first_dict(raw.get("choices"))
    finish_reason = str(choice.get("finish_reason") or "").lower()
    details = raw.get("usage", {}).get("completion_tokens_details", {})
    reasoning_tokens = (
        int(details.get("reasoning_tokens", 0) or 0)
        if isinstance(details, dict)
        else 0
    )
    requested = response.request_max_completion_tokens
    if finish_reason == "length":
        if requested is None or response.usage.completion_tokens >= requested or reasoning_tokens:
            return "length_exhausted"
    if not (response.content or "").strip():
        return "empty_content"
    return "invalid_output"


def _classify_exception(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return "timeout"
    return "llm_error"


def _structured_edit_tool_call(
    edits: list[StructuredEdit],
    result: Any,
) -> dict[str, Any]:
    return {
        "name": "structured_edit_materializer",
        "arguments": {
            "edit_count": len(edits),
            "files": [edit.file_path for edit in edits[:20]],
        },
        "result": {
            "success": bool(result.success),
            "error": result.error,
            "applied_paths": [
                edit.get("relative_path") for edit in result.file_edits[:20]
            ],
        },
    }


def _looks_like_unified_diff(text: str) -> bool:
    if "diff --git " in text:
        return True
    return "--- " in text and "+++ " in text and "@@" in text


def _trim_to_diff_start(text: str) -> str:
    markers = ["diff --git ", "--- ", "+++ "]
    starts = [text.find(marker) for marker in markers if text.find(marker) != -1]
    if not starts:
        return text.strip()
    return text[min(starts) :].strip()


def _build_repo_hint(workspace_path: Path | str | None) -> list[str]:
    if workspace_path is None:
        return []
    workspace = Path(workspace_path)
    if not workspace.exists():
        return []

    preferred = [
        path
        for path in workspace.rglob("*.py")
        if path.is_file() and ".git" not in path.parts and "tests" not in path.parts
    ]
    preferred.sort(key=lambda path: (len(path.parts), str(path)))
    return [path.relative_to(workspace).as_posix() for path in preferred[:12]]


def _validation_retry_context(
    task: BenchmarkTask,
    workspace_path: Path,
    *,
    retrieved_context: list[dict[str, Any]],
    file_edits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Refresh localized context after a test failure without unbounded retrieval."""
    fresh = _localize_agentless_context(
        task,
        workspace_path,
        max_files=6,
        max_chars_per_file=6000,
    )
    modified_paths = list(
        dict.fromkeys(
            str(edit.get("relative_path", "")).replace("\\", "/")
            for edit in file_edits
            if edit.get("relative_path") not in {None, "", "."}
        )
    )
    synthetic_edits = [
        StructuredEdit(file_path=path, find="", replace="")
        for path in modified_paths[:3]
    ]
    refreshed = _expand_agentless_context_for_edits(
        task,
        workspace_path,
        synthetic_edits,
        existing_context=fresh or retrieved_context,
        max_files=3,
        max_chars_per_file=8000,
    )
    refreshed = [
        {
            **item,
            "kind": "validation_source",
            "retrieval_reason": "validation_failure",
        }
        for item in refreshed
    ]

    merged: dict[str, dict[str, Any]] = {}
    for item in fresh + refreshed:
        path = str(item.get("path", ""))
        if path:
            merged[path] = item
    return list(merged.values())[:8]


def _bounded_prompt_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return f"{text[:head]}\n...<patch truncated>...\n{text[-tail:]}"


def _line_numbered_localized_content(content: str, max_chars: int = 12000) -> str:
    """Render localized excerpts with stable 1-based line labels for line-range edits."""
    if not content.strip():
        return ""

    output: list[str] = []
    current_line = 1
    emitted_any_numbered_line = False
    for raw_line in content.splitlines():
        header = re.match(r"# lines (\d+)-(\d+)\s*$", raw_line.strip())
        if header:
            current_line = int(header.group(1))
            output.append(raw_line)
            continue
        if raw_line.strip() == "...":
            output.append(raw_line)
            continue
        output.append(f"L{current_line:04d}: {raw_line}")
        current_line += 1
        emitted_any_numbered_line = True
        if sum(len(line) + 1 for line in output) >= max_chars:
            output.append("...<line anchors truncated>...")
            break

    if not emitted_any_numbered_line:
        return ""
    return "\n".join(output)


def _localize_agentless_context(
    task: BenchmarkTask,
    workspace_path: Path | str | None,
    *,
    max_files: int = 6,
    max_chars_per_file: int = 6000,
    max_context_chars: int | None = None,
    max_source_files: int | None = None,
) -> list[dict[str, Any]]:
    """Return budgeted source/test evidence for a fixed Agentless-style repair step."""
    if workspace_path is None:
        return []
    workspace = Path(workspace_path)
    if not workspace.exists():
        return []

    total_budget = max(
        1000,
        int(max_context_chars or LOCALIZER_DEFAULT_CONTEXT_BUDGET_CHARS),
    )
    test_targets = _test_targets(task)
    source_focus_terms = _source_focus_terms(task)
    source_name_hints = _source_name_hints_from_test_targets(test_targets)
    candidates: dict[Path, dict[str, Any]] = {}
    context: list[dict[str, Any]] = []

    def remaining_budget() -> int:
        used = sum(len(str(item.get("content", ""))) for item in context)
        return max(0, total_budget - used)

    def append_context_item(
        *,
        path: str,
        kind: str,
        score: int,
        content: str,
        budget_chars: int,
        focus_terms: list[str] | None = None,
        high_signal_terms: list[str] | None = None,
        score_reasons: list[str] | None = None,
    ) -> bool:
        remaining = remaining_budget()
        if remaining <= 0:
            return False
        limit = max(1, min(budget_chars, remaining))
        bounded = content[:limit]
        context.append(
            {
                "path": path,
                "kind": kind,
                "score": score,
                "content": bounded,
                "content_chars": len(bounded),
                "budget_chars": limit,
                "focus_terms": list(dict.fromkeys(focus_terms or []))[:24],
                "high_signal_terms": list(dict.fromkeys(high_signal_terms or []))[:16],
                "score_reasons": list(dict.fromkeys(score_reasons or []))[:24],
                "context_policy": "budgeted_agentless_v2",
            }
        )
        return True

    if task.test_patch.strip():
        append_context_item(
            path="benchmark_test_patch.diff",
            kind="test_patch",
            score=120,
            content=task.test_patch.strip(),
            budget_chars=min(max_chars_per_file, LOCALIZER_TEST_PATCH_BUDGET_CHARS),
            score_reasons=["benchmark_test_patch"],
        )

    for target in test_targets:
        path_text, _, test_name = target.partition("::")
        path = workspace / path_text
        if path.exists() and path.is_file():
            rel = path.relative_to(workspace)
            candidates[path] = {
                "kind": "test",
                "score": 100,
                "focus_terms": [test_name] if test_name else [],
                "relative_path": rel.as_posix(),
                "high_signal_terms": [test_name] if test_name else [],
                "score_reasons": [f"fail_to_pass_target:{target}"],
            }

    for path in _iter_python_files(workspace):
        if path in candidates:
            continue
        rel_text = path.relative_to(workspace).as_posix().lower()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lowered = text.lower()
        score = 0
        focus_terms: list[str] = []
        high_signal_terms: list[str] = []
        score_reasons: list[str] = []
        for term in source_focus_terms:
            low_term = term.lower()
            high_signal = _is_high_signal_localizer_term(term)
            if low_term in rel_text:
                score += 8 if high_signal else 1
                focus_terms.append(term)
                score_reasons.append(
                    f"{'high' if high_signal else 'low'}_signal_path_match:{term}"
                )
                if high_signal:
                    high_signal_terms.append(term)
            if low_term in lowered:
                score += 5 if high_signal else 1
                focus_terms.append(term)
                score_reasons.append(
                    f"{'high' if high_signal else 'low'}_signal_content_match:{term}"
                )
                if high_signal:
                    high_signal_terms.append(term)
        if path.name.lower() in source_name_hints:
            score += 45
            focus_terms.extend(source_name_hints[path.name.lower()])
            high_signal_terms.extend(source_name_hints[path.name.lower()])
            score_reasons.append(f"test_source_name_hint:{path.name.lower()}")
        for target in test_targets:
            target_path = target.split("::", 1)[0].lower()
            if target_path in rel_text:
                score += 12
                score_reasons.append(f"test_path_overlap:{target_path}")
        if score <= 0:
            continue
        candidates[path] = {
            "kind": "source",
            "score": score,
            "focus_terms": list(dict.fromkeys(focus_terms)),
            "high_signal_terms": list(dict.fromkeys(high_signal_terms)),
            "relative_path": path.relative_to(workspace).as_posix(),
            "score_reasons": list(dict.fromkeys(score_reasons)),
        }

    ranked_tests = sorted(
        (
            (path, info)
            for path, info in candidates.items()
            if info["kind"] == "test"
        ),
        key=lambda item: (
            -int(item[1]["score"]),
            len(item[1]["relative_path"]),
            item[1]["relative_path"],
        ),
    )
    ranked_sources = sorted(
        (
            (path, info)
            for path, info in candidates.items()
            if info["kind"] == "source"
        ),
        key=lambda item: (
            -len(item[1].get("high_signal_terms", [])),
            -int(item[1]["score"]),
            len(item[1]["relative_path"]),
            item[1]["relative_path"],
        ),
    )

    remaining_slots = max(0, max_files - len(context))
    for path, info in ranked_tests[:remaining_slots]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        content = _focused_excerpt(
            text,
            focus_terms=info.get("focus_terms", []) + source_focus_terms,
            max_chars=min(max_chars_per_file, LOCALIZER_TEST_FILE_BUDGET_CHARS),
        )
        if not append_context_item(
            path=info["relative_path"],
            kind=info["kind"],
            score=info["score"],
            content=content,
            budget_chars=min(max_chars_per_file, LOCALIZER_TEST_FILE_BUDGET_CHARS),
            focus_terms=info.get("focus_terms", []),
            high_signal_terms=info.get("high_signal_terms", []),
            score_reasons=info.get("score_reasons", []),
        ):
            break

    remaining_slots = max(0, max_files - len(context))
    source_slots = max_source_files
    if source_slots is None:
        source_slots = max(1, min(3, remaining_slots))
    source_slots = max(0, min(source_slots, remaining_slots))
    best_source_score = int(ranked_sources[0][1]["score"]) if ranked_sources else 0
    source_score_floor = max(12, int(best_source_score * 0.65))
    selected_sources = 0
    for path, info in ranked_sources:
        if selected_sources >= source_slots:
            break
        if (
            selected_sources > 0
            and int(info["score"]) < source_score_floor
            and not info.get("high_signal_terms")
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        content_budget = min(max_chars_per_file, LOCALIZER_SOURCE_FILE_BUDGET_CHARS)
        content = _focused_excerpt(
            text,
            focus_terms=info.get("focus_terms", []) + source_focus_terms,
            max_chars=content_budget,
        )
        appended = append_context_item(
            path=info["relative_path"],
            kind=info["kind"],
            score=info["score"],
            content=content,
            budget_chars=content_budget,
            focus_terms=info.get("focus_terms", []),
            high_signal_terms=info.get("high_signal_terms", []),
            score_reasons=info.get("score_reasons", []),
        )
        if not appended:
            break
        selected_sources += 1
    return context


def _expand_agentless_context_for_edits(
    task: BenchmarkTask,
    workspace_path: Path | str,
    edits: list[StructuredEdit],
    *,
    existing_context: list[dict[str, Any]],
    max_files: int = 3,
    max_chars_per_file: int = 8000,
) -> list[dict[str, Any]]:
    """Retrieve source excerpts named by failed structured edits."""
    workspace = Path(workspace_path).resolve()
    if not workspace.exists():
        return []

    existing_paths = {
        str(item.get("path", "")).replace("\\", "/")
        for item in existing_context
        if item.get("path")
    }
    issue_terms = _issue_terms(task)
    expanded: list[dict[str, Any]] = []
    seen: set[Path] = set()

    for edit in edits:
        if len(expanded) >= max_files:
            break
        target, resolution = _resolve_edit_target_file(
            workspace,
            edit.file_path,
        )
        if target is None or target in seen:
            continue
        seen.add(target)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        relative_path = target.relative_to(workspace).as_posix()
        focus_terms = issue_terms + _structured_edit_focus_terms(edit)
        content = _focused_excerpt(
            text,
            focus_terms=focus_terms,
            max_chars=max_chars_per_file,
            window=50,
        )
        expanded.append(
            {
                "path": relative_path,
                "kind": "dynamic_source",
                "score": 200 if resolution == "exact" else 160,
                "content": content,
                "resolution": resolution,
                "retrieval_reason": "structured_edit_apply_failed",
                "requested_path": edit.file_path,
                "was_in_initial_context": relative_path in existing_paths,
            }
        )
    return expanded


def _resolve_edit_target_file(
    workspace: Path,
    requested_path: str,
) -> tuple[Path | None, str | None]:
    normalized = requested_path.replace("\\", "/").strip()
    if not normalized:
        return None, None
    relative = Path(normalized)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None, None

    exact = (workspace / relative).resolve()
    if workspace in exact.parents and exact.is_file():
        return exact, "exact"

    basename = relative.name
    if not basename:
        return None, None
    matches = [
        path.resolve()
        for path in _iter_python_files(workspace)
        if path.name == basename
    ]
    if not matches:
        return None, None
    requested_parts = relative.parts
    matches.sort(
        key=lambda path: (
            -_common_suffix_length(
                path.relative_to(workspace).parts,
                requested_parts,
            ),
            len(path.relative_to(workspace).parts),
            path.relative_to(workspace).as_posix(),
        )
    )
    return matches[0], "basename"


def _structured_edit_focus_terms(edit: StructuredEdit) -> list[str]:
    terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", edit.find)
    terms.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", edit.replace))
    terms.extend(part for part in Path(edit.file_path).parts if len(part) >= 3)
    return list(dict.fromkeys(terms))[:32]


def _common_suffix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    count = 0
    for left_part, right_part in zip(reversed(left), reversed(right)):
        if left_part != right_part:
            break
        count += 1
    return count


def _test_targets(task: BenchmarkTask) -> list[str]:
    metadata = task.metadata or {}
    raw_targets = list(metadata.get("fail_to_pass", []) or [])
    raw_targets.extend(metadata.get("pass_to_pass", []) or [])
    raw_targets.extend(task.test_commands)
    targets: list[str] = []
    for item in raw_targets:
        text = str(item)
        for part in text.split():
            if ".py" in part and "::" in part:
                targets.append(part.strip("'\""))
            elif part.endswith(".py") or ".py::" in part:
                targets.append(part.strip("'\""))
    return list(dict.fromkeys(targets))


def _source_name_hints_from_test_targets(test_targets: list[str]) -> dict[str, list[str]]:
    hints: dict[str, list[str]] = {}
    for target in test_targets:
        path_text = target.split("::", 1)[0].replace("\\", "/")
        basename = Path(path_text).name
        if not basename.startswith("test_") or not basename.endswith(".py"):
            continue
        source_name = f"{basename[len('test_'):]}"
        stem = Path(source_name).stem
        hints.setdefault(source_name.lower(), []).extend([stem, f"_{stem}"])
    return {key: list(dict.fromkeys(value)) for key, value in hints.items()}


def _is_high_signal_localizer_term(term: str) -> bool:
    normalized = term.strip().lower()
    if not normalized or normalized in LOCALIZER_LOW_SIGNAL_TERMS:
        return False
    if any(marker in normalized for marker in ("_", ".", "/", "::")):
        return True
    if len(re.findall(r"[a-z][a-z0-9]{2,}", normalized)) >= 2:
        return True
    return len(normalized) >= 6


def _issue_terms(task: BenchmarkTask) -> list[str]:
    text = f"{task.prompt}\n{task.issue_description}"
    terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
    stop = {
        "the",
        "and",
        "for",
        "such",
        "with",
        "should",
        "allow",
        "default",
        "object",
        "file",
        "mode",
        "add",
        "way",
    }
    selected: list[str] = []
    for term in terms:
        if term.lower() in stop:
            continue
        selected.append(term)
        if "_" in term:
            selected.extend(piece for piece in term.split("_") if len(piece) >= 3)
    dotted = re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", text)
    for term in dotted:
        selected.append(term)
        selected.extend(
            piece
            for piece in term.split(".")
            if len(piece) >= 3 and piece.lower() not in stop
        )
    return list(dict.fromkeys(selected))[:24]


def _source_focus_terms(task: BenchmarkTask) -> list[str]:
    """High-signal terms for source excerpts, preferring test-patch evidence."""
    selected: list[str] = []
    test_patch = task.test_patch or ""
    for match in re.finditer(r"""["']([^"'\n]{6,120})["']""", test_patch):
        phrase = match.group(1).strip()
        if _is_high_signal_phrase(phrase):
            selected.append(phrase)

    for target in _test_targets(task):
        node = target.split("::")[-1]
        if node:
            selected.append(node)
            selected.extend(
                piece
                for piece in node.split("_")
                if len(piece) >= 5 and piece.lower() not in {"test", "tests"}
            )

    selected.extend(_issue_terms(task))
    return list(dict.fromkeys(selected))[:48]


def _is_high_signal_phrase(text: str) -> bool:
    lowered = text.lower()
    if len(lowered) < 6:
        return False
    if any(char in lowered for char in "_./"):
        return True
    words = re.findall(r"[a-z][a-z0-9]{2,}", lowered)
    if len(words) >= 3:
        return True
    return len(lowered) >= 12


def _iter_python_files(workspace: Path) -> list[Path]:
    ignored_parts = {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "build",
        "dist",
    }
    paths = [
        path
        for path in workspace.rglob("*.py")
        if path.is_file() and not (set(path.parts) & ignored_parts)
    ]
    paths.sort(key=lambda path: (len(path.parts), str(path)))
    return paths[:1500]


def _focused_excerpt(
    text: str,
    *,
    focus_terms: list[str],
    max_chars: int,
    window: int = 35,
) -> str:
    lines = text.splitlines()
    if len(text) <= max_chars:
        return text

    lowered_terms = _prioritize_focus_terms(focus_terms)
    identifier_terms = [term for term in lowered_terms if "_" in term or "." in term]
    matched_indexes = _matching_line_indexes(lines, identifier_terms[:8])
    if not matched_indexes:
        matched_indexes = _matching_line_indexes(lines, lowered_terms)
    if not matched_indexes:
        return "\n".join(lines[: max(1, window * 2)])[:max_chars]

    ranges: list[tuple[int, int, int]] = []
    for index in matched_indexes[:8]:
        start = max(0, index - window // 2)
        end = min(len(lines), index + window // 2 + 1)
        if ranges and start <= ranges[-1][1]:
            previous_start, previous_end, previous_center = ranges[-1]
            ranges[-1] = (
                previous_start,
                max(previous_end, end),
                previous_center,
            )
        else:
            ranges.append((start, end, index))

    chunks: list[str] = []
    used = 0
    for start, end, center in ranges:
        remaining = max_chars - used
        chunk = _fit_focused_chunk(lines, start, end, center, remaining)
        if used + len(chunk) > max_chars and chunks:
            break
        chunks.append(chunk)
        used += len(chunk)
        if used >= max_chars:
            break
    return "\n\n...\n\n".join(chunks)[:max_chars]


def _fit_focused_chunk(
    lines: list[str],
    start: int,
    end: int,
    center: int,
    max_chars: int,
) -> str:
    start = max(0, min(start, center))
    end = min(len(lines), max(end, center + 1))

    def build(left: int, right: int) -> str:
        return f"# lines {left + 1}-{right}\n" + "\n".join(lines[left:right])

    while start < center or end > center + 1:
        chunk = build(start, end)
        if len(chunk) <= max_chars:
            return chunk
        left_distance = center - start
        right_distance = end - (center + 1)
        if left_distance >= right_distance and start < center:
            start += 1
        elif end > center + 1:
            end -= 1
        else:
            break
    return build(start, end)[:max_chars]


def _prioritize_focus_terms(focus_terms: list[str]) -> list[str]:
    stop = {"flask", "config", "python", "api", "false", "true", "load"}
    unique = list(dict.fromkeys(term.lower() for term in focus_terms if term))
    high_signal = [
        term
        for term in unique
        if term not in stop and ("_" in term or "." in term or len(term) >= 6)
    ]
    return high_signal + [term for term in unique if term not in high_signal]


def _matching_line_indexes(lines: list[str], terms: list[str]) -> list[int]:
    if not terms:
        return []
    indexes: list[int] = []
    seen: set[int] = set()
    for term in terms:
        lowered_term = term.lower()
        for index, line in enumerate(lines):
            if index in seen:
                continue
            if lowered_term in line.lower():
                indexes.append(index)
                seen.add(index)
    return indexes
