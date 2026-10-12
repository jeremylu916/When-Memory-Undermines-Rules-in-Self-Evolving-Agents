"""Outcome-blind selectors; ranking reasons are saved before any evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
import random
import re

from .contracts import SkillEdit, TaskRecord


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_]+", text.lower()))


@dataclass(frozen=True)
class Selection:
    task_id: str
    score: float
    reasons: tuple[str, ...]

    def to_dict(self):
        return asdict(self)


def change_score(task: TaskRecord, edit: SkillEdit, source_repositories: set[str]) -> Selection:
    score, reasons = 0.0, []
    linked = edit.skill_id in task.retrieved_skill_ids
    if linked:
        score += 8.0
        reasons.append("retrieved_edited_skill")
    old_scope, new_scope = set(edit.old_applicability), set(edit.new_applicability)
    tags = set(task.capability_tags)
    # Empty scope denotes all tasks. This also catches newly affected tasks
    # which could not have retrieved the old, narrower skill.
    old_match = not old_scope or bool(tags & old_scope)
    new_match = not new_scope or bool(tags & new_scope)
    if old_match != new_match:
        score += 6.0
        reasons.append("crosses_applicability_boundary")
    elif old_scope != new_scope and linked:
        score += 3.0
        reasons.append("linked_skill_scope_changed")
    if linked and source_repositories and task.repository not in source_repositories:
        score += 3.0
        reasons.append("linked_cross_repository")
    changed = " ".join(line[2:] for line in difflib.ndiff(
        edit.old_text.splitlines(), edit.new_text.splitlines()) if line[:2] in {"+ ", "- "})
    edit_tokens = tokens(changed)
    context_tokens = tokens(" ".join((task.issue, *task.trace_events, *task.capability_tags)))
    overlap = len(edit_tokens & context_tokens) / max(1, len(edit_tokens))
    score += 2.0 * overlap
    if overlap:
        reasons.append("changed_procedure_overlap")
    return Selection(task.task_id, round(score, 6), tuple(reasons or ["fallback"]))


def select_tasks(tasks: list[TaskRecord], edit: SkillEdit, *, method: str,
                 budget: int = 10, seed: int = 0,
                 source_repositories: set[str] | None = None) -> list[Selection]:
    if type(budget) is not int or budget < 1:
        raise ValueError("selection budget must be positive")
    if len({t.task_id for t in tasks}) != len(tasks):
        raise ValueError("duplicate task IDs")
    eligible = sorted((t for t in tasks if t.panel == "known"
                       and t.task_id not in edit.source_task_ids), key=lambda t: t.task_id)
    if method == "change_aware":
        rows = [change_score(t, edit, source_repositories or set()) for t in eligible]
        return sorted(rows, key=lambda row: (-row.score, row.task_id))[:budget]
    if method == "random":
        random.Random(seed).shuffle(eligible)
    elif method in {"recent", "recent_diverse"}:
        eligible.sort(key=lambda t: (-t.sequence, t.task_id))
        if method == "recent_diverse":
            groups: dict[str, list[TaskRecord]] = {}
            for task in eligible:
                groups.setdefault(task.repository, []).append(task)
            eligible = []
            while any(groups.values()):
                for group in groups.values():
                    if group:
                        eligible.append(group.pop(0))
    else:
        raise ValueError(f"unknown selector: {method}")
    return [Selection(t.task_id, 0.0, (method,)) for t in eligible[:budget]]
