"""Serializable, immutable inputs for skill-only retention experiments."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")


@dataclass(frozen=True)
class Skill:
    skill_id: str
    version: int
    text: str
    applicability: tuple[str, ...] = ()

    def __post_init__(self):
        require_text(self.skill_id, "skill_id")
        require_text(self.text, "skill text")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("skill version must be a positive integer")
        object.__setattr__(self, "applicability", tuple(self.applicability))
        for tag in self.applicability:
            require_text(tag, "applicability tag")


@dataclass(frozen=True)
class SkillLibrary:
    skills: tuple[Skill, ...]

    def __post_init__(self):
        object.__setattr__(self, "skills", tuple(sorted(self.skills, key=lambda s: s.skill_id)))
        if len({s.skill_id for s in self.skills}) != len(self.skills):
            raise ValueError("duplicate skill ID")

    @property
    def fingerprint(self) -> str:
        return digest(self.to_dict())

    def to_dict(self) -> dict:
        return {"skills": [asdict(skill) for skill in self.skills]}

    @classmethod
    def from_dict(cls, value: dict) -> SkillLibrary:
        return cls(tuple(Skill(**row) for row in value["skills"]))


@dataclass(frozen=True)
class SkillEdit:
    update_id: str
    skill_id: str
    old_version: int
    old_text: str
    new_text: str
    claimed_benefit: str
    old_applicability: tuple[str, ...]
    new_applicability: tuple[str, ...]
    source_task_ids: tuple[str, ...]

    def __post_init__(self):
        for name in ("update_id", "skill_id", "old_text", "new_text", "claimed_benefit"):
            require_text(getattr(self, name), name)
        for name in ("old_applicability", "new_applicability", "source_task_ids"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
            for item in getattr(self, name):
                require_text(item, name)
        if type(self.old_version) is not int or self.old_version < 1:
            raise ValueError("old_version must be a positive integer")
        if not self.source_task_ids or len(set(self.source_task_ids)) != len(self.source_task_ids):
            raise ValueError("candidate requires distinct source task IDs")
        if self.old_text == self.new_text and self.old_applicability == self.new_applicability:
            raise ValueError("candidate must change text or applicability")

    def apply(self, library: SkillLibrary) -> SkillLibrary:
        matches = [s for s in library.skills if s.skill_id == self.skill_id]
        if len(matches) != 1:
            raise ValueError("edited skill is absent from the library")
        old = matches[0]
        if (old.version, old.text, old.applicability) != (
                self.old_version, self.old_text, self.old_applicability):
            raise ValueError("stale edit: old skill/version/applicability differs")
        new = replace(old, version=old.version + 1, text=self.new_text,
                      applicability=self.new_applicability)
        return SkillLibrary(tuple(new if s.skill_id == self.skill_id else s for s in library.skills))


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    repository: str
    snapshot: str
    issue: str
    test_command: tuple[str, ...]
    sequence: int
    panel: str
    previously_solved: bool
    retrieved_skill_ids: tuple[str, ...] = ()
    trace_events: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("task_id", "repository", "issue"):
            require_text(getattr(self, name), name)
        if not Path(self.repository).is_absolute():
            raise ValueError("repository must be an absolute local path")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.snapshot):
            raise ValueError("snapshot must be an immutable full Git commit ID")
        if self.panel not in {"known", "new", "heldout"}:
            raise ValueError("panel must be known, new, or heldout")
        if type(self.previously_solved) is not bool:
            raise ValueError("previously_solved must be boolean")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must be a nonnegative integer")
        for name in ("test_command", "retrieved_skill_ids", "trace_events", "capability_tags"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
            for item in getattr(self, name):
                require_text(item, name)
        if not self.test_command:
            raise ValueError("test_command cannot be empty")

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class FrozenAgent:
    model: str
    model_revision: str
    prompt_template: str
    tools_digest: str
    runner_digest: str
    max_steps: int
    max_tokens: int
    temperature: float = 0.0

    def __post_init__(self):
        for name in ("model", "model_revision", "prompt_template", "tools_digest", "runner_digest"):
            require_text(getattr(self, name), name)
        if any(type(x) is not int or x < 1 for x in (self.max_steps, self.max_tokens)):
            raise ValueError("frozen budgets must be positive integers")
        if not math.isfinite(self.temperature) or self.temperature < 0:
            raise ValueError("invalid temperature")

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))
