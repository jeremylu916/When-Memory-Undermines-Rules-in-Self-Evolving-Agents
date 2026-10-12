"""Official evaluator and exact-result cache for Verified-20 M5.6 runs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
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
    recover_aggregate_report_from_detail_reports,
)
from self_evolving_agent.python_runtime import (
    default_generation_python,
    default_harness_python,
)
from self_evolving_agent.scoring import EvaluationScore, score_records
from self_evolving_agent.subprocess_observability import (
    run_checked_subprocess,
    subprocess_phase_slug,
)
from self_evolving_agent.swe_verified_loader import (
    VERIFIED_BENCHMARK_IDENTITY,
    load_swe_verified_snapshot,
)


VERIFIED_EVALUATOR_SCHEMA_VERSION = "m5.6.swe_verified_evaluator.v1"
VERIFIED_CACHE_SCHEMA_VERSION = "m5.6.swe_verified_evaluator.cache.v1"
CommandRunner = Callable[..., Any]

_INLINE_CREDENTIAL_KEYS = {
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
}


class SWEVerifiedEvaluatorAdapter:
    """Generate and officially evaluate exact Verified subset configurations."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        run_root: str | Path,
        subset_config: str | Path = "configs/swe_verified_subset_20.json",
        official_harness_dataset: str | Path = (
            "benchmark_snapshots/"
            "swe_verified_subset_20_official_harness.json"
        ),
        generation_python: str | None = None,
        harness_python: str | None = None,
        aggregate_metric: str = "task_success_rate",
        max_workers: int = 4,
        harness_timeout: int = 1800,
        harness_retries: int = 2,
        generation_args: Iterable[str] = (),
        harness_args: Iterable[str] = (),
        strict_composition: bool = True,
        dry_run: bool = False,
        command_runner: CommandRunner | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.run_root = self._resolve_from_repo(run_root)
        self.subset_config = self._resolve_from_repo(subset_config)
        self.official_harness_dataset = self._resolve_from_repo(
            official_harness_dataset
        )
        self.generation_python = str(generation_python or default_generation_python())
        self.harness_python = str(harness_python or default_harness_python())
        self.aggregate_metric = str(aggregate_metric)
        self.max_workers = int(max_workers)
        self.harness_timeout = int(harness_timeout)
        self.harness_retries = max(0, int(harness_retries))
        self.generation_args = tuple(str(item) for item in generation_args)
        self.harness_args = tuple(str(item) for item in harness_args)
        self.strict_composition = bool(strict_composition)
        self.dry_run = bool(dry_run)
        self.command_runner = command_runner or subprocess.run
        self.snapshot = load_swe_verified_snapshot(
            self.subset_config,
            strict_composition=self.strict_composition,
        )

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
        del round_index
        _reject_inline_credentials(config)
        selected_tasks = sorted(tasks, key=lambda task: task.task_id)
        self._validate_tasks(selected_tasks)
        cache_key = self.cache_key(config=config, tasks=selected_tasks)
        if not self.dry_run:
            cached = self._load_cache(cache_key)
            if cached is not None:
                return cached

        evaluation_id = f"verified20-{cache_key[:16]}"
        evaluation_dir = self.run_root / "verified_evaluations" / evaluation_id
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        config_path = evaluation_dir / "agent_config.yaml"
        predictions_path = evaluation_dir / "predictions.jsonl"
        generation_run_id = "generation"
        generation_dir = evaluation_dir / generation_run_id
        harness_run_id = f"{evaluation_id}-official"
        model_identity = _model_identity(config)
        write_config(config, config_path)
        if not selected_tasks:
            empty_summary_path = evaluation_dir / "empty_evaluation_summary.json"
            empty_summary = {
                "schema_version": VERIFIED_EVALUATOR_SCHEMA_VERSION,
                "empty_task_set": True,
                "evaluation_id": evaluation_id,
                "benchmark_identity": VERIFIED_BENCHMARK_IDENTITY,
                "subset_fingerprint": self.snapshot["subset_fingerprint"],
                "model_identity": model_identity,
                "task_ids": [],
                "llm_requests_executed": 0,
                "official_harness_executed": False,
            }
            _write_json(empty_summary_path, empty_summary)
            return EvolutionEvaluationResult(
                score=_zero_score(0),
                records=[],
                diagnosis={},
                artifact_paths={
                    "empty_evaluation_summary": str(empty_summary_path),
                    "config": str(config_path),
                },
                cost=default_evaluation_cost(),
                evaluation_id=evaluation_id,
                cache_hit=False,
                task_ids=[],
                source="official_harness_verified_empty",
                metadata={
                    "empty_task_set": True,
                    "benchmark_identity": VERIFIED_BENCHMARK_IDENTITY,
                    "subset_fingerprint": self.snapshot["subset_fingerprint"],
                    "model_identity": model_identity,
                },
            )

        generation_command = self._generation_command(
            config_path=config_path,
            predictions_path=predictions_path,
            evaluation_dir=evaluation_dir,
            run_id=generation_run_id,
            tasks=selected_tasks,
            model_identity=model_identity,
        )
        harness_command = self._harness_command(
            predictions_path=predictions_path,
            run_id=harness_run_id,
            tasks=selected_tasks,
            report_dir=evaluation_dir,
        )
        if self.dry_run:
            plan_path = evaluation_dir / "implementation_dry_run_plan.json"
            plan = {
                "schema_version": VERIFIED_EVALUATOR_SCHEMA_VERSION,
                "implementation_dry_run": True,
                "evaluation_id": evaluation_id,
                "benchmark_identity": VERIFIED_BENCHMARK_IDENTITY,
                "subset_fingerprint": self.snapshot["subset_fingerprint"],
                "task_ids": [task.task_id for task in selected_tasks],
                "model_identity": model_identity,
                "generation_command": generation_command,
                "official_harness_command": harness_command,
                "llm_requests_executed": 0,
                "official_harness_executed": False,
                "cache_written": False,
            }
            _write_json(plan_path, plan)
            return EvolutionEvaluationResult(
                score=_zero_score(len(selected_tasks)),
                records=[],
                artifact_paths={"implementation_dry_run_plan": str(plan_path)},
                cost=default_evaluation_cost(),
                evaluation_id=evaluation_id,
                cache_hit=False,
                task_ids=[task.task_id for task in selected_tasks],
                source="verified_implementation_dry_run",
                metadata={
                    "implementation_dry_run": True,
                    "benchmark_identity": VERIFIED_BENCHMARK_IDENTITY,
                    "subset_fingerprint": self.snapshot["subset_fingerprint"],
                    "model_identity": model_identity,
                },
            )

        self._run_checked(
            generation_command,
            cwd=self.repo_root,
            environment=self._preserved_environment(),
            phase="Verified prediction generation",
            evaluation_dir=evaluation_dir,
        )
        if not predictions_path.exists():
            raise RuntimeError(
                "Verified prediction generation produced no predictions file; "
                f"see {evaluation_dir}"
            )
        prediction_records = load_prediction_records(predictions_path)
        predicted_ids = {
            str(record.get("instance_id"))
            for record in prediction_records
            if isinstance(record, dict)
        }
        expected_ids = {task.task_id for task in selected_tasks}
        if predicted_ids != expected_ids:
            raise RuntimeError(
                "Verified prediction generation did not cover the exact task set"
            )

        detail_root = evaluation_dir / "logs" / "run_evaluation" / harness_run_id
        try:
            self._run_checked(
                harness_command,
                cwd=evaluation_dir,
                environment=self._preserved_environment(),
                phase="Verified official harness",
                evaluation_dir=evaluation_dir,
                retries=self.harness_retries,
            )
        except RuntimeError as harness_error:
            try:
                aggregate_path = self._recover_aggregate_report(
                    evaluation_dir=evaluation_dir,
                    predictions_path=predictions_path,
                    detail_root=detail_root,
                    harness_run_id=harness_run_id,
                    tasks=selected_tasks,
                    reason=str(harness_error),
                )
            except RuntimeError as recovery_error:
                raise RuntimeError(
                    f"{harness_error}; strict report recovery failed: "
                    f"{recovery_error}"
                ) from recovery_error
        else:
            try:
                aggregate_path = self._find_aggregate_report(
                    evaluation_dir,
                    harness_run_id,
                )
            except RuntimeError as aggregate_error:
                aggregate_path = self._recover_aggregate_report(
                    evaluation_dir=evaluation_dir,
                    predictions_path=predictions_path,
                    detail_root=detail_root,
                    harness_run_id=harness_run_id,
                    tasks=selected_tasks,
                    reason=str(aggregate_error),
                )
        result = self._materialize_result(
            tasks=selected_tasks,
            evaluation_id=evaluation_id,
            evaluation_dir=evaluation_dir,
            predictions_path=predictions_path,
            aggregate_report_path=aggregate_path,
            detail_report_root=detail_root,
            generation_run_dir=generation_dir,
            model_identity=model_identity,
            cache_hit=False,
        )
        self._write_cache(cache_key, config, result)
        return result

    def cache_key(
        self,
        *,
        config: dict[str, Any],
        tasks: list[BenchmarkTask],
    ) -> str:
        _reject_inline_credentials(config)
        ordered_task_ids = sorted(task.task_id for task in tasks)
        llm = config.get("llm", {}) or {}
        workflow = config.get("workflow", {}) or {}
        payload = {
            "evaluator_schema_version": VERIFIED_EVALUATOR_SCHEMA_VERSION,
            "benchmark_identity": VERIFIED_BENCHMARK_IDENTITY,
            "subset_fingerprint": self.snapshot["subset_fingerprint"],
            "ordered_task_ids": ordered_task_ids,
            "agent_config_fingerprint": config_fingerprint(config),
            "model_identity": _model_identity(config),
            "generation_parameters": {
                "generation_python": self.generation_python,
                "generation_args": self.generation_args,
                "patch_materializer": workflow.get("patch_materializer"),
                "max_completion_tokens": llm.get("max_completion_tokens"),
                "length_retries": llm.get("length_retries"),
            },
            "validation_parameters": {
                "enabled": workflow.get("validation_enabled", False),
                "retries": workflow.get("validation_retries", 0),
                "timeout_s": workflow.get("validation_timeout_s"),
                "max_commands": workflow.get("validation_max_commands", 0),
                "log_chars": workflow.get("validation_log_chars"),
            },
            "official_harness_settings": {
                "dataset_sha256": _file_sha256(
                    self.official_harness_dataset
                ),
                "max_workers": self.max_workers,
                "timeout": self.harness_timeout,
                "retries": self.harness_retries,
                "aggregate_metric": self.aggregate_metric,
                "harness_args": self.harness_args,
            },
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _generation_command(
        self,
        *,
        config_path: Path,
        predictions_path: Path,
        evaluation_dir: Path,
        run_id: str,
        tasks: list[BenchmarkTask],
        model_identity: dict[str, Any],
    ) -> list[str]:
        command = [
            self.generation_python,
            str(
                self.repo_root
                / "scripts"
                / "generate_swe_verified_predictions.py"
            ),
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
            str(model_identity["prediction_model_name"]),
        ]
        for task in tasks:
            command.extend(["--task-id", task.task_id])
        if not self.strict_composition:
            command.append("--flex-manifest")
        command.extend(self.generation_args)
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
            str(self.official_harness_dataset),
            "--split",
            "test",
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
        tasks: list[BenchmarkTask],
        evaluation_id: str,
        evaluation_dir: Path,
        predictions_path: Path,
        aggregate_report_path: Path,
        detail_report_root: Path,
        generation_run_dir: Path,
        model_identity: dict[str, Any],
        cache_hit: bool,
    ) -> EvolutionEvaluationResult:
        aggregate = load_aggregate_report(aggregate_report_path)
        details = load_detail_reports(detail_report_root)
        predictions = load_prediction_records(predictions_path)
        traces = _load_trace_records(generation_run_dir, tasks)
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
        returned_raw_models = sorted(
            {
                str(call.get("raw_model"))
                for record in traces
                for call in record.get("llm_calls", []) or []
                if isinstance(call, dict) and call.get("raw_model")
            }
        )
        complete_model_identity = {
            **model_identity,
            "returned_raw_models": returned_raw_models,
        }

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
            source="official_harness_verified",
            metadata={
                "evaluation_id": evaluation_id,
                "cache_hit": cache_hit,
                "task_ids": [task.task_id for task in tasks],
                "evaluator_schema_version": VERIFIED_EVALUATOR_SCHEMA_VERSION,
                "subset_fingerprint": self.snapshot["subset_fingerprint"],
            },
        )
        artifact_paths = {
            "predictions": str(predictions_path),
            "official_aggregate_report": str(aggregate_report_path),
            "official_detail_reports": str(detail_report_root),
            "generation_run": str(generation_run_dir),
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
            source="official_harness_verified",
            metadata={
                "implementation_dry_run": False,
                "benchmark_identity": VERIFIED_BENCHMARK_IDENTITY,
                "subset_fingerprint": self.snapshot["subset_fingerprint"],
                "model_identity": complete_model_identity,
            },
        )

    def _cache_path(self, cache_key: str) -> Path:
        return (
            self.run_root
            / "verified_evaluation_cache"
            / f"{cache_key}.json"
        )

    def _write_cache(
        self,
        cache_key: str,
        config: dict[str, Any],
        result: EvolutionEvaluationResult,
    ) -> None:
        manifest = {
            "schema_version": VERIFIED_CACHE_SCHEMA_VERSION,
            "cache_key": cache_key,
            "evaluation_id": result.evaluation_id,
            "benchmark_identity": VERIFIED_BENCHMARK_IDENTITY,
            "subset_fingerprint": self.snapshot["subset_fingerprint"],
            "agent_config_fingerprint": config_fingerprint(config),
            "task_ids": result.task_ids,
            "cache_hit": False,
            "score": asdict(result.score),
            "failure_taxonomy": result.diagnosis.get("failure_taxonomy", {}),
            "model_identity": result.metadata.get("model_identity", {}),
            "reasoning_effort": result.metadata.get("model_identity", {}).get(
                "reasoning_effort"
            ),
            "cost": normalize_evaluation_cost(result.cost),
            "artifact_paths": result.artifact_paths,
        }
        _write_json(self._cache_path(cache_key), manifest)

    def _load_cache(self, cache_key: str) -> EvolutionEvaluationResult | None:
        path = self._cache_path(cache_key)
        if not path.exists():
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != VERIFIED_CACHE_SCHEMA_VERSION
            or manifest.get("cache_key") != cache_key
            or manifest.get("benchmark_identity") != VERIFIED_BENCHMARK_IDENTITY
            or manifest.get("subset_fingerprint")
            != self.snapshot["subset_fingerprint"]
        ):
            return None
        artifacts = manifest.get("artifact_paths")
        if not isinstance(artifacts, dict):
            return None
        required_files = (
            "predictions",
            "official_aggregate_report",
            "official_critic_records",
            "official_critic_diagnosis",
            "official_score",
        )
        if any(
            not Path(str(artifacts.get(key) or "")).is_file()
            for key in required_files
        ):
            return None
        detail_root = Path(
            str(artifacts.get("official_detail_reports") or "")
        )
        generation_root = Path(str(artifacts.get("generation_run") or ""))
        if not detail_root.is_dir() or not generation_root.is_dir():
            return None
        try:
            records = json.loads(
                Path(str(artifacts["official_critic_records"])).read_text(
                    encoding="utf-8"
                )
            )
            diagnosis = json.loads(
                Path(str(artifacts["official_critic_diagnosis"])).read_text(
                    encoding="utf-8"
                )
            )
            score = EvaluationScore(**manifest["score"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return None
        return EvolutionEvaluationResult(
            score=score,
            records=records if isinstance(records, list) else [],
            diagnosis=diagnosis if isinstance(diagnosis, dict) else {},
            artifact_paths={
                str(key): str(value) for key, value in artifacts.items()
            },
            cost=normalize_evaluation_cost(manifest.get("cost", {})),
            evaluation_id=str(manifest.get("evaluation_id") or ""),
            cache_hit=True,
            task_ids=[str(item) for item in manifest.get("task_ids", [])],
            source="official_harness_verified",
            metadata={
                "implementation_dry_run": False,
                "benchmark_identity": VERIFIED_BENCHMARK_IDENTITY,
                "subset_fingerprint": self.snapshot["subset_fingerprint"],
                "model_identity": manifest.get("model_identity", {}),
            },
        )

    def _validate_tasks(self, tasks: list[BenchmarkTask]) -> None:
        expected = {
            str(item["instance_id"]) for item in self.snapshot["tasks"]
        }
        for task in tasks:
            if task.benchmark != "swe-verified-subset":
                raise ValueError(
                    f"Non-Verified task supplied to Verified evaluator: {task.task_id}"
                )
            if task.task_id not in expected:
                raise ValueError(
                    f"Task not present in fixed Verified subset: {task.task_id}"
                )
            metadata = task.metadata or {}
            if metadata.get("subset_fingerprint") != self.snapshot.get(
                "subset_fingerprint"
            ):
                raise ValueError(
                    f"Verified subset fingerprint mismatch for {task.task_id}"
                )
            if task.test_patch or task.test_commands:
                raise ValueError(
                    f"Leakage-sensitive test evidence present for {task.task_id}"
                )

    def _run_checked(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        phase: str,
        evaluation_dir: Path,
        retries: int = 0,
    ) -> None:
        legacy_log_name = f"subprocess_{subprocess_phase_slug(phase)}.json"
        unified_log_name = f"subprocess_{_subprocess_phase_key(phase)}.json"
        additional_log_names = []
        if unified_log_name != legacy_log_name:
            additional_log_names.append(unified_log_name)
        run_checked_subprocess(
            command,
            cwd=cwd,
            environment=environment,
            phase=phase,
            phase_key=_subprocess_phase_key(phase),
            evaluation_dir=evaluation_dir,
            command_runner=self.command_runner,
            retries=retries,
            log_name=legacy_log_name,
            additional_log_names=additional_log_names,
            stream_output=True,
            announce_attempts=True,
        )

    def _find_aggregate_report(
        self,
        evaluation_dir: Path,
        run_id: str,
    ) -> Path:
        matches = sorted(evaluation_dir.glob(f"*.{run_id}.json"))
        if len(matches) != 1:
            raise RuntimeError(
                "Verified official harness aggregate report missing or ambiguous"
            )
        return matches[0]

    def _recover_aggregate_report(
        self,
        *,
        evaluation_dir: Path,
        predictions_path: Path,
        detail_root: Path,
        harness_run_id: str,
        tasks: list[BenchmarkTask],
        reason: str,
    ) -> Path:
        return recover_aggregate_report_from_detail_reports(
            predictions_path=predictions_path,
            detail_report_root=detail_root,
            task_ids=[task.task_id for task in tasks],
            output_path=(
                evaluation_dir / f"recovered.{harness_run_id}.json"
            ),
            recovery_reason=reason,
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
    if "prediction generation" in normalized:
        return "prediction_generation"
    if "official harness" in normalized:
        return "official_harness"
    return subprocess_phase_slug(phase)


def _existing_subprocess_artifact_paths(evaluation_dir: Path) -> dict[str, str]:
    paths = {
        "subprocess_prediction_generation": (
            evaluation_dir / "subprocess_prediction_generation.json"
        ),
        "subprocess_official_harness": (
            evaluation_dir / "subprocess_official_harness.json"
        ),
        "subprocess_verified_prediction_generation": (
            evaluation_dir / "subprocess_verified_prediction_generation.json"
        ),
        "subprocess_verified_official_harness": (
            evaluation_dir / "subprocess_verified_official_harness.json"
        ),
    }
    return {key: str(path) for key, path in paths.items() if path.exists()}


def _model_identity(config: dict[str, Any]) -> dict[str, Any]:
    llm = config.get("llm", {}) or {}
    requested = str(
        llm.get("requested_model") or llm.get("model_family") or llm.get("model") or ""
    )
    deployment = str(
        llm.get("deployment") or llm.get("deployment_id") or llm.get("model") or ""
    )
    reasoning_effort = llm.get("reasoning_effort")
    return {
        "provider": str(llm.get("provider") or ""),
        "requested_model": requested,
        "configured_model": str(llm.get("model") or ""),
        "deployment": deployment,
        "reasoning_effort": reasoning_effort,
        "prediction_model_name": (
            f"{deployment or requested}-reasoning-"
            f"{reasoning_effort or 'default'}"
        ),
        "request_budget": {
            "max_completion_tokens": llm.get("max_completion_tokens"),
            "timeout_s": llm.get("timeout_s"),
            "timeout_retries": llm.get("timeout_retries"),
        },
    }


def _load_trace_records(
    generation_run_dir: Path,
    tasks: list[BenchmarkTask],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for task in tasks:
        path = (
            generation_run_dir
            / "tasks"
            / _safe_name(task.task_id)
            / "traces"
            / "single_task.jsonl"
        )
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                payload = json.loads(line)
                if isinstance(payload, dict):
                    records.append(payload)
    return records


def _records_llm_cost(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = default_evaluation_cost()
    for record in records:
        for call in record.get("llm_calls", []) or []:
            if not isinstance(call, dict):
                continue
            total["prompt_tokens"] += _int_or_zero(call.get("prompt_tokens"))
            total["completion_tokens"] += _int_or_zero(
                call.get("completion_tokens")
            )
            total["total_tokens"] += _int_or_zero(call.get("total_tokens"))
            total["cost_usd"] = round(
                total["cost_usd"] + _float_or_zero(call.get("cost_usd")),
                10,
            )
    if total["total_tokens"] == 0:
        total["total_tokens"] = (
            total["prompt_tokens"] + total["completion_tokens"]
        )
    return total


def _reject_inline_credentials(value: Any, path: str = "config") -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if (
            str(key).lower() in _INLINE_CREDENTIAL_KEYS
            and item not in {None, ""}
        ):
            raise ValueError(
                f"Inline credentials are forbidden in Verified configuration at {path}"
            )
        if isinstance(item, dict):
            _reject_inline_credentials(item, f"{path}.{key}")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _zero_score(task_count: int) -> EvaluationScore:
    return EvaluationScore(
        task_success_rate=0.0,
        patch_correctness=0.0,
        security_validity=0.0,
        test_pass_rate=0.0,
        cost_adjusted_performance=0.0,
        aggregate_score=0.0,
        task_count=task_count,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _safe_name(value: str) -> str:
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
