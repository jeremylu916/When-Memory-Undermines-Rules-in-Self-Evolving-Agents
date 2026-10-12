"""Utilities for loading fixed SWE-bench Lite subsets into BenchmarkTask objects."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from self_evolving_agent.benchmarks import BenchmarkTask


def load_swe_lite_subset(path: str | Path) -> list[BenchmarkTask]:
    """Load a fixed SWE-bench Lite subset snapshot.

    The snapshot is a committed JSON file extracted from the official
    ``princeton-nlp/SWE-bench_Lite`` dataset. Committing the 15-task subset keeps
    M1 reproducible and avoids requiring the ``datasets`` package in the
    ``evo`` environment.
    """
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = [task_from_snapshot(item, snapshot_meta=snapshot) for item in snapshot["tasks"]]
    return tasks


def task_from_snapshot(
    item: dict[str, Any],
    *,
    snapshot_meta: dict[str, Any] | None = None,
) -> BenchmarkTask:
    fail_to_pass = _parse_test_list(item.get("fail_to_pass", []))
    pass_to_pass = _parse_test_list(item.get("pass_to_pass", []))
    test_command_template = item.get(
        "test_command_template",
        "python -m pytest -q {test_target}",
    )
    test_commands = tuple(
        test_command_template.format(test_target=target) for target in fail_to_pass
    )

    metadata = {
        "source": "SWE-bench_Lite",
        "source_dataset": (snapshot_meta or {}).get("source_dataset"),
        "source_split": item.get("original_split", (snapshot_meta or {}).get("source_split")),
        "repo": item["repo"],
        "version": item.get("version"),
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "gold_patch": item.get("patch", ""),
        "environment_setup_commit": item.get("environment_setup_commit"),
    }

    return BenchmarkTask(
        task_id=item["instance_id"],
        split=item["split"],
        benchmark="swe-lite-subset",
        category="software",
        prompt=item["problem_statement"],
        issue_description=item["problem_statement"],
        # M1 still uses the oracle gold patch. The official harness is the real
        # evaluator; locally we only need the patch application to succeed.
        expected_output="patch_applied",
        repo_url=item.get("repo_url", f"https://github.com/{item['repo']}.git"),
        base_commit=item["base_commit"],
        test_commands=test_commands,
        test_patch=_text_block(item.get("test_patch", "")),
        command_timeout_s=item.get("command_timeout_s", 120),
        metadata=metadata,
    )


def _parse_test_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    raise ValueError(f"Could not parse SWE test target list: {value!r}")


def _text_block(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(line) for line in value)
    return str(value)
