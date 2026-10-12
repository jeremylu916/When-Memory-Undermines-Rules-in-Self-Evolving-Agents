"""Mock benchmark adapters used before wiring real SWE-bench or CVE-bench."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Protocol


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    split: str
    benchmark: str
    category: str
    prompt: str
    expected_output: str
    issue_description: str = ""
    repo_path: str | None = None
    repo_url: str | None = None
    base_commit: str | None = None
    setup_commands: tuple[str, ...] = ()
    test_commands: tuple[str, ...] = ()
    test_patch: str = ""
    command_timeout_s: int | None = None
    metadata: dict[str, Any] | None = None


class BenchmarkAdapter(Protocol):
    name: str

    def train_tasks(self) -> list[BenchmarkTask]:
        """Return tasks used for trace collection and offline optimization."""

    def heldout_tasks(self) -> list[BenchmarkTask]:
        """Return tasks used only for candidate comparison and promotion."""


class MockBenchmark:
    """Small deterministic benchmark with SWE-like and CVE-like tasks."""

    name = "mock"

    def train_tasks(self) -> list[BenchmarkTask]:
        return [
            BenchmarkTask(
                task_id="mock-swe-add-one",
                split="train",
                benchmark=self.name,
                category="software",
                prompt="Patch add_one(x) so it returns x + 1.",
                expected_output="return x + 1",
                issue_description="add_one returns the wrong value for simple inputs.",
            ),
            BenchmarkTask(
                task_id="mock-cve-shell-escape",
                split="train",
                benchmark=self.name,
                category="security",
                prompt="Repair command construction so user input is shell escaped.",
                expected_output="use shlex.quote(user_input)",
                issue_description="Command construction is vulnerable to shell injection.",
            ),
        ]

    def heldout_tasks(self) -> list[BenchmarkTask]:
        return [
            BenchmarkTask(
                task_id="mock-swe-uppercase",
                split="heldout",
                benchmark=self.name,
                category="software",
                prompt="Patch normalize_name(name) so it returns an uppercase name.",
                expected_output="return name.upper()",
                issue_description="normalize_name should uppercase names.",
            ),
            BenchmarkTask(
                task_id="mock-cve-sql-params",
                split="heldout",
                benchmark=self.name,
                category="security",
                prompt="Repair SQL construction so user input is passed as a parameter.",
                expected_output="use parameterized query",
                issue_description="SQL construction interpolates user input directly.",
            ),
        ]


class LocalRepoBenchmark:
    """Toy repository benchmark that exercises copy, edit, test, and diff flow."""

    name = "local-toy"

    def __init__(self, repo_path: str | None = None):
        self.repo_path = repo_path or str(
            Path(__file__).resolve().parent.parent / "fixtures" / "toy_repo"
        )

    def train_tasks(self) -> list[BenchmarkTask]:
        return [
            BenchmarkTask(
                task_id="local-toy-add-one",
                split="train",
                benchmark=self.name,
                category="software",
                prompt="Fix add_one(x) in math_utils.py so the focused unit test passes.",
                expected_output="patch_applied",
                issue_description="add_one subtracts one instead of adding one.",
                repo_path=self.repo_path,
                setup_commands=(),
                test_commands=(
                    f'"{sys.executable}" -m unittest test_math_utils.TestMathUtils.test_add_one',
                ),
                command_timeout_s=10,
                metadata={
                    "patch_file": "math_utils.py",
                    "find": "return x - 1",
                    "replace": "return x + 1",
                },
            ),
        ]

    def heldout_tasks(self) -> list[BenchmarkTask]:
        return [
            BenchmarkTask(
                task_id="local-toy-double",
                split="heldout",
                benchmark=self.name,
                category="software",
                prompt="Fix double(x) in math_utils.py so the focused unit test passes.",
                expected_output="patch_applied",
                issue_description="double adds two instead of multiplying by two.",
                repo_path=self.repo_path,
                setup_commands=(),
                test_commands=(
                    f'"{sys.executable}" -m unittest test_math_utils.TestMathUtils.test_double',
                ),
                command_timeout_s=10,
                metadata={
                    "patch_file": "math_utils.py",
                    "find": "return x + 2",
                    "replace": "return x * 2",
                },
            ),
        ]


class SweSmokeBenchmark:
    """Single-instance SWE-bench Lite smoke adapter.

    This intentionally does not implement the official SWE-bench harness.
    It only turns one configured instance into a real repo checkout and test run.
    """

    name = "swe-smoke"

    def __init__(self, instance_path: str | None = None):
        self.instance_path = Path(instance_path or "configs/swe_smoke_instance.json")
        self.task = self._load_task(self.instance_path)

    def train_tasks(self) -> list[BenchmarkTask]:
        return [self.task]

    def heldout_tasks(self) -> list[BenchmarkTask]:
        return []

    def _load_task(self, path: Path) -> BenchmarkTask:
        data = json.loads(path.read_text(encoding="utf-8"))
        instance_id = data["instance_id"]
        repo = data["repo"]
        fail_to_pass = data.get("fail_to_pass", [])
        if isinstance(fail_to_pass, str):
            fail_to_pass = [fail_to_pass]

        test_command_template = data.get(
            "test_command_template",
            "python -m pytest -q {test_target}",
        )
        test_commands = tuple(
            test_command_template.format(test_target=target)
            for target in fail_to_pass
        )
        setup_commands = tuple(data.get("setup_commands", []))
        test_patch = _text_block(data.get("test_patch", ""))

        metadata = {
            "source": "SWE-bench_Lite",
            "instance_config": str(path),
            "repo": repo,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": data.get("pass_to_pass", []),
            "version": data.get("version"),
        }
        metadata.update(data.get("metadata", {}))

        return BenchmarkTask(
            task_id=instance_id,
            split="smoke",
            benchmark=self.name,
            category="software",
            prompt=data.get("problem_statement", ""),
            expected_output=data.get("expected_output", "swe_smoke_patch_not_implemented"),
            issue_description=data.get("problem_statement", ""),
            repo_url=data.get("repo_url", f"https://github.com/{repo}.git"),
            base_commit=data["base_commit"],
            setup_commands=setup_commands,
            test_commands=test_commands,
            test_patch=test_patch,
            command_timeout_s=data.get("command_timeout_s", 120),
            metadata=metadata,
        )


class SweLiteSubsetBenchmark:
    """Fixed SWE-bench Lite subset adapter used for M1/M2 engineering work."""

    name = "swe-lite-subset"

    def __init__(self, subset_path: str | None = None):
        from self_evolving_agent.swe_loader import load_swe_lite_subset

        self.subset_path = Path(subset_path or "configs/swe_lite_subset_15.json")
        self.tasks = load_swe_lite_subset(self.subset_path)

    def train_tasks(self) -> list[BenchmarkTask]:
        return [task for task in self.tasks if task.split == "train"]

    def heldout_tasks(self) -> list[BenchmarkTask]:
        return [task for task in self.tasks if task.split == "heldout"]


def get_benchmark(
    name: str,
    *,
    swe_smoke_config: str | None = None,
    swe_subset_config: str | None = None,
) -> BenchmarkAdapter:
    if name == "mock":
        return MockBenchmark()
    if name == "local-toy":
        return LocalRepoBenchmark()
    if name == "swe-smoke":
        return SweSmokeBenchmark(swe_smoke_config)
    if name == "swe-lite-subset":
        return SweLiteSubsetBenchmark(swe_subset_config)
    raise ValueError(f"Unknown benchmark: {name}")


def _text_block(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(line) for line in value)
    return str(value)
