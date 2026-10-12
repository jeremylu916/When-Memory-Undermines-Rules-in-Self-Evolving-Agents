"""Harness-run tool augmentation for the Agentless agent (Trace2Tool, piece 3a).

The ExecutionAgent is Agentless: it cannot call tools while solving (its own
prompt states it has no shell). A synthesized tool therefore cannot be *invoked*
by the agent; instead the harness runs eligible library tools on the workspace
*before* the agent acts and injects their stdout into the issue text the agent
embeds in its prompt. This wrapper is non-invasive: it augments the task, then
delegates to the real agent, so no core agent code changes.

Each tool obeys the standard interface enforced at synthesis time:
``python <tool>.py --repo-root <DIR> --issue-file <FILE>`` -> concise localization
hints on stdout. Tools run read-only, sandboxed to a per-run temp dir for the
script itself (never written into the workspace, so they cannot pollute the
captured ``git diff``), with a wall-clock timeout and output cap. A failing or
silent tool contributes nothing and never breaks the run.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from self_evolving_agent.tool_library import LibraryTool, ToolLibrary

DEFAULT_TOOL_TIMEOUT_S = 60
DEFAULT_MAX_OUTPUT_CHARS = 4000
DEFAULT_MAX_SOURCE_FILES = 3
DEFAULT_MAX_CHARS_PER_FILE = 6000

# Any repo-relative python path token in tool stdout, e.g. "sklearn/grid_search.py".
_PYPATH_RE = re.compile(r"([A-Za-z0-9_][\w./-]*\.py)")
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_|_test\.py$|conftest\.py$", re.IGNORECASE)


@dataclass
class ToolRunResult:
    name: str
    scope: str
    ok: bool
    output: str
    error: str = ""


def run_library_tool(
    entry: LibraryTool,
    *,
    repo_root: Path,
    issue_file: Path,
    timeout_s: int = DEFAULT_TOOL_TIMEOUT_S,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    scripts_dir: Path | None = None,
) -> ToolRunResult:
    """Run one tool read-only against the workspace; capture stdout.

    The script is written OUTSIDE the workspace (to ``scripts_dir`` or a temp
    dir) so it never appears in the workspace ``git diff``. Any failure is
    swallowed into an empty, non-fatal result.
    """

    name = entry.tool.name
    cleanup = None
    try:
        if scripts_dir is None:
            tmp = tempfile.mkdtemp(prefix="t2t_tool_")
            scripts_dir = Path(tmp)
            cleanup = tmp
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_path = scripts_dir / f"{name}.py"
        script_path.write_text(entry.tool.script, encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--repo-root",
                str(repo_root),
                "--issue-file",
                str(issue_file),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(repo_root),
        )
        out = (proc.stdout or "")[:max_output_chars].strip()
        if proc.returncode != 0 and not out:
            return ToolRunResult(name, entry.calibrated_scope, False, "", (proc.stderr or "")[:400])
        return ToolRunResult(name, entry.calibrated_scope, bool(out), out)
    except subprocess.TimeoutExpired:
        return ToolRunResult(name, entry.calibrated_scope, False, "", "timeout")
    except Exception as exc:  # pragma: no cover - defensive; tools must never break a run.
        return ToolRunResult(name, entry.calibrated_scope, False, "", str(exc)[:400])
    finally:
        if cleanup is not None:
            import shutil

            shutil.rmtree(cleanup, ignore_errors=True)


def render_tool_context(results: list[ToolRunResult]) -> str:
    """Render non-empty tool outputs into a compact hint block (legacy mode)."""

    blocks = [r for r in results if r.ok and r.output]
    if not blocks:
        return ""
    lines = [
        "",
        "Additional localization hints from repository analysis tools "
        "(read-only; treat as hints, not ground truth):",
    ]
    for r in blocks:
        lines.append(f"<tool name=\"{r.name}\">")
        lines.append(r.output)
        lines.append("</tool>")
    return "\n".join(lines)


def extract_surfaced_files(
    results: list[ToolRunResult],
    workspace_path: Path,
    *,
    max_files: int = DEFAULT_MAX_SOURCE_FILES,
) -> list[str]:
    """Rank repo-relative .py files the tools named, by mention frequency.

    Keeps only non-test files that actually exist in the workspace, so the
    surfaced files can be loaded as full-source context. This is what converts a
    tool's localization from a buried text pointer into the code the agent reads.
    """

    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    seq = 0
    for r in results:
        if not (r.ok and r.output):
            continue
        for m in _PYPATH_RE.finditer(r.output):
            raw = m.group(1).lstrip("./")
            if _TEST_PATH.search(raw):
                continue
            # Normalize away leading build/ prefixes that some tools emit.
            cand = raw
            if not (workspace_path / cand).is_file():
                # try trimming a leading "build/lib/" style prefix
                parts = cand.split("/")
                for i in range(1, len(parts)):
                    sub = "/".join(parts[i:])
                    if (workspace_path / sub).is_file():
                        cand = sub
                        break
            if not (workspace_path / cand).is_file():
                continue
            counts[cand] = counts.get(cand, 0) + 1
            if cand not in order:
                order[cand] = seq
                seq += 1
    ranked = sorted(counts, key=lambda p: (-counts[p], order[p]))
    return ranked[:max_files]


def render_source_context(
    files: list[str],
    workspace_path: Path,
    *,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
) -> str:
    """Load the surfaced files' source into a primary-context block."""

    blocks: list[str] = []
    for path in files:
        try:
            text = (workspace_path / path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(text) > max_chars_per_file:
            text = text[:max_chars_per_file] + "\n... [truncated]"
        blocks.append(f"<file path=\"{path}\" kind=\"tool_surfaced\">\n{text}\n</file>")
    if not blocks:
        return ""
    header = (
        "\nPrimary suspect files identified by repository-analysis tools "
        "(most likely to contain the fix; prioritize these):"
    )
    return header + "\n" + "\n".join(blocks)


class ToolAugmentedAgent:
    """Wraps an Agentless agent; injects library-tool output into the issue text.

    ``agent_factory`` builds the underlying agent from the config (defaults to
    ExecutionAgent). ``library`` supplies scope-eligible tools per task. Set
    ``k=0`` (or an empty library) to make this a transparent pass-through, which
    is exactly the no-tool control arm.
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        library: ToolLibrary | None = None,
        k: int = 3,
        timeout_s: int = DEFAULT_TOOL_TIMEOUT_S,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        inject_source: bool = True,
        max_source_files: int = DEFAULT_MAX_SOURCE_FILES,
        max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
        agent_factory: Callable[[dict[str, Any]], Any] | None = None,
        run_tool_fn: Callable[..., ToolRunResult] | None = None,
    ):
        self.config = config
        self.library = library if library is not None else ToolLibrary()
        self.k = max(0, int(k))
        self.timeout_s = int(timeout_s)
        self.max_output_chars = int(max_output_chars)
        # inject_source=True: load tool-surfaced files as full-source primary
        # context (the integration that converts localization into resolved
        # tasks). False: legacy behavior (append raw hint text).
        self.inject_source = bool(inject_source)
        self.max_source_files = int(max_source_files)
        self.max_chars_per_file = int(max_chars_per_file)
        self._agent_factory = agent_factory
        self._run_tool_fn = run_tool_fn or run_library_tool
        self.last_tool_results: list[ToolRunResult] = []
        self.last_surfaced_files: list[str] = []
        self._inner = self._build_inner(config)

    def _build_inner(self, config: dict[str, Any]):
        if self._agent_factory is not None:
            return self._agent_factory(config)
        from self_evolving_agent.agent import ExecutionAgent

        return ExecutionAgent(config)

    def solve(self, task: Any, workspace_path: Path | None = None) -> Any:
        self.last_tool_results = []
        if self.k > 0 and workspace_path is not None and len(self.library) > 0:
            augmented = self._augment_task(task, Path(workspace_path))
        else:
            augmented = task
        return self._inner.solve(augmented, workspace_path=workspace_path)

    def _augment_task(self, task: Any, workspace_path: Path):
        repo = str((getattr(task, "metadata", None) or {}).get("repo", ""))
        task_id = str(getattr(task, "task_id", ""))
        issue_text = str(getattr(task, "prompt", "") or getattr(task, "issue_description", ""))
        entries = self.library.retrieve(
            issue_text=issue_text, repo=repo, task_id=task_id, k=self.k
        )
        if not entries:
            return task
        # Write the issue to a file the tools read.
        scripts_dir = workspace_path.parent / f"{workspace_path.name}__t2t"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        issue_file = scripts_dir / "issue.txt"
        issue_file.write_text(issue_text, encoding="utf-8")
        results = [
            self._run_tool_fn(
                entry,
                repo_root=workspace_path,
                issue_file=issue_file,
                timeout_s=self.timeout_s,
                max_output_chars=self.max_output_chars,
                scripts_dir=scripts_dir,
            )
            for entry in entries
        ]
        self.last_tool_results = results
        self.last_surfaced_files = []
        if self.inject_source:
            files = extract_surfaced_files(
                results, workspace_path, max_files=self.max_source_files
            )
            self.last_surfaced_files = files
            context_block = render_source_context(
                files, workspace_path, max_chars_per_file=self.max_chars_per_file
            )
        else:
            context_block = render_tool_context(results)
        if not context_block:
            return task
        return _task_with_appended_prompt(task, context_block)


def _task_with_appended_prompt(task: Any, extra: str) -> Any:
    """Return a copy of the task with ``extra`` appended to its prompt/issue.

    Works for dataclass-like BenchmarkTask (via dataclasses.replace) and falls
    back to a shallow copy with attributes set.
    """

    import copy
    import dataclasses

    base_prompt = str(getattr(task, "prompt", "") or "")
    new_prompt = base_prompt + "\n" + extra
    if dataclasses.is_dataclass(task):
        try:
            return dataclasses.replace(task, prompt=new_prompt)
        except Exception:
            pass
    clone = copy.copy(task)
    try:
        setattr(clone, "prompt", new_prompt)
    except Exception:
        return task
    return clone
