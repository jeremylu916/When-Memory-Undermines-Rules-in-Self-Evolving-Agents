"""Command-line runner for one controlled self-evolution iteration."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from self_evolving_agent.agent import AgentResult, ExecutionAgent
from self_evolving_agent.benchmarks import BenchmarkAdapter, BenchmarkTask, get_benchmark
from self_evolving_agent.config import clone_config, load_config, write_config
from self_evolving_agent.critic import (
    CRITIC_MODE_LLM,
    CRITIC_MODE_RULE,
    diagnose_failures,
    write_diagnosis,
)
from self_evolving_agent.execution import (
    apply_patch_text,
    cleanup_workspace,
    classify_execution,
    collect_patch_diff,
    prepare_task_workspace,
    run_command,
)
from self_evolving_agent.harness_report import score_harness_report
from self_evolving_agent.llm import LLMClient
from self_evolving_agent.optimizer import propose_candidate_config
from self_evolving_agent.promotion import (
    PromotionDecision,
    decide_promotion,
    write_decision,
)
from self_evolving_agent.safety import safety_check, write_safety_result
from self_evolving_agent.scoring import EvaluationScore, score_records, write_score
from self_evolving_agent.tracing import TraceLogger, build_trace_record


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config)
    config = _with_agent_mode(load_config(config_path), args.agent_mode)
    metric = config.get("evaluation", {}).get("promotion_metric", "aggregate_score")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
    run_dir = Path(args.output_dir) / run_id
    config_dir = run_dir / "configs"
    trace_dir = run_dir / "traces"
    score_dir = run_dir / "scores"
    config_dir.mkdir(parents=True, exist_ok=True)

    archived_config = config_dir / config_path.name
    write_config(config, archived_config)

    if args.benchmark == "swe-smoke" and not args.task_id:
        raise SystemExit("swe-smoke only supports single-task runs; pass --task-id.")

    benchmark: BenchmarkAdapter = get_benchmark(
        args.benchmark,
        swe_smoke_config=args.swe_smoke_config,
        swe_subset_config=args.swe_subset_config,
    )
    workspace_root = run_dir / "workspaces"

    if args.task_id:
        return _run_single_task(
            args=args,
            benchmark=benchmark,
            config=config,
            config_path=str(archived_config),
            run_id=run_id,
            run_dir=run_dir,
            workspace_root=workspace_root,
        )

    train_records, train_score = _run_and_score(
        run_id=run_id,
        config=config,
        config_path=str(archived_config),
        tasks=benchmark.train_tasks(),
        trace_path=trace_dir / "train_current.jsonl",
        score_path=score_dir / "train_current.json",
        workspace_root=workspace_root,
        keep_workdirs=args.keep_workdirs,
        command_timeout_s=args.command_timeout,
    )

    critic_settings = config.get("critic", {}) or {}
    critic_mode = args.critic_mode or str(
        critic_settings.get("mode", CRITIC_MODE_RULE)
    )
    critic_max_evidence_chars = (
        args.critic_max_evidence_chars
        if args.critic_max_evidence_chars is not None
        else int(critic_settings.get("max_evidence_chars", 12000))
    )
    critic_llm_client = LLMClient(config) if critic_mode == CRITIC_MODE_LLM else None
    diagnosis = diagnose_failures(
        train_records,
        critic_mode=critic_mode,
        llm_client=critic_llm_client,
        max_evidence_chars=critic_max_evidence_chars,
    )
    diagnosis_path = run_dir / "critic_diagnosis.json"
    write_diagnosis(diagnosis, diagnosis_path)

    candidate_config = propose_candidate_config(config, diagnosis)
    candidate_path = config_dir / "candidate_agent.yaml"
    write_config(candidate_config, candidate_path)

    # Gate 1 (safety): reject candidates that weaken required safety behavio
    # *before* spending compute on the held-out performance comparison.
    safety_result = safety_check(config, candidate_config)
    safety_path = run_dir / "safety_check.json"
    write_safety_result(safety_result, safety_path)

    decision_path = run_dir / "promotion_decision.json"

    if not safety_result.passed:
        heldout_current_records = []
        heldout_current_score = None
        heldout_candidate_score = None
        decision = PromotionDecision(
            accepted=False,
            metric=metric,
            current_score=0.0,
            candidate_score=0.0,
            reason=f"rejected by safety checker: {safety_result.reason}",
        )
        write_decision(decision, decision_path)
    else:
        n_samples = max(1, args.heldout_samples)

        heldout_current_records, current_samples = _sample_heldout(
            run_id=run_id,
            config=config,
            config_path=str(archived_config),
            benchmark=benchmark,
            label="heldout_current",
            n_samples=n_samples,
            trace_dir=trace_dir,
            score_dir=score_dir,
            workspace_root=workspace_root,
            args=args,
        )
        _, candidate_samples = _sample_heldout(
            run_id=run_id,
            config=candidate_config,
            config_path=str(candidate_path),
            benchmark=benchmark,
            label="heldout_candidate",
            n_samples=n_samples,
            trace_dir=trace_dir,
            score_dir=score_dir,
            workspace_root=workspace_root,
            args=args,
        )

        heldout_current_score = current_samples[0]
        heldout_candidate_score = candidate_samples[0]

        if args.heldout_current_report:
            current_samples = [
                score_harness_report(
                    tasks=benchmark.heldout_tasks(),
                    aggregate_report_path=args.heldout_current_report,
                    detail_report_root=args.heldout_current_report_dir,
                    aggregate_metric=metric,
                )
            ]
            heldout_current_score = current_samples[0]
        if args.heldout_candidate_report:
            candidate_samples = [
                score_harness_report(
                    tasks=benchmark.heldout_tasks(),
                    aggregate_report_path=args.heldout_candidate_report,
                    detail_report_root=args.heldout_candidate_report_dir,
                    aggregate_metric=metric,
                )
            ]
            heldout_candidate_score = candidate_samples[0]

        # Gate 2 (performance): only promote on a held-out improvement that
        # exceeds the noise margin, using the mean of all samples.
        decision = decide_promotion(
            current=current_samples,
            candidate=candidate_samples,
            metric=metric,
            margin=args.promotion_margin,
        )
        write_decision(decision, decision_path)
        if decision.accepted:
            write_config(candidate_config, config_dir / "promoted_agent.yaml")

    if args.export_predictions:
        prediction_records = list(train_records) + list(heldout_current_records)
        prediction_path = Path(args.export_predictions)
        _write_predictions(
            records=prediction_records,
            path=prediction_path,
            allow_gold_patch_fallback=args.agent_mode == "gold-edit",
            model_name_or_path=args.prediction_model_name
            or config.get("name", "self-evolving-agent"),
        )

    manifest = {
        "run_id": run_id,
        "benchmark": benchmark.name,
        "agent_mode": args.agent_mode,
        "input_config": str(archived_config),
        "candidate_config": str(candidate_path),
        "critic_diagnosis": str(diagnosis_path),
        "critic_mode": critic_mode,
        "critic_max_evidence_chars": critic_max_evidence_chars,
        "safety_check": str(safety_path),
        "safety_passed": safety_result.passed,
        "promotion_decision": str(decision_path),
        "exported_predictions": args.export_predictions,
        "heldout_current_report": args.heldout_current_report,
        "heldout_candidate_report": args.heldout_candidate_report,
        "promotion_margin": args.promotion_margin,
        "heldout_samples": args.heldout_samples,
        "structured_updates": candidate_config.get("optimizer", {}).get(
            "structured_updates", []
        ),
        "train_score": train_score.aggregate_score,
        "heldout_current_score": (
            heldout_current_score.aggregate_score
            if heldout_current_score is not None
            else None
        ),
        "heldout_candidate_score": (
            heldout_candidate_score.aggregate_score
            if heldout_candidate_score is not None
            else None
        ),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"Run directory: {run_dir}")
    print(f"Train aggregate score: {train_score.aggregate_score:.3f}")
    if not safety_result.passed:
        print(f"Safety check: FAILED ({safety_result.reason})")
    else:
        print("Safety check: passed")
        print(f"Held-out current score: {heldout_current_score.aggregate_score:.3f}")
        print(f"Held-out candidate score: {heldout_candidate_score.aggregate_score:.3f}")
    print(f"Promotion decision: {'accepted' if decision.accepted else 'rejected'}")
    print(f"Critic diagnosis: {diagnosis_path}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to agent config YAML")
    parser.add_argument(
        "--agent-mode",
        choices=["gold-edit", "self-edit"],
        default="gold-edit",
        help="gold-edit uses configured oracle edits; self-edit infers edits from issue/repo",
    )
    parser.add_argument(
        "--benchmark",
        choices=["mock", "local-toy", "swe-smoke", "swe-lite-subset"],
        default="mock",
        help="Benchmark adapter to run",
    )
    parser.add_argument(
        "--swe-smoke-config",
        default="configs/swe_smoke_instance.json",
        help="JSON config for the single SWE-bench Lite smoke instance",
    )
    parser.add_argument(
        "--swe-subset-config",
        default="configs/swe_lite_subset_15.json",
        help="JSON snapshot for the fixed SWE-bench Lite subset",
    )
    parser.add_argument("--output-dir", default="runs", help="Directory for run artifacts")
    parser.add_argument("--run-id", default=None, help="Optional deterministic run id")
    parser.add_argument("--task-id", default=None, help="Run one benchmark task only")
    parser.add_argument(
        "--split",
        choices=["all", "train", "heldout"],
        default="all",
        help="Split to search when --task-id is used",
    )
    parser.add_argument(
        "--keep-workdirs",
        action="store_true",
        help="Keep per-task workspaces under the run directory",
    )
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=None,
        help="Override timeout in seconds for setup and test commands",
    )
    parser.add_argument(
        "--heldout-samples",
        type=int,
        default=1,
        help="Number of held-out evaluation samples per config (noise control)",
    )
    parser.add_argument(
        "--promotion-margin",
        type=float,
        default=0.0,
        help="Minimum mean held-out improvement required to promote",
    )
    parser.add_argument(
        "--critic-mode",
        choices=[CRITIC_MODE_RULE, CRITIC_MODE_LLM],
        default=None,
        help="Critic implementation for train-trace diagnosis; defaults to config critic.mode or rule.",
    )
    parser.add_argument(
        "--critic-max-evidence-chars",
        type=int,
        default=None,
        help="Maximum sanitized trace-evidence characters sent to the LLM critic.",
    )
    parser.add_argument(
        "--heldout-current-report",
        default=None,
        help="Official SWE-bench aggregate report.json for current held-out predictions",
    )
    parser.add_argument(
        "--heldout-current-report-dir",
        default=None,
        help="Directory containing per-instance report.json files for current held-out predictions",
    )
    parser.add_argument(
        "--heldout-candidate-report",
        default=None,
        help="Official SWE-bench aggregate report.json for candidate held-out predictions",
    )
    parser.add_argument(
        "--heldout-candidate-report-dir",
        default=None,
        help="Directory containing per-instance report.json files for candidate held-out predictions",
    )
    parser.add_argument(
        "--export-predictions",
        default=None,
        help="If set, write official predictions.jsonl for the current config run",
    )
    parser.add_argument(
        "--prediction-model-name",
        default=None,
        help="model_name_or_path field written to predictions.jsonl",
    )
    return parser.parse_args(argv)


def _sample_heldout(
    *,
    run_id: str,
    config: dict[str, Any],
    config_path: str,
    benchmark: BenchmarkAdapter,
    label: str,
    n_samples: int,
    trace_dir: Path,
    score_dir: Path,
    workspace_root: Path,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], list[EvaluationScore]]:
    """Evaluate the held-out split ``n_samples`` times for noise control.

    Sample 0 keeps the canonical filename; extra samples are suffixed. With the
    deterministic mock agent the samples are identical; once the agent is
    LLM-driven (M4) they vary and the mean drives the promotion decision.
    """
    sample0_records: list[dict[str, Any]] = []
    scores: list[EvaluationScore] = []
    for sample_index in range(n_samples):
        suffix = "" if sample_index == 0 else f"_s{sample_index}"
        records, score = _run_and_score(
            run_id=run_id,
            config=config,
            config_path=config_path,
            tasks=benchmark.heldout_tasks(),
            trace_path=trace_dir / f"{label}{suffix}.jsonl",
            score_path=score_dir / f"{label}{suffix}.json",
            workspace_root=workspace_root,
            keep_workdirs=args.keep_workdirs,
            command_timeout_s=args.command_timeout,
        )
        if sample_index == 0:
            sample0_records = records
        scores.append(score)
    return sample0_records, scores


def _run_and_score(
    *,
    run_id: str,
    config: dict[str, Any],
    config_path: str,
    tasks: list[BenchmarkTask],
    trace_path: Path,
    score_path: Path,
    workspace_root: Path,
    keep_workdirs: bool,
    command_timeout_s: int | None,
) -> tuple[list[dict[str, Any]], EvaluationScore]:
    agent = ExecutionAgent(config)
    records = [
        build_trace_record(
            run_id=run_id,
            config=config,
            config_path=config_path,
            result=_run_task(
                agent=agent,
                task=task,
                workspace_root=workspace_root,
                keep_workdir=keep_workdirs,
                default_command_timeout_s=command_timeout_s,
            ),
        )
        for task in tasks
    ]
    TraceLogger(trace_path).write_records(records)
    aggregate_metric = config.get("evaluation", {}).get("aggregate_metric", "task_success_rate")
    score = score_records(records, aggregate_metric=aggregate_metric)
    write_score(score, score_path)
    return records, score


def _write_predictions(
    *,
    records: list[dict[str, Any]],
    path: Path,
    model_name_or_path: str,
    allow_gold_patch_fallback: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            patch = record.get("patch_diff") or ""
            if not patch and allow_gold_patch_fallback:
                patch = record["task"]["metadata"].get("gold_patch", "")
            handle.write(
                json.dumps(
                    {
                        "instance_id": record["task"]["id"],
                        "model_name_or_path": model_name_or_path,
                        "model_patch": patch,
                    }
                )
                + "\n"
            )


def _run_single_task(
    *,
    args: argparse.Namespace,
    benchmark: BenchmarkAdapter,
    config: dict[str, Any],
    config_path: str,
    run_id: str,
    run_dir: Path,
    workspace_root: Path,
) -> int:
    task = _find_task(benchmark, task_id=args.task_id, split=args.split)
    trace_dir = run_dir / "traces"
    score_dir = run_dir / "scores"
    records, score = _run_and_score(
        run_id=run_id,
        config=config,
        config_path=config_path,
        tasks=[task],
        trace_path=trace_dir / "single_task.jsonl",
        score_path=score_dir / "single_task.json",
        workspace_root=workspace_root,
        keep_workdirs=args.keep_workdirs,
        command_timeout_s=args.command_timeout,
    )
    manifest = {
        "run_id": run_id,
        "mode": "single_task",
        "benchmark": benchmark.name,
        "agent_mode": args.agent_mode,
        "task_id": task.task_id,
        "trace": str(trace_dir / "single_task.jsonl"),
        "score": str(score_dir / "single_task.json"),
        "execution_status": records[0]["execution_status"],
        "aggregate_score": score.aggregate_score,
    }
    if args.export_predictions:
        _write_predictions(
            records=records,
            path=Path(args.export_predictions),
            allow_gold_patch_fallback=args.agent_mode == "gold-edit",
            model_name_or_path=args.prediction_model_name
            or config.get("name", "self-evolving-agent"),
        )
        manifest["exported_predictions"] = args.export_predictions
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"Run directory: {run_dir}")
    print(f"Task: {task.task_id}")
    print(f"Execution status: {records[0]['execution_status']}")
    print(f"Aggregate score: {score.aggregate_score:.3f}")
    return 0


def _find_task(
    benchmark: BenchmarkAdapter,
    *,
    task_id: str,
    split: str,
) -> BenchmarkTask:
    tasks: list[BenchmarkTask] = []
    if split in {"all", "train"}:
        tasks.extend(benchmark.train_tasks())
    if split in {"all", "heldout"}:
        tasks.extend(benchmark.heldout_tasks())
    for task in tasks:
        if task.task_id == task_id:
            return task
    available = ", ".join(task.task_id for task in tasks)
    raise SystemExit(f"Unknown task id '{task_id}'. Available tasks: {available}")


def _with_agent_mode(config: dict[str, Any], agent_mode: str) -> dict[str, Any]:
    effective = clone_config(config)
    effective.setdefault("workflow", {})["agent_mode"] = agent_mode
    return effective


def _run_task(
    *,
    agent: ExecutionAgent,
    task: BenchmarkTask,
    workspace_root: Path,
    keep_workdir: bool,
    default_command_timeout_s: int | None,
) -> AgentResult:
    timeout_s = (
        default_command_timeout_s
        if default_command_timeout_s is not None
        else task.command_timeout_s
        if task.command_timeout_s is not None
        else 30
    )
    workspace = prepare_task_workspace(task, workspace_root, timeout_s)
    command_results = [result.to_trace() for result in workspace.setup_commands]
    environment_error = workspace.error

    if environment_error:
        result = _environment_failure_result(task, environment_error)
        patch_diff = ""
    else:
        task_setup_start = len(command_results)
        for command in task.setup_commands:
            command_results.append(
                run_command(
                    command,
                    cwd=workspace.path,
                    timeout_s=timeout_s,
                    phase="setup",
                ).to_trace()
            )

        setup_failed = any(
            command["phase"] == "setup"
            and (command["timed_out"] or command["exit_code"] != 0)
            for command in command_results[task_setup_start:]
        )

        if setup_failed:
            result = _environment_failure_result(task, "Setup command failed.")
            patch_diff = ""
        else:
            result = agent.solve(task, workspace_path=workspace.path)
            patch_diff = collect_patch_diff(workspace.path, timeout_s)
            if task.test_patch:
                command_results.append(
                    apply_patch_text(
                        workspace=workspace.path,
                        patch_text=task.test_patch,
                        timeout_s=timeout_s,
                        phase="test_patch",
                    ).to_trace()
                )
            command_results.extend(_ensure_test_dependencies(task, workspace.path, timeout_s))
            for command in task.test_commands:
                command_results.append(
                    run_command(
                        command,
                        cwd=workspace.path,
                        timeout_s=timeout_s,
                        phase="test",
                    ).to_trace()
                )

    status = classify_execution(
        agent_success=result.success,
        environment_error=environment_error,
        command_results=command_results,
    )
    success = status == "success"
    test_results = _merge_test_results(result.test_results, command_results)
    workspace_record = {
        "path": str(workspace.path),
        "repo_path": task.repo_path,
        "repo_url": task.repo_url,
        "base_commit": task.base_commit,
        "setup_commands": list(task.setup_commands),
        "has_test_patch": bool(task.test_patch),
        "kept": keep_workdir,
        "cleaned": not keep_workdir,
        "error": environment_error,
    }

    final_result = replace(
        result,
        success=success,
        test_commands=list(task.test_commands) or result.test_commands,
        test_results=test_results,
        patch_diff=patch_diff,
        command_results=command_results,
        workspace=workspace_record,
        execution_status=status,
        environment_error=environment_error,
    )

    if not keep_workdir:
        cleanup_workspace(workspace.path)
    return final_result


def _environment_failure_result(task: BenchmarkTask, error: str) -> AgentResult:
    return AgentResult(
        task=task,
        output="",
        success=False,
        intermediate_steps=["prepare_workspace_failed"],
        retrieved_context=[],
        tool_calls=[],
        file_edits=[],
        test_commands=list(task.test_commands),
        test_results=[],
        latency_ms=1,
        cost=0.0,
        environment_error=error,
    )


def _merge_test_results(
    existing: list[dict[str, Any]],
    command_results: list[dict[str, object]],
) -> list[dict[str, Any]]:
    merged = list(existing)
    for result in command_results:
        if result["phase"] != "test":
            continue
        merged.append(
            {
                "name": result["command"],
                "passed": result["exit_code"] == 0 and not result["timed_out"],
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
            }
        )
    return merged


def _ensure_test_dependencies(
    task: BenchmarkTask,
    workspace_path: Path,
    timeout_s: int,
) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = []
    uses_pytest = any(
        " -m pytest" in command or command.startswith("pytest ")
        for command in task.test_commands
    )

    if _should_install_repo_package(workspace_path):
        install_repo = run_command(
            "python -m pip install -e .",
            cwd=workspace_path,
            timeout_s=timeout_s,
            phase="setup",
        )
        commands.append(install_repo.to_trace())

    if not uses_pytest:
        return commands

    check = run_command(
        'python -c "import pytest"',
        cwd=workspace_path,
        timeout_s=timeout_s,
        phase="setup",
    )
    if check.exit_code == 0 and not check.timed_out:
        return commands

    install = run_command(
        "python -m pip install pytest",
        cwd=workspace_path,
        timeout_s=timeout_s,
        phase="setup",
    )
    commands.append(install.to_trace())
    return commands


def _should_install_repo_package(workspace_path: Path) -> bool:
    return (workspace_path / "setup.py").exists() or (workspace_path / "pyproject.toml").exists()


if __name__ == "__main__":
    raise SystemExit(main())
