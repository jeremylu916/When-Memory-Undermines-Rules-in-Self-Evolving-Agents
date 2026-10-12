"""Leakage-safe loader for SWE-bench Verified development subsets."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from self_evolving_agent.benchmarks import BenchmarkTask


VERIFIED_BENCHMARK_IDENTITY = "swe-bench-verified"
VERIFIED_SUBSET_SCHEMA_VERSION = "swe_verified_subset.v1"
VERIFIED_SUBSET_TASK_COUNT = 20
VERIFIED_MIN_REPOSITORIES = 10
VERIFIED_MAX_INSTANCES_PER_REPOSITORY = 2

REQUIRED_ISSUE_TYPES = frozenset(
    {
        "localization_heavy",
        "api_behavioral_correctness",
        "regression_sensitive",
        "parsing_serialization_formatting",
        "framework_configuration",
        "scientific_numerical",
        "error_handling_edge_cases",
        "multi_file_cross_module",
    }
)

_REQUIRED_TOP_LEVEL_FIELDS = {
    "schema_version",
    "name",
    "benchmark_identity",
    "source_dataset",
    "source_split",
    "source_revision",
    "subset_fingerprint",
    "task_count",
    "tasks",
}

_REQUIRED_TASK_FIELDS = {
    "instance_id",
    "repository",
    "base_commit",
    "version",
    "problem_statement",
    "source_dataset",
    "source_split",
    "official_image",
    "development_split",
    "stratum",
    "issue_type_tags",
}

_LEAKAGE_SENSITIVE_FIELDS = {
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


def load_swe_verified_subset(path: str | Path) -> list[BenchmarkTask]:
    """Load and validate the immutable Verified-20 generation snapshot."""
    return _load_swe_verified_subset_impl(path, strict_composition=True)


def load_swe_verified_subset_flex(path: str | Path) -> list[BenchmarkTask]:
    """Load a Verified subset with arbitrary task count (baseline manifest mode).

    Use this for manifests other than the fixed Verified-20 development subset,
    such as the 30-task implementation baseline. Structural and leakage-safety
    validations still run; only the composition constraints (exact count,
    repository diversity caps, required issue-type coverage, 10/10 split) are
    relaxed.
    """
    return _load_swe_verified_subset_impl(path, strict_composition=False)


def _load_swe_verified_subset_impl(
    path: str | Path,
    *,
    strict_composition: bool,
) -> list[BenchmarkTask]:
    snapshot = load_swe_verified_snapshot(path, strict_composition=strict_composition)
    fingerprint = str(snapshot["subset_fingerprint"])
    tasks: list[BenchmarkTask] = []
    for item in snapshot["tasks"]:
        repository = str(item["repository"])
        tasks.append(
            BenchmarkTask(
                task_id=str(item["instance_id"]),
                split=str(item["development_split"]),
                benchmark="swe-verified-subset",
                category="software",
                prompt=str(item["problem_statement"]),
                expected_output="patch_applied",
                issue_description=str(item["problem_statement"]),
                repo_url=f"https://github.com/{repository}.git",
                base_commit=str(item["base_commit"]),
                setup_commands=(),
                test_commands=(),
                test_patch="",
                command_timeout_s=120,
                metadata={
                    "source": VERIFIED_BENCHMARK_IDENTITY,
                    "source_dataset": str(item["source_dataset"]),
                    "source_split": str(item["source_split"]),
                    "source_revision": str(snapshot["source_revision"]),
                    "repo": repository,
                    "version": str(item["version"]),
                    "official_image": str(item["official_image"]),
                    "stratum": str(item["stratum"]),
                    "issue_type_tags": list(item["issue_type_tags"]),
                    "subset_fingerprint": fingerprint,
                    "leakage_safe": True,
                },
            )
        )
    return tasks


def load_swe_verified_snapshot(
    path: str | Path,
    strict_composition: bool = True,
) -> dict[str, Any]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    validate_swe_verified_snapshot(
        payload, source=source, strict_composition=strict_composition
    )
    return payload


def validate_swe_verified_snapshot(
    payload: Any,
    *,
    source: str | Path = "<memory>",
    strict_composition: bool = True,
) -> None:
    """Validate a SWE-bench Verified generation snapshot.

    When ``strict_composition=True`` (default), enforces the composition rules
    that apply to the fixed Verified-20 development subset: exactly 20 tasks,
    minimum 10 repositories, at most 2 per repository, all 8 required issue
    types represented, and a balanced 10/10 train/heldout split.

    When ``strict_composition=False``, those constraints are relaxed so that
    arbitrary baseline manifests (e.g. the 30-task implementation baseline) can
    be validated. Structural integrity checks (required fields, leakage-field
    rejection, ordering, uniqueness, task_count header consistency, and
    fingerprint) always run regardless of this flag.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"Verified subset must be a JSON object: {source}")
    missing = _REQUIRED_TOP_LEVEL_FIELDS - set(payload)
    if missing:
        raise ValueError(
            f"Verified subset missing top-level fields {sorted(missing)}: {source}"
        )
    _reject_leakage_fields(payload, location="top level", recurse_tasks=False)
    if payload.get("schema_version") != VERIFIED_SUBSET_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Verified subset schema: {payload.get('schema_version')}"
        )
    if payload.get("benchmark_identity") != VERIFIED_BENCHMARK_IDENTITY:
        raise ValueError("Verified subset benchmark_identity is invalid")

    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Verified subset tasks must be a list")
    task_count = len(tasks)
    if task_count < 1:
        raise ValueError("Verified subset must contain at least one task")
    if strict_composition and task_count != VERIFIED_SUBSET_TASK_COUNT:
        raise ValueError(
            f"Verified subset must contain exactly {VERIFIED_SUBSET_TASK_COUNT} tasks"
        )
    if int(payload.get("task_count") or 0) != task_count:
        raise ValueError("Verified subset task_count does not match the task list")

    task_ids: list[str] = []
    repositories: list[str] = []
    represented_types: set[str] = set()
    split_counts: Counter[str] = Counter()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"Verified subset task {index} must be an object")
        missing_task = _REQUIRED_TASK_FIELDS - set(task)
        if missing_task:
            raise ValueError(
                f"Verified subset task {index} missing fields {sorted(missing_task)}"
            )
        _reject_leakage_fields(task, location=f"task {index}", recurse_tasks=True)
        task_id = str(task["instance_id"])
        repository = str(task["repository"])
        tags = task.get("issue_type_tags")
        if not isinstance(tags, list) or not tags:
            raise ValueError(f"Verified subset task {task_id} has no issue type tags")
        if task.get("source_dataset") != payload.get("source_dataset"):
            raise ValueError(f"Verified subset task {task_id} source_dataset mismatch")
        if task.get("source_split") != payload.get("source_split"):
            raise ValueError(f"Verified subset task {task_id} source_split mismatch")
        if task.get("development_split") not in {
            "development_train",
            "development_heldout",
        }:
            raise ValueError(
                f"Verified subset task {task_id} has invalid development split"
            )
        if not str(task.get("official_image") or "").startswith(
            "swebench/sweb.eval."
        ):
            raise ValueError(f"Verified subset task {task_id} has invalid image")
        task_ids.append(task_id)
        repositories.append(repository)
        represented_types.update(str(tag) for tag in tags)
        split_counts[str(task["development_split"])] += 1

    if len(set(task_ids)) != task_count:
        raise ValueError("Verified subset instance IDs must be unique")
    if task_ids != sorted(task_ids):
        raise ValueError("Verified subset tasks must be ordered by instance_id")

    if strict_composition:
        repository_counts = Counter(repositories)
        if len(repository_counts) < VERIFIED_MIN_REPOSITORIES:
            raise ValueError(
                f"Verified subset must contain at least {VERIFIED_MIN_REPOSITORIES} repositories"
            )
        if max(repository_counts.values(), default=0) > VERIFIED_MAX_INSTANCES_PER_REPOSITORY:
            raise ValueError(
                "Verified subset may contain at most "
                f"{VERIFIED_MAX_INSTANCES_PER_REPOSITORY} instances per repository"
            )
        missing_types = REQUIRED_ISSUE_TYPES - represented_types
        if missing_types:
            raise ValueError(
                f"Verified subset missing issue type coverage: {sorted(missing_types)}"
            )
        if split_counts != Counter(
            {"development_train": 10, "development_heldout": 10}
        ):
            raise ValueError("Verified subset must contain a deterministic 10/10 split")

    expected_fingerprint = swe_verified_subset_fingerprint(payload)
    if payload.get("subset_fingerprint") != expected_fingerprint:
        raise ValueError(
            "Verified subset fingerprint mismatch: "
            f"expected {expected_fingerprint}, got {payload.get('subset_fingerprint')}"
        )


def swe_verified_subset_fingerprint(payload: dict[str, Any]) -> str:
    canonical = {
        "benchmark_identity": payload.get("benchmark_identity"),
        "source_dataset": payload.get("source_dataset"),
        "source_revision": payload.get("source_revision"),
        "tasks": payload.get("tasks"),
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_leakage_fields(
    value: dict[str, Any],
    *,
    location: str,
    recurse_tasks: bool,
) -> None:
    present = _LEAKAGE_SENSITIVE_FIELDS & set(value)
    if present:
        raise ValueError(
            f"Leakage-sensitive fields are forbidden in Verified {location}: "
            f"{sorted(present)}"
        )
    if not recurse_tasks:
        return
    for key, item in value.items():
        if isinstance(item, dict):
            _reject_leakage_fields(
                item,
                location=f"{location}.{key}",
                recurse_tasks=True,
            )
