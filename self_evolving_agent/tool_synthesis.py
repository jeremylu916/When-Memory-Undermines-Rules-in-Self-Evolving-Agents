"""Tool synthesis from failure traces (Trace2Tool, piece 1).

Prior on-the-fly tool-synthesis agents write a script *inside* an episode and
throw it away when the task ends. This module is the offline analogue: it reads a
diagnosed failure trace and proposes a small, self-contained script that would
have shortcut the failure, annotated with a *scope hypothesis* (task/repo/global)
so a later transfer gate can decide whether the tool earns a place in the
agent's persistent, standing toolset.

Design mirrors :class:`critic.LLMCritic`: the LLM client is injected and
duck-typed (``.chat(messages, max_completion_tokens=, timeout_s=)`` returning an
object with ``.content``); everything else---prompt construction, JSON parsing,
schema validation, and a light synthesis-time safety scan---is pure and
deterministic so it is unit-testable without a live model. The *authoritative*
safety gate and the transfer-promotion gate live in separate modules; the scan
here only rejects candidates that are unusable on their face.

The scope hypothesis is only a hypothesis. It is not trusted until validated by
transfer (see the transfer gate, analogous to :mod:`memory_transfer_gate`).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Sequence

# Failure patterns a synthesized *tool* can plausibly address. Deliberately a
# subset of the critic taxonomy: tools help with finding/navigating/applying, not
# with model-reasoning failures that no script can fix.
TOOL_FAILURE_PATTERNS: tuple[str, ...] = (
    "localization_failure",       # cannot find the site to edit
    "repo_navigation_failure",    # cannot map the codebase structure
    "patch_application_failure",  # diff does not apply cleanly
    "regression_over_broad_edit", # edit too broad, breaks PASS_TO_PASS
    "redundant_exploration",      # wastes steps re-deriving the same context
)

# Hypothesized generality of a tool. Validated later by transfer, not trusted now.
SCOPE_HYPOTHESES: tuple[str, ...] = ("task", "repo", "global")

# Synthesis-time safety scan reasons (authoritative gate is separate).
UNSAFE_MODIFIES_TESTS = "modifies_tests_or_evaluator"
UNSAFE_DESTRUCTIVE = "destructive_filesystem_op"
UNSAFE_NETWORK = "network_or_exfiltration"
UNSAFE_PRIVILEGE = "privilege_or_budget_escalation"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")

# Coarse substring guards for dangers that hold *regardless of intent*: no
# well-formed read-only tool needs these. These do not replace the authoritative
# safety checker; they only drop candidates that are unusable on their face.
_UNSAFE_PATTERNS: tuple[tuple[str, str], ...] = (
    (UNSAFE_DESTRUCTIVE, "rm -rf"),
    (UNSAFE_DESTRUCTIVE, "shutil.rmtree"),
    (UNSAFE_NETWORK, "requests.post"),
    (UNSAFE_NETWORK, "urllib.request"),
    (UNSAFE_NETWORK, "socket.socket"),
    (UNSAFE_PRIVILEGE, "sudo "),
    (UNSAFE_PRIVILEGE, "os.setuid"),
)

# Test/evaluator modification must be detected by WRITE INTENT, not by mention:
# well-behaved localization tools legitimately name test paths in skip-lists to
# *avoid* them, so a plain substring match false-positives on the safest tools.
# We flag only an actual write to a test-ish path.
_TEST_TOKENS = ("conftest", "test_patch", "fail_to_pass", "pass_to_pass")
# open(<path...test...>, <mode with w/a/x>)  -- either argument order of quotes.
_OPEN_WRITE_RE = re.compile(
    r"open\s*\(\s*[^,)]*?(?:conftest|test_patch|fail_to_pass|pass_to_pass|test[_a-z0-9]*\.py)[^,)]*?,\s*['\"][^'\"]*[wax][^'\"]*['\"]",
    re.IGNORECASE,
)
# pathlib write to a test path: Path(...test...).write_text(/.write_bytes(
_PATH_WRITE_RE = re.compile(
    r"(?:conftest|test_patch|fail_to_pass|pass_to_pass|test[_a-z0-9]*\.py)['\"]?\s*\)?\s*\.\s*write_(?:text|bytes)\s*\(",
    re.IGNORECASE,
)
# shell redirection into a test-ish path:  > .../test_foo.py  or  >> conftest.py
_SHELL_REDIR_RE = re.compile(
    r">>?\s*\S*(?:conftest|test_patch|fail_to_pass|pass_to_pass|test[_a-z0-9]*\.py)",
    re.IGNORECASE,
)


def _writes_to_tests(script: str) -> bool:
    """True only if the script appears to WRITE to a test/evaluator path."""

    return bool(
        _OPEN_WRITE_RE.search(script)
        or _PATH_WRITE_RE.search(script)
        or _SHELL_REDIR_RE.search(script)
    )


@dataclass(frozen=True)
class SynthesizedTool:
    """A candidate reusable tool distilled from one failure trace.

    ``scope_hypothesis`` is the optimizer's guess at generality; the transfer
    gate overwrites it with a *calibrated* scope. ``safety_flags`` records any
    synthesis-time scan hits (empty tuple means it passed the coarse scan).
    """

    name: str
    description: str
    failure_pattern: str
    scope_hypothesis: str
    language: str
    script: str
    cli: str
    rationale: str
    safety_notes: str
    source_task_id: str
    source_repo: str
    safety_flags: tuple[str, ...] = ()

    @property
    def rejected(self) -> bool:
        return bool(self.safety_flags)

    def to_audit(self) -> dict[str, Any]:
        data = asdict(self)
        # Scripts can be large; keep the audit record light but complete.
        data["script_chars"] = len(self.script)
        data["rejected"] = self.rejected
        return data


class ToolSynthesisError(ValueError):
    """Raised when an LLM tool payload cannot be parsed or validated."""


def basic_safety_scan(script: str) -> tuple[str, ...]:
    """Coarse, deterministic synthesis-time safety scan.

    Returns the tuple of triggered reason codes (empty == passed). This is a
    fail-fast guard, not the authoritative safety gate: it only catches tools
    that are obviously unusable (destructive, network-exfiltrating, or that
    touch tests/the evaluator).
    """

    lowered = script.lower()
    flags: list[str] = []
    for reason, needle in _UNSAFE_PATTERNS:
        if needle.lower() in lowered and reason not in flags:
            flags.append(reason)
    if _writes_to_tests(script):
        flags.append(UNSAFE_MODIFIES_TESTS)
    return tuple(flags)


class ToolSynthesizer:
    """Distills candidate tools from a diagnosed failure trace via an LLM.

    Mirrors :class:`critic.LLMCritic`: inject ``llm_client`` (or ``None`` to
    disable). With no client, :meth:`synthesize` returns an empty list rather
    than fabricating tools---tool synthesis needs a model that can write code.
    """

    def __init__(
        self,
        llm_client: Any | None,
        *,
        max_completion_tokens: int = 16384,
        max_tools_per_trace: int = 2,
        timeout_s: int | None = None,
    ):
        self.llm_client = llm_client
        # GPT-5.5 is a reasoning model: it spends completion tokens on hidden
        # reasoning before emitting the answer. A full self-contained script plus
        # reasoning overruns a 4096 budget (finish_reason=length, empty content),
        # so tool synthesis needs a generous budget. This is an offline evolution
        # step, not the fixed per-task agent budget, so raising it is legitimate.
        self.max_completion_tokens = max(4096, int(max_completion_tokens))
        self.max_tools_per_trace = max(1, int(max_tools_per_trace))
        self.timeout_s = timeout_s

    def synthesize(
        self,
        record: Mapping[str, Any],
        analysis: Mapping[str, Any] | None = None,
    ) -> list[SynthesizedTool]:
        """Propose tools for one failed trace. Empty list if none/unavailable."""

        if self.llm_client is None:
            return []
        try:
            response = self.llm_client.chat(
                self.build_messages(record, analysis),
                max_completion_tokens=self.max_completion_tokens,
                timeout_s=self.timeout_s,
            )
        except Exception:  # pragma: no cover - provider errors vary.
            return []
        try:
            payload = _parse_json_object(response.content)
        except ValueError:
            return []
        source_task_id = str(record.get("task_id") or record.get("instance_id") or "")
        source_repo = _record_repo(record)
        return parse_tool_payload(
            payload,
            source_task_id=source_task_id,
            source_repo=source_repo,
            max_tools=self.max_tools_per_trace,
        )

    def build_messages(
        self,
        record: Mapping[str, Any],
        analysis: Mapping[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        labels = list((analysis or {}).get("labels") or [])
        hint = str((analysis or {}).get("optimizer_hint") or "")
        system = (
            "You are a tool-synthesis engineer for a self-evolving software "
            "agent. The agent is Agentless: it cannot call tools during solving. "
            "Instead, a synthesized tool is run by the harness BEFORE the agent "
            "acts, and its stdout is injected into the localized repository "
            "context the agent reads. So each tool is a CONTEXT-ENRICHMENT "
            "operator: given the repository and the issue, it must print concise, "
            "grounded hints---relevant files, definitions, call sites, line "
            f"spans---that help locate and fix the bug. Propose at most "
            f"{self.max_tools_per_trace} such tools that would have shortcut the "
            "observed failure and, crucially, would be REUSABLE on other "
            "repositories. "
            "MANDATORY INTERFACE: each tool is a self-contained Python 3 script "
            "invoked EXACTLY as `python <tool>.py --repo-root <DIR> --issue-file "
            "<FILE>` and MUST print its hints to stdout and exit 0. It must parse "
            "those two flags (extra optional flags allowed). Keep stdout compact "
            "(a few KB). Hard rules: (1) READ-ONLY---never write files, and never "
            "read, write, or infer test_patch, FAIL_TO_PASS, PASS_TO_PASS, or "
            "conftest (skip test files); (2) no network, no destructive fs ops, "
            "no privilege/budget escalation; (3) standard library only; (4) fail "
            "soft---on any error print nothing and exit 0, never crash. Give an "
            "honest scope_hypothesis: 'global' if codebase-independent, 'repo' if "
            "it relies on one codebase's layout, 'task' if it fits one instance. "
            "Return JSON only, shape: "
            '{"tools":[{"name","description","failure_pattern","scope_hypothesis"'
            ',"language","script","cli","rationale","safety_notes"}]}. '
            "name must match ^[a-z][a-z0-9_]{1,48}$. failure_pattern must be one "
            "of the provided patterns. Return an empty tools list if no reusable "
            "tool would help."
        )
        user = json.dumps(
            {
                "tool_failure_patterns": list(TOOL_FAILURE_PATTERNS),
                "scope_hypotheses": list(SCOPE_HYPOTHESES),
                "diagnosed_labels": labels,
                "optimizer_hint": hint,
                "trace": _trace_summary(record),
            },
            sort_keys=True,
            default=str,
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]


def parse_tool_payload(
    payload: Mapping[str, Any],
    *,
    source_task_id: str,
    source_repo: str,
    max_tools: int = 2,
) -> list[SynthesizedTool]:
    """Validate an LLM tool payload into :class:`SynthesizedTool` objects.

    Invalid individual entries are skipped (best-effort), so one malformed tool
    does not discard a good sibling. Each surviving tool carries its
    synthesis-time ``safety_flags``.
    """

    raw_tools = payload.get("tools")
    if not isinstance(raw_tools, list):
        return []
    tools: list[SynthesizedTool] = []
    for entry in raw_tools[: max(1, int(max_tools))]:
        try:
            tools.append(
                _validate_tool_entry(
                    entry,
                    source_task_id=source_task_id,
                    source_repo=source_repo,
                )
            )
        except ToolSynthesisError:
            continue
    return tools


def _validate_tool_entry(
    entry: Any,
    *,
    source_task_id: str,
    source_repo: str,
) -> SynthesizedTool:
    if not isinstance(entry, Mapping):
        raise ToolSynthesisError("tool entry is not an object")
    name = str(entry.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ToolSynthesisError(f"invalid tool name: {name!r}")
    script = entry.get("script")
    if not isinstance(script, str) or len(script.strip()) < 20:
        raise ToolSynthesisError("script missing or too short")
    failure_pattern = str(entry.get("failure_pattern") or "").strip()
    if failure_pattern not in TOOL_FAILURE_PATTERNS:
        raise ToolSynthesisError(f"unknown failure_pattern: {failure_pattern!r}")
    scope = str(entry.get("scope_hypothesis") or "").strip().lower()
    if scope not in SCOPE_HYPOTHESES:
        raise ToolSynthesisError(f"invalid scope_hypothesis: {scope!r}")
    return SynthesizedTool(
        name=name,
        description=str(entry.get("description") or "").strip(),
        failure_pattern=failure_pattern,
        scope_hypothesis=scope,
        language=str(entry.get("language") or "python").strip() or "python",
        script=script,
        cli=str(entry.get("cli") or "").strip(),
        rationale=str(entry.get("rationale") or "").strip(),
        safety_notes=str(entry.get("safety_notes") or "").strip(),
        source_task_id=source_task_id,
        source_repo=source_repo,
        safety_flags=basic_safety_scan(script),
    )


def _record_repo(record: Mapping[str, Any]) -> str:
    meta = record.get("metadata")
    if isinstance(meta, Mapping) and meta.get("repo"):
        return str(meta["repo"])
    return str(record.get("repo") or "")


def _trace_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, model-facing view of a trace. Excludes any oracle test content."""

    keys = (
        "task_id",
        "instance_id",
        "problem_statement",
        "issue_description",
        "model_patch",
        "patch",
        "inspected_files",
        "commands",
        "steps",
    )
    summary: dict[str, Any] = {}
    for key in keys:
        if key in record and record[key] not in (None, "", [], {}):
            summary[key] = record[key]
    return summary


def _parse_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object from a model response (tolerates fences)."""

    if not isinstance(text, str):
        raise ValueError("response content is not a string")
    stripped = text.strip()
    # Strip common ```json fences.
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped).strip()
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON object found in response")
        obj = json.loads(stripped[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("parsed JSON is not an object")
    return obj
