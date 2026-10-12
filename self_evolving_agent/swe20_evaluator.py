"""SWE-20 adapter for official, cacheable M5.6 evaluations.

The adapter invokes the existing image prediction generator and official
SWE-bench harness without changing either implementation. It then fuses
generation traces, predictions, aggregate results, and per-instance reports
into the critic records consumed by the evolution loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from self_evolving_agent.benchmarks import BenchmarkTask
from self_evolving_agent.config import config_fingerprint, write_config
from self_evolving_agent.critic import CRITIC_MODE_RULE, diagnose_failures
from self_evolving_agent.evolution_evaluation import (
    EvolutionEvaluationResult,
    default_evaluation_cost,
    normalize_evaluation_cost,
)
from self_evolving_agent.failure_cause_artifacts import (
    ROOT_CAUSE_EVENTS_ARTIFACT_NAME,
    write_root_cause_events_artifact,
)
from self_evolving_agent.harness_report import (
    build_critic_records_from_official_evidence,
    load_aggregate_report,
    load_detail_reports,
    load_prediction_records,
)
from self_evolving_agent.scoring import EvaluationScore, score_records
from self_evolving_agent.progress import ProgressReporter
from self_evolving_agent.subprocess_observability import run_checked_subprocess


SWE20_EVALUATOR_SCHEMA_VERSION = "m5.6.swe20_evaluator.v1"
SWE20_CACHE_SCHEMA_VERSION = "m5.6.swe20_evaluator.cache.v1"

CommandRunner = Callable[..., Any]


class SWE20EvaluatorAdapter:
    """Run or reuse official SWE-bench evaluations for a fixed task set."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        run_root: str | Path,
        subset_config: str | Path = "configs/swe_lite_subset_20.json",
        generation_python: str = "python",
        harness_python: str = "python",
        prediction_model_name: str = "llm-agent",
        dataset_name: str = "SWE-bench/SWE-bench_Lite",
        dataset_split: str = "test",
        aggregate_metric: str = "task_success_rate",
        max_workers: int = 4,
        harness_timeout: int = 1800,
        generation_args: Iterable[str] = (),
        harness_args: Iterable[str] = (),
        command_runner: CommandRunner | None = None,
        progress_reporter: ProgressReporter | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.run_root = self._resolve_from_repo(run_root)
        self.subset_config = self._resolve_from_repo(subset_config)
        self.generation_python = str(generation_python)
        self.harness_python = str(harness_python)
        self.prediction_model_name = str(prediction_model_name)
        self.dataset_name = str(dataset_name)
        self.dataset_split = str(dataset_split)
        self.aggregate_metric = str(aggregate_metric)
        self.max_workers = int(max_workers)
        self.harness_timeout = int(harness_timeout)
        self.generation_args = tuple(str(item) for item in generation_args)
        self.harness_args = tuple(str(item) for item in harness_args)
        self.command_runner = command_runner or subprocess.run
        self.progress_reporter = progress_reporter

    def __call__(
        self,
        config: dict[str, Any],
        tasks: list[BenchmarkTask],
        round_index: int,
    ) -> EvolutionEvaluationResult:
        return self.evaluate(config=config, tasks=tasks, round_index=round_index)

    def evaluate(
        self,
        *,
        config: dict[str, Any],
        tasks: list[BenchmarkTask],
        round_index: int = 0,
    ) -> EvolutionEvaluationResult:
        """Evaluate ``config`` on ``tasks``, reusing an exact cache hit."""

        del round_index
        selected_tasks = _sorted_tasks(tasks)
        cache_key = self._cache_key(config, selected_tasks)
        cached = self._load_cache(cache_key)
        if cached is not None:
            if self.progress_reporter is not None:
                self.progress_reporter.emit(
                    "evaluation_cache_hit",
                    status="completed",
                    split=_task_split(selected_tasks),
                    tasks=len(selected_tasks),
                    evaluation_id=cached.evaluation_id,
                    score=cached.score.aggregate_score,
                )
            return cached

        evaluation_id = f"swe20-{cache_key[:16]}"
        evaluation_dir = self.run_root / "evaluations" / evaluation_id
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        config_path = evaluation_dir / "agent_config.yaml"
        predictions_path = evaluation_dir / "predictions.jsonl"
        generation_run_id = "generation"
        generation_dir = evaluation_dir / generation_run_id
        harness_run_id = f"{evaluation_id}-official"
        write_config(config, config_path)

        generation_command = self._generation_command(
            config_path=config_path,
            predictions_path=predictions_path,
            evaluation_dir=evaluation_dir,
            run_id=generation_run_id,
            tasks=selected_tasks,
        )
        generation_started_at = time.monotonic()
        if self.progress_reporter is not None:
            self.progress_reporter.emit(
                "prediction_generation_started",
                status="started",
                split=_task_split(selected_tasks),
                tasks=len(selected_tasks),
                evaluation_id=evaluation_id,
            )
        try:
            self._run_checked(
                generation_command,
                cwd=self.repo_root,
                environment=self._preserved_environment(),
                phase="prediction generation",
                evaluation_dir=evaluation_dir,
            )
        except Exception as exc:
            if self.progress_reporter is not None:
                self.progress_reporter.emit(
                    "prediction_generation_failed",
                    status="failed",
                    split=_task_split(selected_tasks),
                    evaluation_id=evaluation_id,
                    duration_s=round(time.monotonic() - generation_started_at, 3),
                    error_type=type(exc).__name__,
                )
            raise
        if self.progress_reporter is not None:
            self.progress_reporter.emit(
                "prediction_generation_completed",
                status="completed",
                split=_task_split(selected_tasks),
                tasks=len(selected_tasks),
                evaluation_id=evaluation_id,
                duration_s=round(time.monotonic() - generation_started_at, 3),
            )
        if not predictions_path.exists():
            raise RuntimeError(
                f"Prediction generation produced no predictions file; see {evaluation_dir}"
            )

        harness_command = self._harness_command(
            predictions_path=predictions_path,
            run_id=harness_run_id,
            tasks=selected_tasks,
            report_dir=evaluation_dir,
        )
        harness_started_at = time.monotonic()
        if self.progress_reporter is not None:
            self.progress_reporter.emit(
                "official_harness_started",
                status="started",
                split=_task_split(selected_tasks),
                tasks=len(selected_tasks),
                workers=self.max_workers,
                evaluation_id=evaluation_id,
            )
        try:
            self._run_checked(
                harness_command,
                cwd=evaluation_dir,
                environment=self._preserved_environment(),
                phase="official harness",
                evaluation_dir=evaluation_dir,
            )
        except Exception as exc:
            if self.progress_reporter is not None:
                self.progress_reporter.emit(
                    "official_harness_failed",
                    status="failed",
                    split=_task_split(selected_tasks),
                    evaluation_id=evaluation_id,
                    duration_s=round(time.monotonic() - harness_started_at, 3),
                    error_type=type(exc).__name__,
                )
            raise
        if self.progress_reporter is not None:
            self.progress_reporter.emit(
                "official_harness_completed",
                status="completed",
                split=_task_split(selected_tasks),
                tasks=len(selected_tasks),
                evaluation_id=evaluation_id,
                duration_s=round(time.monotonic() - harness_started_at, 3),
            )

        aggregate_path = self._find_aggregate_report(evaluation_dir, harness_run_id)
        detail_root = evaluation_dir / "logs" / "run_evaluation" / harness_run_id
        result = self._materialize_result(
            config=config,
            tasks=selected_tasks,
            evaluation_id=evaluation_id,
            evaluation_dir=evaluation_dir,
            predictions_path=predictions_path,
            aggregate_report_path=aggregate_path,
            detail_report_root=detail_root,
            generation_run_dir=generation_dir,
            cache_hit=False,
        )
        self._write_cache(cache_key, config, result)
        return result

    def seed_official_cache(
        self,
        *,
        config: dict[str, Any],
        tasks: list[BenchmarkTask],
        predictions_path: str | Path,
        aggregate_report_path: str | Path,
        detail_report_root: str | Path,
        generation_run_dir: str | Path | None = None,
    ) -> EvolutionEvaluationResult:
        """Register an existing official run without modifying its JSON files."""

        selected_tasks = _sorted_tasks(tasks)
        cache_key = self._cache_key(config, selected_tasks)
        evaluation_id = f"swe20-{cache_key[:16]}"
        evaluation_dir = self.run_root / "evaluations" / evaluation_id
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        result = self._materialize_result(
            config=config,
            tasks=selected_tasks,
            evaluation_id=evaluation_id,
            evaluation_dir=evaluation_dir,
            predictions_path=self._resolve_from_repo(predictions_path),
            aggregate_report_path=self._resolve_from_repo(aggregate_report_path),
            detail_report_root=self._resolve_from_repo(detail_report_root),
            generation_run_dir=(
                self._resolve_from_repo(generation_run_dir)
                if generation_run_dir is not None
                else None
            ),
            cache_hit=False,
        )
        self._write_cache(cache_key, config, result)
        return result

    def _generation_command(
        self,
        *,
        config_path: Path,
        predictions_path: Path,
        evaluation_dir: Path,
        run_id: str,
        tasks: list[BenchmarkTask],
    ) -> list[str]:
        command = [
            self.generation_python,
            str(self.repo_root / "scripts" / "generate_predictions_from_images.py"),
            "--config",
            str(config_path),
            "--subset-config",
            str(self.subset_config),
            "--output-dir",
            str(evaluation_dir),
            "--run-id",
            run_id,
            "--export-predictions",
            str(predictions_path),
            "--prediction-model-name",
            self.prediction_model_name,
            "--continue-on-failure",
            "--empty-patch-on-failure",
        ]
        for task in tasks:
            command.extend(["--task-id", task.task_id])
        command.extend(self.generation_args)
        if self.progress_reporter is not None:
            command.append(
                "--progress" if self.progress_reporter.enabled else "--no-progress"
            )
            if self.progress_reporter.log_path is not None:
                command.extend(
                    ["--progress-log", str(self.progress_reporter.log_path)]
                )
            if self.progress_reporter.jsonl_path is not None:
                command.extend(
                    ["--progress-jsonl", str(self.progress_reporter.jsonl_path)]
                )
        return command

    def _harness_command(
        self,
        *,
        predictions_path: Path,
        run_id: str,
        tasks: list[BenchmarkTask],
        report_dir: Path,
    ) -> list[str]:
        command = [
            self.harness_python,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            self.dataset_name,
            "--split",
            self.dataset_split,
            "--predictions_path",
            str(predictions_path),
            "--max_workers",
            str(self.max_workers),
            "--timeout",
            str(self.harness_timeout),
            "--run_id",
            run_id,
            "--report_dir",
            str(report_dir),
            "--instance_ids",
        ]
        command.extend(task.task_id for task in tasks)
        command.extend(self.harness_args)
        return command

    def _materialize_result(
        self,
        *,
        config: dict[str, Any],
        tasks: list[BenchmarkTask],
        evaluation_id: str,
        evaluation_dir: Path,
        predictions_path: Path,
        aggregate_report_path: Path,
        detail_report_root: Path,
        generation_run_dir: Path | None,
        cache_hit: bool,
    ) -> EvolutionEvaluationResult:
        aggregate = load_aggregate_report(aggregate_report_path)
        details = load_detail_reports(detail_report_root)
        predictions = load_prediction_records(predictions_path)
        traces = _load_original_records(generation_run_dir, tasks)
        records = build_critic_records_from_official_evidence(
            tasks=tasks,
            aggregate_report=aggregate,
            detail_reports=details,
            original_trace_records=traces,
            prediction_records=predictions,
        )
        score = score_records(records, aggregate_metric=self.aggregate_metric)
        diagnosis = diagnose_failures(records, critic_mode=CRITIC_MODE_RULE)
        cost = _records_llm_cost(traces)

        records_path = evaluation_dir / "official_critic_records.json"
        diagnosis_path = evaluation_dir / "official_critic_diagnosis.json"
        score_path = evaluation_dir / "official_score.json"
        root_cause_events_path = evaluation_dir / ROOT_CAUSE_EVENTS_ARTIFACT_NAME
        _write_json(records_path, records)
        _write_json(diagnosis_path, diagnosis)
        _write_json(score_path, asdict(score))
        write_root_cause_events_artifact(
            root_cause_events_path,
            records,
            source="official_harness_swe20",
            metadata={
                "evaluation_id": evaluation_id,
                "cache_hit": cache_hit,
                "task_ids": [task.task_id for task in tasks],
                "evaluator_schema_version": SWE20_EVALUATOR_SCHEMA_VERSION,
            },
        )
        artifact_paths = {
            "predictions": str(predictions_path),
            "official_aggregate_report": str(aggregate_report_path),
            "official_detail_reports": str(detail_report_root),
            "generation_run": str(generation_run_dir or ""),
            "official_critic_records": str(records_path),
            "official_critic_diagnosis": str(diagnosis_path),
            "official_score": str(score_path),
            "root_cause_events": str(root_cause_events_path),
        }
        artifact_paths.update(_existing_subprocess_artifact_paths(evaluation_dir))
        return EvolutionEvaluationResult(
            score=score,
            records=records,
            diagnosis=diagnosis,
            artifact_paths=artifact_paths,
            cost=cost,
            evaluation_id=evaluation_id,
            cache_hit=cache_hit,
            task_ids=[task.task_id for task in tasks],
            source="official_harness",
        )

    def _cache_key(
        self,
        config: dict[str, Any],
        tasks: list[BenchmarkTask],
    ) -> str:
        payload = {
            "schema_version": SWE20_EVALUATOR_SCHEMA_VERSION,
            "config": config,
            "task_ids": sorted(task.task_id for task in tasks),
            "subset_config": str(self.subset_config),
            "prediction_model_name": self.prediction_model_name,
            "dataset_name": self.dataset_name,
            "dataset_split": self.dataset_split,
            "aggregate_metric": self.aggregate_metric,
            "max_workers": self.max_workers,
            "harness_timeout": self.harness_timeout,
            "generation_args": self.generation_args,
            "harness_args": self.harness_args,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _cache_path(self, cache_key: str) -> Path:
        return self.run_root / "evaluation_cache" / f"{cache_key}.json"

    def _write_cache(
        self,
        cache_key: str,
        config: dict[str, Any],
        result: EvolutionEvaluationResult,
    ) -> None:
        cache_path = self._cache_path(cache_key)
        manifest = {
            "schema_version": SWE20_CACHE_SCHEMA_VERSION,
            "cache_key": cache_key,
            "evaluation_id": result.evaluation_id,
            "config_hash": config_fingerprint(config),
            "task_ids": result.task_ids,
            "source": result.source,
            "score": asdict(result.score),
            "cost": normalize_evaluation_cost(result.cost),
            "failure_taxonomy": result.diagnosis.get("failure_taxonomy", {}),
            "artifact_paths": result.artifact_paths,
        }
        _write_json(cache_path, manifest)

    def _load_cache(self, cache_key: str) -> EvolutionEvaluationResult | None:
        cache_path = self._cache_path(cache_key)
        if not cache_path.exists():
            return None
        manifest = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != SWE20_CACHE_SCHEMA_VERSION
            or manifest.get("cache_key") != cache_key
        ):
            return None
        artifact_paths = manifest.get("artifact_paths", {})
        if not isinstance(artifact_paths, dict):
            return None
        records_path = Path(str(artifact_paths.get("official_critic_records", "")))
        diagnosis_path = Path(str(artifact_paths.get("official_critic_diagnosis", "")))
        if not records_path.exists() or not diagnosis_path.exists():
            return None
        score_payload = manifest.get("score", {})
        if not isinstance(score_payload, dict):
            return None
        records = json.loads(records_path.read_text(encoding="utf-8"))
        diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
        return EvolutionEvaluationResult(
            score=EvaluationScore(**score_payload),
            records=records if isinstance(records, list) else [],
            diagnosis=diagnosis if isinstance(diagnosis, dict) else {},
            artifact_paths={str(key): str(value) for key, value in artifact_paths.items()},
            cost=normalize_evaluation_cost(manifest.get("cost", {})),
            evaluation_id=str(manifest.get("evaluation_id") or ""),
            cache_hit=True,
            task_ids=[str(item) for item in manifest.get("task_ids", [])],
            source=str(manifest.get("source") or "official_harness"),
        )

    def _find_aggregate_report(self, evaluation_dir: Path, run_id: str) -> Path:
        matches = sorted(evaluation_dir.glob(f"*.{run_id}.json"))
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise RuntimeError(
                f"Official harness produced no aggregate report; see {evaluation_dir}"
            )
        raise RuntimeError(
            f"Official harness produced ambiguous aggregate reports; see {evaluation_dir}"
        )

    def _run_checked(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        phase: str,
        evaluation_dir: Path,
    ) -> None:
        run_checked_subprocess(
            command,
            cwd=cwd,
            environment=environment,
            phase=phase,
            phase_key=_subprocess_phase_key(phase),
            evaluation_dir=evaluation_dir,
            command_runner=self.command_runner,
            stream_output=bool(
                self.progress_reporter is not None
                and self.progress_reporter.enabled
            ),
            announce_attempts=bool(
                self.progress_reporter is not None
                and self.progress_reporter.enabled
            ),
        )

    def _preserved_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HF_HUB_OFFLINE"] = "1"
        environment["HF_DATASETS_OFFLINE"] = "1"
        return environment

    def _resolve_from_repo(self, path: str | Path) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.repo_root / candidate


def _subprocess_phase_key(phase: str) -> str:
    normalized = " ".join(str(phase).lower().split())
    if normalized == "prediction generation":
        return "prediction_generation"
    if normalized == "official harness":
        return "official_harness"
    return normalized.replace(" ", "_")


def _existing_subprocess_artifact_paths(evaluation_dir: Path) -> dict[str, str]:
    paths = {
        "subprocess_prediction_generation": (
            evaluation_dir / "subprocess_prediction_generation.json"
        ),
        "subprocess_official_harness": (
            evaluation_dir / "subprocess_official_harness.json"
        ),
    }
    return {key: str(path) for key, path in paths.items() if path.exists()}


def _sorted_tasks(tasks: list[BenchmarkTask]) -> list[BenchmarkTask]:
    return sorted(tasks, key=lambda task: task.task_id)


def _task_split(tasks: list[BenchmarkTask]) -> str:
    splits = sorted({str(task.split) for task in tasks})
    return ",".join(splits) if splits else "unknown"


def _load_original_records(
    generation_run_dir: Path | None,
    tasks: list[BenchmarkTask],
) -> list[dict[str, Any]]:
    if generation_run_dir is None:
        return []
    records: list[dict[str, Any]] = []
    for task in tasks:
        task_dir = generation_run_dir / "tasks" / _safe_path_name(task.task_id)
        trace_path = task_dir / "traces" / "single_task.jsonl"
        if trace_path.exists():
            records.extend(_read_jsonl(trace_path))
            continue
        failure_path = task_dir / "generation_failed.json"
        if not failure_path.exists():
            continue
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        if not isinstance(failure, dict):
            continue
        failure.setdefault(
            "task",
            {
                "id": task.task_id,
                "category": task.category,
                "benchmark": task.benchmark,
            },
        )
        failure.setdefault("success", False)
        failure.setdefault("execution_status", "patch_failed")
        failure.setdefault("metrics", {"test_pass": 0.0})
        failure.setdefault("cost", 0.0)
        records.append(failure)
    return records


def _records_llm_cost(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = default_evaluation_cost()
    for record in records:
        for call in record.get("llm_calls", []) or []:
            if not isinstance(call, dict):
                continue
            total["prompt_tokens"] += _int_or_zero(call.get("prompt_tokens"))
            total["completion_tokens"] += _int_or_zero(call.get("completion_tokens"))
            total["total_tokens"] += _int_or_zero(call.get("total_tokens"))
            total["cost_usd"] = round(
                total["cost_usd"] + _float_or_zero(call.get("cost_usd")),
                10,
            )
    if total["total_tokens"] == 0:
        total["total_tokens"] = total["prompt_tokens"] + total["completion_tokens"]
    return total


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True),
        encoding="utf-8",
    )


def _safe_path_name(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_").replace(":", "_")


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
