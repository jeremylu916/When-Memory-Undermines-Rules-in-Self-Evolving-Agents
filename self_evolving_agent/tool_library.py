"""Persistent tool library with scope-eligible retrieval (Trace2Tool, piece 2).

The property missing from episode-local tool synthesis: tools that *persist*
across tasks and rounds. This module stores :class:`SynthesizedTool` entries with
a *calibrated* scope (task/repo/global) and retrieves the top-k scope-eligible
tools for a new task. Global tools are always eligible; repository- and
task-scoped tools are eligible only when the active task matches their scope, so
a codebase-specific tool never enriches an unrelated repository's context.

The store is a plain JSON file so a run can accumulate a library across rounds
and a later transfer gate can promote/demote entries in place. Retrieval scoring
is deterministic (token overlap between the issue and the tool's
description/failure pattern) so it is unit-testable without embeddings.

Scope semantics mirror :mod:`memory_transfer_gate`: ``scope_hypothesis`` is the
optimizer's guess; ``calibrated_scope`` is what the transfer gate decides. Until
gated, ``calibrated_scope`` defaults to the hypothesis.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from self_evolving_agent.tool_synthesis import (
    SCOPE_HYPOTHESES,
    SynthesizedTool,
)

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")
_SCOPE_RANK = {"task": 0, "repo": 1, "global": 2}


@dataclass(frozen=True)
class LibraryTool:
    """A persisted tool: the synthesized script plus its calibrated scope.

    ``calibrated_scope`` starts equal to the synthesis hypothesis and is
    overwritten by the transfer gate. ``transfer_gain`` records the measured
    T(t) once gated (None before). ``scope_id`` binds a repo/task-scoped tool to
    the codebase or instance it is eligible for.
    """

    tool: SynthesizedTool
    calibrated_scope: str
    scope_id: str = ""
    transfer_gain: float | None = None
    promoted_round: int = -1

    def to_audit(self) -> dict[str, Any]:
        return {
            "name": self.tool.name,
            "calibrated_scope": self.calibrated_scope,
            "scope_id": self.scope_id,
            "scope_hypothesis": self.tool.scope_hypothesis,
            "transfer_gain": self.transfer_gain,
            "failure_pattern": self.tool.failure_pattern,
            "source_repo": self.tool.source_repo,
            "promoted_round": self.promoted_round,
        }


class ToolLibrary:
    """Persistent, scope-annotated store of synthesized tools."""

    def __init__(self, tools: Iterable[LibraryTool] | None = None):
        self._tools: list[LibraryTool] = list(tools or [])

    # -- persistence -----------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "ToolLibrary":
        p = Path(path)
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text(encoding="utf-8"))
        tools = [_library_tool_from_dict(entry) for entry in raw.get("tools", [])]
        return cls(tools)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tools": [_library_tool_to_dict(t) for t in self._tools]}
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # -- mutation --------------------------------------------------------
    def add(
        self,
        tool: SynthesizedTool,
        *,
        calibrated_scope: str | None = None,
        scope_id: str | None = None,
        transfer_gain: float | None = None,
        promoted_round: int = -1,
    ) -> LibraryTool:
        """Add a tool. Rejected (unsafe) tools are refused. De-dupes by name."""

        if tool.rejected:
            raise ValueError(f"refusing unsafe tool {tool.name!r}: {tool.safety_flags}")
        scope = (calibrated_scope or tool.scope_hypothesis).lower()
        if scope not in SCOPE_HYPOTHESES:
            raise ValueError(f"invalid scope {scope!r}")
        sid = scope_id if scope_id is not None else _default_scope_id(scope, tool)
        entry = LibraryTool(
            tool=tool,
            calibrated_scope=scope,
            scope_id=sid,
            transfer_gain=transfer_gain,
            promoted_round=promoted_round,
        )
        self._tools = [t for t in self._tools if t.tool.name != tool.name]
        self._tools.append(entry)
        return entry

    def recalibrate(
        self,
        name: str,
        *,
        calibrated_scope: str,
        scope_id: str | None = None,
        transfer_gain: float | None = None,
    ) -> None:
        """Overwrite a tool's calibrated scope (the transfer gate's output)."""

        scope = calibrated_scope.lower()
        if scope not in SCOPE_HYPOTHESES:
            raise ValueError(f"invalid scope {scope!r}")
        for i, entry in enumerate(self._tools):
            if entry.tool.name == name:
                self._tools[i] = replace(
                    entry,
                    calibrated_scope=scope,
                    scope_id=scope_id if scope_id is not None else entry.scope_id,
                    transfer_gain=transfer_gain if transfer_gain is not None else entry.transfer_gain,
                )
                return
        raise KeyError(name)

    # -- retrieval -------------------------------------------------------
    def eligible(self, *, repo: str, task_id: str) -> list[LibraryTool]:
        """Tools whose scope admits this task (global always; repo/task on match)."""

        out: list[LibraryTool] = []
        for entry in self._tools:
            scope = entry.calibrated_scope
            if scope == "global":
                out.append(entry)
            elif scope == "repo" and entry.scope_id and entry.scope_id == repo:
                out.append(entry)
            elif scope == "task" and entry.scope_id and entry.scope_id == task_id:
                out.append(entry)
        return out

    def retrieve(
        self,
        *,
        issue_text: str,
        repo: str,
        task_id: str,
        k: int = 3,
    ) -> list[LibraryTool]:
        """Top-k scope-eligible tools by relevance to the issue.

        Ties and zero-overlap tools are broken by scope breadth (global first,
        then repo, then task) so a generally-useful tool is preferred, then by
        name for determinism.
        """

        pool = self.eligible(repo=repo, task_id=task_id)
        issue_tokens = _tokens(issue_text)
        scored = [
            (_relevance(entry, issue_tokens), _SCOPE_RANK.get(entry.calibrated_scope, 0), entry)
            for entry in pool
        ]
        scored.sort(key=lambda s: (-s[0], -s[1], s[2].tool.name))
        return [entry for _score, _rank, entry in scored[: max(0, int(k))]]

    # -- introspection ---------------------------------------------------
    def __len__(self) -> int:
        return len(self._tools)

    def tools(self) -> list[LibraryTool]:
        return list(self._tools)

    def by_scope(self) -> dict[str, int]:
        counts = {"task": 0, "repo": 0, "global": 0}
        for entry in self._tools:
            counts[entry.calibrated_scope] = counts.get(entry.calibrated_scope, 0) + 1
        return counts


def _default_scope_id(scope: str, tool: SynthesizedTool) -> str:
    if scope == "repo":
        return tool.source_repo
    if scope == "task":
        return tool.source_task_id
    return ""


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _relevance(entry: LibraryTool, issue_tokens: set[str]) -> float:
    tool_tokens = _tokens(
        " ".join(
            [
                entry.tool.description,
                entry.tool.failure_pattern,
                entry.tool.name.replace("_", " "),
                entry.tool.rationale,
            ]
        )
    )
    if not tool_tokens:
        return 0.0
    return len(tool_tokens & issue_tokens) / len(tool_tokens)


def _library_tool_to_dict(entry: LibraryTool) -> dict[str, Any]:
    return {
        "tool": asdict(entry.tool),
        "calibrated_scope": entry.calibrated_scope,
        "scope_id": entry.scope_id,
        "transfer_gain": entry.transfer_gain,
        "promoted_round": entry.promoted_round,
    }


def _library_tool_from_dict(data: Mapping[str, Any]) -> LibraryTool:
    td = dict(data["tool"])
    # safety_flags persisted as list -> tuple.
    td["safety_flags"] = tuple(td.get("safety_flags", ()) or ())
    tool = SynthesizedTool(**td)
    return LibraryTool(
        tool=tool,
        calibrated_scope=str(data.get("calibrated_scope", tool.scope_hypothesis)),
        scope_id=str(data.get("scope_id", "")),
        transfer_gain=data.get("transfer_gain"),
        promoted_round=int(data.get("promoted_round", -1)),
    )
