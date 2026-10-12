"""Isolated snapshot execution and repeated, paired regression evidence.

The adapter receives a request JSON path and a response JSON path as its final
two argv entries. Each invocation gets a fresh Git archive, including tests.
The adapter owns solver invocation and test-result interpretation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import io
import json
import math
import hashlib
import os
from pathlib import Path
import subprocess
import signal
import tarfile
import tempfile
import time
from typing import Protocol

from .contracts import FrozenAgent, SkillLibrary, TaskRecord, digest


def command_digest(command: list[str]) -> str:
    """Bind argv plus executable/script contents; dependencies belong in tools_digest."""
    return digest([{ "arg": arg, "file_sha256": hashlib.sha256(Path(arg).read_bytes()).hexdigest()
                    if Path(arg).is_file() else None} for arg in command])


def run_adapter(command: list[str], *, timeout: float, **kwargs) -> None:
    """Bound the entire adapter process group, including solver subprocesses."""
    with subprocess.Popen(command, start_new_session=True, **kwargs) as process:
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        if code:
            raise subprocess.CalledProcessError(code, command)


@dataclass(frozen=True)
class Outcome:
    resolved: bool | None
    executions: int = 1
    model_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""
    retrieved_skill_ids: tuple[str, ...] = ()
    trace_events: tuple[str, ...] = ()

    def __post_init__(self):
        if self.resolved is not None and type(self.resolved) is not bool:
            raise ValueError("resolved must be boolean or null")
        if any(type(v) is not int or v < 0 for v in (self.executions, self.model_calls, self.tokens)):
            raise ValueError("invalid execution/call/token accounting")
        if not math.isfinite(self.cost_usd) or self.cost_usd < 0:
            raise ValueError("invalid cost accounting")
        if self.error and self.resolved is not None:
            raise ValueError("infrastructure errors cannot be solved/unsolved outcomes")
        for name in ("retrieved_skill_ids", "trace_events"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{name} must be a sequence of strings")
            object.__setattr__(self, name, tuple(value))


class Evaluator(Protocol):
    def __call__(self, task: TaskRecord, library: SkillLibrary,
                 agent: FrozenAgent, seed: int) -> Outcome: ...


class CommandEvaluator:
    def __init__(self, command: list[str], artifact_root: Path, timeout: float = 1800):
        if not command or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("command and positive timeout required")
        self.command = command
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    def __call__(self, task, library, agent, seed):
        if command_digest(self.command) != agent.runner_digest:
            raise ValueError("runner changed or does not match frozen runner_digest")
        identity = {"task_hash": task.fingerprint, "library_hash": library.fingerprint,
                    "agent_hash": agent.fingerprint, "seed": seed}
        # Always execute independently. Repeated conditions are not silently cached.
        run_dir = Path(tempfile.mkdtemp(prefix=digest(identity)[:12] + "-", dir=self.artifact_root))
        request = {"schema_version": "retention.request.v1", **identity,
                   "task": asdict(task), "library": library.to_dict(), "agent": asdict(agent)}
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="retention-snapshot-") as temp:
                workspace = Path(temp) / "repo"
                workspace.mkdir()
                repository = str(Path(task.repository).resolve())
                commit = subprocess.run(["git", "-C", repository, "rev-parse",
                                         task.snapshot + "^{commit}"], check=True,
                                        capture_output=True, text=True, timeout=self.timeout).stdout.strip()
                if commit != task.snapshot:
                    raise ValueError("snapshot did not resolve to the declared commit")
                archive = subprocess.run(["git", "-C", repository, "archive", commit],
                                         check=True, capture_output=True, timeout=self.timeout).stdout
                with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                    # Reject links and special files instead of letting extraction
                    # escape the disposable workspace (also works on Python 3.9).
                    for member in bundle.getmembers():
                        target = (workspace / member.name).resolve()
                        if workspace not in target.parents or not (member.isfile() or member.isdir()):
                            raise ValueError("snapshot contains unsupported archive entry")
                    if hasattr(tarfile, "data_filter"):
                        bundle.extractall(workspace, filter="data")
                    else:
                        bundle.extractall(workspace)
                request["workspace"] = str(workspace)
                request_path, response_path = run_dir / "request.json", run_dir / "response.json"
                request_path.write_text(json.dumps(request, indent=2) + "\n")
                with (run_dir / "stdout.log").open("w") as stdout, (run_dir / "stderr.log").open("w") as stderr:
                    run_adapter([*self.command, str(request_path), str(response_path)],
                                cwd=workspace, stdout=stdout, stderr=stderr, timeout=self.timeout)
                response = json.loads(response_path.read_text())
                if response.get("identity") != identity:
                    raise ValueError("adapter response does not acknowledge exact frozen identity")
                if not {"resolved", "executions", "model_calls", "tokens", "cost_usd"} <= set(response["outcome"]):
                    raise ValueError("adapter response requires explicit outcome and complete accounting")
                outcome = Outcome(**response["outcome"])
                if outcome.executions < 1:
                    raise ValueError("adapter must account for at least one execution")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError) as exc:
            outcome = Outcome(None, error=f"{type(exc).__name__}: {exc}")
        (run_dir / "outcome.json").write_text(json.dumps({**identity, "outcome": asdict(outcome),
            "elapsed_seconds": time.monotonic() - started,
            "accounting_complete": outcome.resolved is not None}, indent=2) + "\n")
        return outcome


def evaluate_pairs(tasks: list[TaskRecord], old: SkillLibrary, new: SkillLibrary,
                   agent: FrozenAgent, evaluator: Evaluator, seeds: tuple[int, ...],
                   min_regression_trials: int = 2) -> list[dict]:
    if (len(set(seeds)) != len(seeds) or len(seeds) < 2
            or any(type(seed) is not int or seed < 0 for seed in seeds)):
        raise ValueError("at least two distinct independent trial seeds are required")
    if not 2 <= min_regression_trials <= len(seeds):
        raise ValueError("regression confirmation requires 2..len(seeds) trials")
    if len({task.task_id for task in tasks}) != len(tasks):
        raise ValueError("duplicate paired task ID")
    rows = []
    for task in tasks:
        trials = []
        for index, seed in enumerate(seeds):
            # Counterbalance temporal/API drift without changing paired settings.
            arms = [("old", old), ("new", new)]
            if index % 2:
                arms.reverse()
            outcomes = {arm: asdict(evaluator(task, library, agent, seed)) for arm, library in arms}
            trials.append({"seed": seed, **outcomes})
        valid = all(t[a]["resolved"] is not None for t in trials for a in ("old", "new"))
        losses = sum(t["old"]["resolved"] is True and t["new"]["resolved"] is False for t in trials)
        gains = sum(t["old"]["resolved"] is False and t["new"]["resolved"] is True for t in trials)
        old_rate = sum(t["old"]["resolved"] is True for t in trials) / len(trials) if valid else None
        new_rate = sum(t["new"]["resolved"] is True for t in trials) / len(trials) if valid else None
        rows.append({"task_id": task.task_id, "panel": task.panel,
                     "previously_solved": task.previously_solved,
                     "task_hash": task.fingerprint, "snapshot": task.snapshot,
                     "old_library_hash": old.fingerprint, "new_library_hash": new.fingerprint,
                     "agent_hash": agent.fingerprint, "trials": trials, "valid": valid,
                     "loss_trials": losses, "gain_trials": gains,
                     "old_solve_rate": old_rate, "new_solve_rate": new_rate,
                     "verified_regression": valid and losses >= min_regression_trials and gains == 0,
                     "suspected_regression": losses > 0,
                     "unstable": losses > 0 and gains > 0})
    return rows


def accounting(rows: list[dict]) -> dict:
    total = {"executions": 0, "model_calls": 0, "tokens": 0, "cost_usd": 0.0}
    for row in rows:
        for trial in row["trials"]:
            for arm in ("old", "new"):
                for key in total:
                    total[key] += trial[arm][key]
    total["accounting_complete"] = all(row["valid"] for row in rows)
    return total


def panel_metrics(rows: list[dict]) -> dict:
    valid = [r for r in rows if r["valid"]]
    solved = [r for r in valid if r["previously_solved"]]
    regressions = [r["task_id"] for r in valid if r["verified_regression"]]
    return {"tasks": len(rows), "valid_tasks": len(valid),
            "old_solve_rate": sum(r["old_solve_rate"] for r in valid) / len(valid) if valid else None,
            "new_solve_rate": sum(r["new_solve_rate"] for r in valid) / len(valid) if valid else None,
            "verified_regressions": regressions,
            "suspected_regressions": [r["task_id"] for r in rows if r["suspected_regression"]],
            "previously_solved_tasks": len(solved),
            "previously_solved_to_unsolved_fraction": (
                sum(r["verified_regression"] for r in solved) / len(solved) if solved else None),
            "invalid_tasks": [r["task_id"] for r in rows if not r["valid"]]}
