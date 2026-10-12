"""Single-process multi-round controlled evolution runner.

M5.6a wires together the existing critic, aggregator, optimizer, candidate
builder, safety gate, promotion gate, and append-only memory ledger. Benchmark
execution remains injectable; official adapters can return structured results
without changing legacy callback behavior.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from time import perf_counter
from pathlib import Path
from typing import Any, Callable

from self_evolving_agent.candidate_builder import build_candidate_prompt_config
from self_evolving_agent.config import (
    clone_config,
    config_fingerprint,
    load_config,
    write_config,
)
from self_evolving_agent.critic import CRITIC_MODE_RULE, diagnose_failures
from self_evolving_agent.evolution_memory import (
    append_evolution_record,
    build_evolution_record,
    read_evolution_memory,
    summarize_evolution_memory,
    verify_hash_chain,
)
from self_evolving_agent.evolution_evaluation import (
    EvolutionEvaluationResult,
    default_evaluation_cost,
    merge_evaluation_costs,
    normalize_evaluation_cost,
)
from self_evolving_agent.failure_cause_artifacts import (
    ROOT_CAUSE_EVENTS_ARTIFACT_NAME,
    write_root_cause_events_artifact,
)
from self_evolving_agent.failure_feedback import build_round_failure_feedback
from self_evolving_agent.optimizer import (
    OPTIMIZER_MODE_LLM,
    OPTIMIZER_MODE_LLM_V2,
    OPTIMIZER_MODE_RULE,
    build_structured_updates,
)
from self_evolving_agent.promotion import PromotionDecision, decide_promotion
from self_evolving_agent.progress import ProgressReporter
from self_evolving_agent.safety import SafetyResult, safety_check
from self_evolving_agent.scoring import EvaluationScore, score_records


TrainRunner = Callable[
    [dict[str, Any], list[Any], int],
    list[dict[str, Any]] | EvolutionEvaluationResult,
]
DevEvaluator = Callable[
    [dict[str, Any], list[Any], int],
    EvaluationScore | EvolutionEvaluationResult,
]
SafetyChecker = Callable[[dict[str, Any], dict[str, Any]], SafetyResult]
OptimizerMemoryBlockProvider = Callable[
    [dict[str, Any], int],
    dict[str, Any] | None,
]


def run_evolution_rounds(
    *,
    base_config: dict[str, Any],
    train_tasks: list[Any],
    dev_tasks: list[Any],
    rounds: int,
    run_dir: str | Path,
    optimizer_mode: str = OPTIMIZER_MODE_RULE,
    llm_client: Any | None = None,
    max_updates_per_round: int = 3,
    optimizer_max_completion_tokens: int = 900,
    optimizer_timeout_s: int | None = None,
    promotion_margin: float = 0.0,
    aggregate_metric: str = "task_success_rate",
    dry_run: bool = False,
    resume: bool = False,
    train_runner: TrainRunner | None = None,
    dev_evaluator: DevEvaluator | None = None,
    safety_checker: SafetyChecker | None = None,
    optimizer_memory_block_provider: OptimizerMemoryBlockProvider | None = None,
    progress_reporter: ProgressReporter | None = None,
    resume_incomplete_round: bool = False,
) -> dict[str, Any]:
    """Run a deterministic, auditable multi-round evolution loop."""

    if rounds < 0:
        raise ValueError("rounds must be non-negative")
    root = Path(run_dir)
    run_started_at = perf_counter()
    if progress_reporter is not None:
        progress_reporter.emit(
            "run_started",
            status="started",
            rounds=rounds,
            completed_rounds=len(read_evolution_memory(root / "evolution_memory.jsonl")),
            train_tasks=len(train_tasks),
            dev_tasks=len(dev_tasks),
            optimizer_mode=optimizer_mode,
        )
    rounds_root = root / "rounds"
    rounds_root.mkdir(parents=True, exist_ok=True)
    memory_path = root / "evolution_memory.jsonl"
    existing_records = read_evolution_memory(memory_path)

    if existing_records and not resume:
        raise ValueError(
            "run_dir already contains evolution memory; use resume=True or "
            "choose a new run_dir"
        )
    if resume and not existing_records:
        raise ValueError("Cannot resume: run_dir contains no evolution memory")
    if existing_records and not verify_hash_chain(existing_records):
        raise ValueError("Cannot resume: evolution memory hash chain is invalid")

    if train_runner is None:
        if not dry_run:
            raise ValueError("train_runner is required unless dry_run=True")
        train_runner = _dry_run_train_runner
    if dev_evaluator is None:
        if not dry_run:
            raise ValueError("dev_evaluator is required unless dry_run=True")
        dev_evaluator = _dry_run_dev_evaluator
    if safety_checker is None:
        safety_checker = safety_check

    completed_rounds = len(existing_records)
    current_config = (
        _load_resume_config(root, completed_rounds)
        if resume
        else clone_config(base_config)
    )
    prior_summary = summarize_evolution_memory(existing_records)
    promoted_count = int(prior_summary.get("promoted_count") or 0)
    rejected_count = int(prior_summary.get("rejected_count") or 0)
    round_summaries = _load_existing_round_summaries(
        rounds_root,
        completed_rounds,
    )
    invocation_round_summaries: list[dict[str, Any]] = []

    for offset in range(rounds):
        round_index = completed_rounds + offset
        round_number = round_index + 1
        round_dir = rounds_root / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        round_started_at = perf_counter()
        if progress_reporter is not None:
            progress_reporter.emit(
                "round_started",
                status="started",
                round=round_number,
                round_index=round_index,
                round_total=completed_rounds + rounds,
            )
        timing: dict[str, Any] = {
            "schema_version": "m5.6.evolution_timing.v1",
            "round": round_number,
            "round_index": round_index,
        }

        current_path = round_dir / "current_config.yaml"
        write_config(current_config, current_path)

        stage_started_at = perf_counter()
        if progress_reporter is not None:
            progress_reporter.emit(
                "train_evaluation_started",
                status="started",
                round=round_number,
                tasks=len(train_tasks),
            )
        train_output = train_runner(current_config, train_tasks, round_index)
        train_evaluation = _normalize_train_evaluation(
            train_output,
            train_tasks,
            aggregate_metric=aggregate_metric,
        )
        timing["train_evaluation_elapsed_s"] = _elapsed_s(stage_started_at)
        if progress_reporter is not None:
            progress_reporter.emit(
                "train_evaluation_completed",
                status="completed",
                round=round_number,
                duration_s=timing["train_evaluation_elapsed_s"],
                score=train_evaluation.score.aggregate_score,
                cache_hit=train_evaluation.cache_hit,
            )
        train_records = train_evaluation.records
        train_records_path = round_dir / "train_records.json"
        _write_json(train_records_path, train_records)
        root_cause_events_path = round_dir / ROOT_CAUSE_EVENTS_ARTIFACT_NAME
        write_root_cause_events_artifact(
            root_cause_events_path,
            train_records,
            source="train",
            metadata={
                "round": round_number,
                "round_index": round_index,
                "evaluation_id": train_evaluation.evaluation_id,
                "task_ids": train_evaluation.task_ids,
                "source": train_evaluation.source,
            },
        )
        train_score = train_evaluation.score
        train_score_path = round_dir / "train_score.json"
        _write_json(train_score_path, asdict(train_score))

        stage_started_at = perf_counter()
        if progress_reporter is not None:
            progress_reporter.emit("critic_started", status="started", round=round_number)
        diagnosis = train_evaluation.diagnosis or diagnose_failures(
            train_records,
            critic_mode=CRITIC_MODE_RULE,
        )
        prior_round_feedback = _load_prior_round_optimizer_feedback(
            rounds_root,
            round_index,
        )
        if prior_round_feedback:
            diagnosis = deepcopy(diagnosis)
            diagnosis["prior_round_feedback"] = prior_round_feedback
        timing["diagnosis_elapsed_s"] = _elapsed_s(stage_started_at)
        if progress_reporter is not None:
            progress_reporter.emit(
                "critic_completed",
                status="completed",
                round=round_number,
                duration_s=timing["diagnosis_elapsed_s"],
                failed_records=len(diagnosis.get("failure_analyses", [])),
            )
        diagnosis_path = round_dir / "critic_diagnosis.json"
        _write_json(diagnosis_path, diagnosis)

        effective_optimizer_mode = (
            OPTIMIZER_MODE_RULE if dry_run else optimizer_mode
        )
        effective_llm_client = None if dry_run else llm_client
        optimizer_memory_contract: dict[str, Any] | None = None
        optimizer_memory_contract_path: Path | None = None
        prompt_update_optimizer_v2_result: dict[str, Any] | None = None
        prompt_update_optimizer_v2_path: Path | None = None
        operation_planner_metadata: dict[str, Any] | None = None
        optimizer_memory_block = (
            optimizer_memory_block_provider(diagnosis, round_index)
            if optimizer_memory_block_provider is not None
            else None
        )
        stage_started_at = perf_counter()
        if progress_reporter is not None:
            progress_reporter.emit(
                "optimizer_started",
                status="started",
                round=round_number,
                mode=effective_optimizer_mode,
            )
        if (
            effective_optimizer_mode == OPTIMIZER_MODE_LLM
            and optimizer_memory_block is not None
        ):
            from self_evolving_agent.optimizer_memory_contract import (
                build_memory_grounded_optimizer_result,
            )

            optimizer_memory_contract = build_memory_grounded_optimizer_result(
                diagnosis,
                llm_client=effective_llm_client,
                memory_block=optimizer_memory_block,
                fallback_updates=build_structured_updates(
                    diagnosis,
                    optimizer_mode=OPTIMIZER_MODE_RULE,
                ),
                max_updates=max_updates_per_round,
                max_completion_tokens=optimizer_max_completion_tokens,
                timeout_s=optimizer_timeout_s,
            )
            structured_updates = optimizer_memory_contract["updates"]
            optimizer_memory_contract_path = (
                round_dir / "optimizer_memory_contract.json"
            )
            _write_json(optimizer_memory_contract_path, optimizer_memory_contract)
        elif effective_optimizer_mode == OPTIMIZER_MODE_LLM_V2:
            from self_evolving_agent.prompt_update_optimizer_v2 import (
                build_v2_prompt_update_result,
            )

            prompt_update_optimizer_v2_path = (
                round_dir / "prompt_update_optimizer_v2.json"
            )
            legacy_updates = build_structured_updates(
                diagnosis,
                optimizer_mode=OPTIMIZER_MODE_RULE,
            )
            if resume_incomplete_round and prompt_update_optimizer_v2_path.is_file():
                expected = build_v2_prompt_update_result(
                    diagnosis,
                    llm_client=None,
                    base_config=current_config,
                    legacy_updates=legacy_updates,
                    max_updates=max_updates_per_round,
                    max_completion_tokens=optimizer_max_completion_tokens,
                    timeout_s=optimizer_timeout_s,
                )
                prompt_update_optimizer_v2_result = _load_reusable_v2_optimizer_result(
                    prompt_update_optimizer_v2_path,
                    expected_messages=expected["optimizer_messages"],
                )
            else:
                prompt_update_optimizer_v2_result = build_v2_prompt_update_result(
                    diagnosis,
                    llm_client=effective_llm_client,
                    base_config=current_config,
                    legacy_updates=legacy_updates,
                    max_updates=max_updates_per_round,
                    max_completion_tokens=optimizer_max_completion_tokens,
                    timeout_s=optimizer_timeout_s,
                )
                _write_json(
                    prompt_update_optimizer_v2_path,
                    prompt_update_optimizer_v2_result,
                )
            structured_updates = prompt_update_optimizer_v2_result["compiled_updates"]
        else:
            structured_updates = build_structured_updates(
                diagnosis,
                optimizer_mode=effective_optimizer_mode,
                llm_client=effective_llm_client,
                base_config=current_config,
                max_updates=max_updates_per_round,
                max_completion_tokens=optimizer_max_completion_tokens,
                timeout_s=optimizer_timeout_s,
            )
        timing["optimizer_elapsed_s"] = _elapsed_s(stage_started_at)
        if progress_reporter is not None:
            progress_reporter.emit(
                "optimizer_completed",
                status="completed",
                round=round_number,
                duration_s=timing["optimizer_elapsed_s"],
                updates=len(structured_updates),
            )

        optimizer_structured_updates = list(structured_updates)
        if effective_optimizer_mode == OPTIMIZER_MODE_LLM:
            from self_evolving_agent.prompt_operation_planner import (
                plan_prompt_edit_operations,
            )

            stage_started_at = perf_counter()
            if progress_reporter is not None:
                progress_reporter.emit(
                    "operation_planner_started",
                    status="started",
                    round=round_number,
                )
            operation_planner_result = plan_prompt_edit_operations(
                current_config,
                structured_updates,
                llm_client=effective_llm_client,
                diagnosis=diagnosis,
                max_completion_tokens=optimizer_max_completion_tokens,
                timeout_s=optimizer_timeout_s,
                return_metadata=True,
            )
            timing["operation_planner_elapsed_s"] = _elapsed_s(stage_started_at)
            if progress_reporter is not None:
                progress_reporter.emit(
                    "operation_planner_completed",
                    status="completed",
                    round=round_number,
                    duration_s=timing["operation_planner_elapsed_s"],
                    updates=len(operation_planner_result["updates"]),
                )
            structured_updates = operation_planner_result["updates"]
            operation_planner_metadata = operation_planner_result[
                "operation_planner"
            ]

        stage_started_at = perf_counter()
        if progress_reporter is not None:
            progress_reporter.emit(
                "candidate_builder_started", status="started", round=round_number
            )
        candidate_config = build_candidate_prompt_config(
            current_config,
            structured_updates,
            diagnosis=diagnosis,
            candidate_prefix=f"round-{round_number:03d}",
            max_updates=max_updates_per_round,
        )
        timing["candidate_builder_elapsed_s"] = _elapsed_s(stage_started_at)
        if progress_reporter is not None:
            progress_reporter.emit(
                "candidate_builder_completed",
                status="completed",
                round=round_number,
                duration_s=timing["candidate_builder_elapsed_s"],
                applied_updates=len(
                    candidate_config.get("candidate_builder", {}).get(
                        "applied_updates", []
                    )
                ),
            )
        candidate_config["optimizer"] = {
            "source": (
                "prompt_update_optimizer_v2"
                if effective_optimizer_mode == OPTIMIZER_MODE_LLM_V2
                else (
                    "llm_optimizer"
                    if effective_optimizer_mode != OPTIMIZER_MODE_RULE
                    else "offline_rule_optimizer"
                )
            ),
            "structured_updates": candidate_config.get("candidate_builder", {}).get(
                "applied_updates",
                [],
            ),
            "optimizer_structured_updates": optimizer_structured_updates,
            "proposed_structured_updates": structured_updates,
            "diagnosis_summary": diagnosis.get("summary", ""),
            "optimizer_elapsed_s": timing.get("optimizer_elapsed_s", 0.0),
        }
        if operation_planner_metadata is not None:
            operation_planner_metadata["elapsed_s"] = timing.get(
                "operation_planner_elapsed_s",
                0.0,
            )
            candidate_config["optimizer"]["operation_planner"] = operation_planner_metadata
        if optimizer_memory_contract is not None:
            candidate_config["optimizer"]["memory_contract"] = {
                "artifact": str(optimizer_memory_contract_path),
                "memory_used": optimizer_memory_contract.get("memory_used", False),
                "fallback_reason": optimizer_memory_contract.get(
                    "fallback_reason",
                    "",
                ),
                "verifier_overall_action": (
                    optimizer_memory_contract.get("verifier_report", {}) or {}
                ).get("overall_action", ""),
            }
        if prompt_update_optimizer_v2_result is not None:
            candidate_config["optimizer"]["prompt_update_optimizer_v2"] = {
                "artifact": str(prompt_update_optimizer_v2_path),
                "proposal_count": prompt_update_optimizer_v2_result.get(
                    "proposal_count",
                    0,
                ),
                "accepted_proposal_count": prompt_update_optimizer_v2_result.get(
                    "accepted_proposal_count",
                    0,
                ),
                "compiled_update_count": prompt_update_optimizer_v2_result.get(
                    "compiled_update_count",
                    0,
                ),
                "fixture_rejected_proposal_count": prompt_update_optimizer_v2_result.get(
                    "fixture_rejected_proposal_count",
                    0,
                ),
                "fixture_gate_passed": prompt_update_optimizer_v2_result.get(
                    "fixture_gate_passed",
                    False,
                ),
                "linter_metrics": (
                    prompt_update_optimizer_v2_result.get("linter", {}) or {}
                ).get("metrics", {}),
                "prompt_behavior_fixture": _prompt_behavior_fixture_summary(
                    prompt_update_optimizer_v2_result.get(
                        "prompt_behavior_fixtures"
                    )
                ),
            }
        candidate_path = round_dir / "candidate_config.yaml"
        write_config(candidate_config, candidate_path)

        prompt_diff_path = round_dir / "prompt_diff.txt"
        prompt_diff_path.write_text(
            candidate_config.get("candidate_builder", {}).get("prompt_diff", ""),
            encoding="utf-8",
        )

        stage_started_at = perf_counter()
        if progress_reporter is not None:
            progress_reporter.emit("safety_gate_started", status="started", round=round_number)
        safety_result = safety_checker(current_config, candidate_config)
        timing["safety_elapsed_s"] = _elapsed_s(stage_started_at)
        if progress_reporter is not None:
            progress_reporter.emit(
                "safety_gate_completed",
                status="passed" if safety_result.passed else "failed",
                round=round_number,
                duration_s=timing["safety_elapsed_s"],
                violations=len(safety_result.violations),
            )
        safety_path = round_dir / "safety_report.json"
        _write_json(safety_path, asdict(safety_result))

        dev_score_before: EvaluationScore | None = None
        dev_score_after: EvaluationScore | None = None
        dev_current_evaluation: EvolutionEvaluationResult | None = None
        dev_candidate_evaluation: EvolutionEvaluationResult | None = None
        current_score_path = round_dir / "dev_current_score.json"
        candidate_score_path = round_dir / "dev_candidate_score.json"
        promotion_path = round_dir / "promotion_report.json"
        fixture_gate_passed = bool(
            prompt_update_optimizer_v2_result is None
            or prompt_update_optimizer_v2_result.get("fixture_gate_passed", False)
        )
        v2_schema_valid = bool(
            prompt_update_optimizer_v2_result is None
            or (
                prompt_update_optimizer_v2_result.get("structured_output", {}) or {}
            ).get("schema_valid", False)
        )
        has_executable_candidate = bool(
            prompt_update_optimizer_v2_result is None
            or (
                prompt_update_optimizer_v2_result.get("compiled_update_count", 0)
                and candidate_config.get("candidate_builder", {}).get("prompt_diff", "")
            )
        )

        if not v2_schema_valid:
            decision = PromotionDecision(
                accepted=False,
                metric=aggregate_metric,
                current_score=0.0,
                candidate_score=0.0,
                reason="rejected by optimizer structured-output gate",
                margin=promotion_margin,
                current_samples=0,
                candidate_samples=0,
            )
        elif not fixture_gate_passed:
            decision = PromotionDecision(
                accepted=False,
                metric=aggregate_metric,
                current_score=0.0,
                candidate_score=0.0,
                reason="rejected by prompt behavior fixture gate",
                margin=promotion_margin,
                current_samples=0,
                candidate_samples=0,
            )
        elif not has_executable_candidate:
            decision = PromotionDecision(
                accepted=False,
                metric=aggregate_metric,
                current_score=0.0,
                candidate_score=0.0,
                reason="rejected because optimizer produced no executable prompt update candidate",
                margin=promotion_margin,
                current_samples=0,
                candidate_samples=0,
            )
        elif not safety_result.passed:
            decision = PromotionDecision(
                accepted=False,
                metric=aggregate_metric,
                current_score=0.0,
                candidate_score=0.0,
                reason=f"rejected by safety checker: {safety_result.reason}",
                margin=promotion_margin,
            )
        elif dry_run:
            decision = PromotionDecision(
                accepted=False,
                metric=aggregate_metric,
                current_score=0.0,
                candidate_score=0.0,
                reason="dry_run skipped dev evaluation",
                margin=promotion_margin,
                current_samples=0,
                candidate_samples=0,
            )
        else:
            stage_started_at = perf_counter()
            if progress_reporter is not None:
                progress_reporter.emit(
                    "dev_current_evaluation_started",
                    status="started",
                    round=round_number,
                    tasks=len(dev_tasks),
                )
            dev_current_output = dev_evaluator(
                current_config,
                dev_tasks,
                round_index,
            )
            dev_current_evaluation = _normalize_dev_evaluation(
                dev_current_output,
                dev_tasks,
            )
            timing["dev_current_evaluation_elapsed_s"] = _elapsed_s(stage_started_at)
            if progress_reporter is not None:
                progress_reporter.emit(
                    "dev_current_evaluation_completed",
                    status="completed",
                    round=round_number,
                    duration_s=timing["dev_current_evaluation_elapsed_s"],
                    score=dev_current_evaluation.score.aggregate_score,
                    cache_hit=dev_current_evaluation.cache_hit,
                )

            stage_started_at = perf_counter()
            if progress_reporter is not None:
                progress_reporter.emit(
                    "dev_candidate_evaluation_started",
                    status="started",
                    round=round_number,
                    tasks=len(dev_tasks),
                )
            dev_candidate_output = dev_evaluator(
                candidate_config,
                dev_tasks,
                round_index,
            )
            dev_candidate_evaluation = _normalize_dev_evaluation(
                dev_candidate_output,
                dev_tasks,
            )
            timing["dev_candidate_evaluation_elapsed_s"] = _elapsed_s(stage_started_at)
            if progress_reporter is not None:
                progress_reporter.emit(
                    "dev_candidate_evaluation_completed",
                    status="completed",
                    round=round_number,
                    duration_s=timing["dev_candidate_evaluation_elapsed_s"],
                    score=dev_candidate_evaluation.score.aggregate_score,
                    cache_hit=dev_candidate_evaluation.cache_hit,
                )
            dev_score_before = dev_current_evaluation.score
            dev_score_after = dev_candidate_evaluation.score
            _write_json(current_score_path, asdict(dev_score_before))
            _write_json(candidate_score_path, asdict(dev_score_after))
            stage_started_at = perf_counter()
            if progress_reporter is not None:
                progress_reporter.emit("promotion_started", status="started", round=round_number)
            decision = decide_promotion(
                current=_promotion_score_samples(dev_current_evaluation),
                candidate=_promotion_score_samples(dev_candidate_evaluation),
                metric=aggregate_metric,
                margin=promotion_margin,
                current_records=dev_current_evaluation.records,
                candidate_records=dev_candidate_evaluation.records,
            )
            timing["promotion_elapsed_s"] = _elapsed_s(stage_started_at)
            if progress_reporter is not None:
                progress_reporter.emit(
                    "promotion_completed",
                    status="accepted" if decision.accepted else "rejected",
                    round=round_number,
                    duration_s=timing["promotion_elapsed_s"],
                    current_score=decision.current_score,
                    candidate_score=decision.candidate_score,
                    margin=decision.margin,
                )

        _write_json(promotion_path, asdict(decision))
        promotion_label = (
            "promoted"
            if decision.accepted
            else ("not_evaluated" if "skipped dev evaluation" in decision.reason else "rejected")
        )

        official_result_feedback = bool(
            dev_current_evaluation is not None
            and dev_candidate_evaluation is not None
            and dev_current_evaluation.source.startswith("official_harness")
            and dev_candidate_evaluation.source.startswith("official_harness")
        )
        failure_taxonomy_before, failure_taxonomy_after, taxonomy_scope = (
            _taxonomy_comparison(
                training_diagnosis=diagnosis,
                current_evaluation=dev_current_evaluation,
                candidate_evaluation=dev_candidate_evaluation,
            )
        )
        round_failure_feedback = build_round_failure_feedback(
            promotion_decision=decision,
            aggregate_metric=aggregate_metric,
            current_score=dev_score_before,
            candidate_score=dev_score_after,
            current_records=(
                dev_current_evaluation.records
                if dev_current_evaluation is not None
                else []
            ),
            candidate_records=(
                dev_candidate_evaluation.records
                if dev_candidate_evaluation is not None
                else []
            ),
            current_diagnosis=(
                dev_current_evaluation.diagnosis
                if dev_current_evaluation is not None
                else {}
            ),
            candidate_diagnosis=(
                dev_candidate_evaluation.diagnosis
                if dev_candidate_evaluation is not None
                else {}
            ),
        )
        round_failure_feedback_path = round_dir / "round_failure_feedback.json"
        _write_json(round_failure_feedback_path, round_failure_feedback)
        timing["round_pre_memory_elapsed_s"] = _elapsed_s(round_started_at)
        timing_path = round_dir / "timing.json"
        _write_json(timing_path, timing)

        evaluation_context = {
            "aggregate_metric": aggregate_metric,
            "timing": timing,
            "taxonomy_comparison_scope": taxonomy_scope,
            "official_result_feedback": official_result_feedback,
            "train_evaluation_id": train_evaluation.evaluation_id,
            "dev_current_evaluation_id": (
                dev_current_evaluation.evaluation_id
                if dev_current_evaluation is not None
                else ""
            ),
            "dev_candidate_evaluation_id": (
                dev_candidate_evaluation.evaluation_id
                if dev_candidate_evaluation is not None
                else ""
            ),
            "train_cache_hit": train_evaluation.cache_hit,
            "dev_current_cache_hit": (
                dev_current_evaluation.cache_hit
                if dev_current_evaluation is not None
                else False
            ),
            "dev_candidate_cache_hit": (
                dev_candidate_evaluation.cache_hit
                if dev_candidate_evaluation is not None
                else False
            ),
            "train_task_ids": train_evaluation.task_ids or _task_ids(train_tasks),
            "dev_task_ids": (
                dev_current_evaluation.task_ids
                if dev_current_evaluation is not None
                and dev_current_evaluation.task_ids
                else _task_ids(dev_tasks)
            ),
            "train_source": train_evaluation.source,
            "dev_current_source": (
                dev_current_evaluation.source
                if dev_current_evaluation is not None
                else "unknown"
            ),
            "dev_candidate_source": (
                dev_candidate_evaluation.source
                if dev_candidate_evaluation is not None
                else "unknown"
            ),
            "train_metadata": train_evaluation.metadata,
            "dev_current_metadata": (
                dev_current_evaluation.metadata
                if dev_current_evaluation is not None
                else {}
            ),
            "dev_candidate_metadata": (
                dev_candidate_evaluation.metadata
                if dev_candidate_evaluation is not None
                else {}
            ),
            "observed_cost": {
                "train": normalize_evaluation_cost(train_evaluation.cost),
                "dev_current": normalize_evaluation_cost(
                    dev_current_evaluation.cost
                    if dev_current_evaluation is not None
                    else {}
                ),
                "dev_candidate": normalize_evaluation_cost(
                    dev_candidate_evaluation.cost
                    if dev_candidate_evaluation is not None
                    else {}
                ),
            },
            "round_failure_feedback": round_failure_feedback,
        }
        round_cost = _round_incurred_cost(
            diagnosis=diagnosis,
            train_evaluation=train_evaluation,
            current_evaluation=dev_current_evaluation,
            candidate_evaluation=dev_candidate_evaluation,
        )
        evaluation_context["newly_incurred_cost"] = round_cost
        artifact_paths = {
            "diagnosis": str(diagnosis_path),
            "root_cause_events": str(root_cause_events_path),
            "candidate_config": str(candidate_path),
            "prompt_diff": str(prompt_diff_path),
            "safety_report": str(safety_path),
            "promotion_report": str(promotion_path),
            "round_failure_feedback": str(round_failure_feedback_path),
            "timing": str(timing_path),
        }
        if optimizer_memory_contract_path is not None:
            artifact_paths["optimizer_memory_contract"] = str(
                optimizer_memory_contract_path
            )
        if prompt_update_optimizer_v2_path is not None:
            artifact_paths["prompt_update_optimizer_v2"] = str(
                prompt_update_optimizer_v2_path
            )
        _merge_artifact_paths(
            artifact_paths,
            "train",
            train_evaluation.artifact_paths,
        )
        if dev_current_evaluation is not None:
            _merge_artifact_paths(
                artifact_paths,
                "dev_current",
                dev_current_evaluation.artifact_paths,
            )
        if dev_candidate_evaluation is not None:
            _merge_artifact_paths(
                artifact_paths,
                "dev_candidate",
                dev_candidate_evaluation.artifact_paths,
            )

        memory_stage_started_at = perf_counter()
        if progress_reporter is not None:
            progress_reporter.emit("memory_write_started", status="started", round=round_number)
        memory_record = build_evolution_record(
            round=round_number,
            candidate_config=candidate_config,
            diagnosis=diagnosis,
            safety_result=safety_result,
            train_score=train_score.aggregate_score,
            dev_score_before=(
                dev_score_before.aggregate_score if dev_score_before is not None else None
            ),
            dev_score_after=(
                dev_score_after.aggregate_score if dev_score_after is not None else None
            ),
            promotion_decision=decision,
            artifact_paths=artifact_paths,
            failure_taxonomy_before=failure_taxonomy_before,
            failure_taxonomy_after=failure_taxonomy_after,
            training_failure_taxonomy=diagnosis.get("failure_taxonomy", {}),
            evaluation_context=evaluation_context,
            cost=round_cost,
        )
        memory_record["promotion_decision"] = promotion_label
        written_memory = append_evolution_record(memory_path, memory_record)
        if progress_reporter is not None:
            progress_reporter.emit(
                "memory_write_completed",
                status="completed",
                round=round_number,
                duration_s=_elapsed_s(memory_stage_started_at),
                record_hash=written_memory["record_hash"],
            )

        if decision.accepted:
            promoted_count += 1
            current_config = candidate_config
            write_config(current_config, round_dir / "promoted_config.yaml")
        else:
            rejected_count += 1

        round_summary = {
            "round": round_number,
            "round_dir": str(round_dir),
            "candidate_id": written_memory["candidate_id"],
            "safety_status": written_memory["safety_status"],
            "promotion_decision": written_memory["promotion_decision"],
            "train_score": train_score.aggregate_score,
            "dev_score_before": written_memory["dev_score_before"],
            "dev_score_after": written_memory["dev_score_after"],
            "baseline_cache_hit": written_memory["baseline_cache_hit"],
            "candidate_cache_hit": written_memory["candidate_cache_hit"],
            "official_result_feedback": written_memory[
                "official_result_feedback"
            ],
            "failure_taxonomy_before": written_memory[
                "failure_taxonomy_before"
            ],
            "failure_taxonomy_after": written_memory["failure_taxonomy_after"],
            "current_config_promoted": bool(decision.accepted),
            "memory_record_hash": written_memory["record_hash"],
            "timing": timing,
        }
        round_summaries.append(round_summary)
        invocation_round_summaries.append(round_summary)
        _write_json(round_dir / "summary.json", round_summary)
        if progress_reporter is not None:
            progress_reporter.emit(
                "round_completed",
                status=promotion_label,
                round=round_number,
                duration_s=_elapsed_s(round_started_at),
                train_score=train_score.aggregate_score,
                dev_score_before=written_memory["dev_score_before"],
                dev_score_after=written_memory["dev_score_after"],
            )

    memory_summary = summarize_evolution_memory(read_evolution_memory(memory_path))
    write_config(current_config, root / "final_config.yaml")
    _write_json(root / "final_config.json", current_config)
    final_summary = {
        "rounds_requested": rounds,
        "rounds_completed": completed_rounds + len(invocation_round_summaries),
        "rounds_completed_this_invocation": len(invocation_round_summaries),
        "resume": bool(resume),
        "resumed_from_round": completed_rounds if resume else 0,
        "promoted_count": promoted_count,
        "rejected_count": rejected_count,
        "final_config_hash": config_fingerprint(current_config),
        "memory_path": str(memory_path),
        "round_summaries": round_summaries,
        "invocation_round_summaries": invocation_round_summaries,
        "memory_summary": memory_summary,
    }
    _write_json(root / "summary.json", final_summary)
    if progress_reporter is not None:
        progress_reporter.emit(
            "run_completed",
            status="completed",
            duration_s=_elapsed_s(run_started_at),
            rounds_completed=final_summary["rounds_completed"],
            promoted=promoted_count,
            rejected=rejected_count,
        )
    return final_summary


def _elapsed_s(started_at: float) -> float:
    return round(max(0.0, perf_counter() - started_at), 6)


def _load_prior_round_optimizer_feedback(
    rounds_root: Path,
    round_index: int,
) -> dict[str, Any]:
    if round_index <= 0:
        return {}
    prior_dir = rounds_root / f"round_{round_index - 1:03d}"
    feedback_path = prior_dir / "round_failure_feedback.json"
    optimizer_path = prior_dir / "prompt_update_optimizer_v2.json"
    if not feedback_path.is_file():
        return {}
    try:
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(feedback, dict):
        return {}

    rejected_proposals: list[dict[str, Any]] = []
    if optimizer_path.is_file():
        try:
            optimizer = json.loads(optimizer_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            optimizer = {}
        if isinstance(optimizer, dict):
            for proposal in optimizer.get("accepted_proposals", [])[:3]:
                if not isinstance(proposal, dict):
                    continue
                rejected_proposals.append(
                    {
                        "operation": str(proposal.get("operation") or ""),
                        "target_module": str(proposal.get("target_module") or ""),
                        "failure_pattern": str(proposal.get("failure_pattern") or ""),
                        "applicability_condition": str(
                            proposal.get("applicability_condition") or ""
                        )[:500],
                        "instruction_delta": str(
                            proposal.get("instruction_delta")
                            or proposal.get("replacement_delta")
                            or ""
                        )[:800],
                    }
                )

    return {
        "schema_version": "prompt_update_optimizer.prior_round_feedback.v1",
        "round_index": round_index - 1,
        "promotion": feedback.get("promotion", {}),
        "metric_delta": feedback.get("metric_delta", {}),
        "failure_delta": feedback.get("failure_delta", {}),
        "regressed_tasks": feedback.get("regressed_tasks", [])[:20],
        "changed_failure_shapes": feedback.get("changed_failure_shapes", [])[:20],
        "do_not_repeat": feedback.get("do_not_repeat", [])[:10],
        "rejected_proposals": rejected_proposals,
    }


def _load_resume_config(root: Path, completed_rounds: int) -> dict[str, Any]:
    final_config_json_path = root / "final_config.json"
    if final_config_json_path.is_file():
        current_config = json.loads(
            final_config_json_path.read_text(encoding="utf-8")
        )
        if not isinstance(current_config, dict):
            raise ValueError(
                "Cannot resume: persisted final config must be an object"
            )
    else:
        last_round_dir = root / "rounds" / f"round_{completed_rounds - 1:03d}"
        promotion_path = last_round_dir / "promotion_report.json"
        if not promotion_path.is_file():
            raise ValueError(
                "Cannot resume: previous promotion report is missing"
            )
        promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
        config_name = (
            "promoted_config.yaml"
            if bool(promotion.get("accepted"))
            else "current_config.yaml"
        )
        final_config_path = last_round_dir / config_name
        if not final_config_path.is_file():
            raise ValueError("Cannot resume: persisted current config is missing")

        current_config = load_config(final_config_path)
    summary_path = root / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        expected_hash = str(summary.get("final_config_hash") or "")
        if expected_hash and config_fingerprint(current_config) != expected_hash:
            raise ValueError(
                "Cannot resume: persisted config does not match summary hash"
            )
    return current_config


def _load_existing_round_summaries(
    rounds_root: Path,
    completed_rounds: int,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for round_index in range(completed_rounds):
        path = rounds_root / f"round_{round_index:03d}" / "summary.json"
        if not path.is_file():
            raise ValueError(
                f"Cannot resume: missing round summary for round {round_index}"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(
                f"Cannot resume: invalid round summary for round {round_index}"
            )
        summaries.append(payload)
    return summaries


def _dry_run_train_runner(
    config: dict[str, Any],
    tasks: list[Any],
    round_index: int,
) -> list[dict[str, Any]]:
    task_id = f"dry-run-train-{round_index}"
    return [
        {
            "task": {"id": task_id, "category": "software"},
            "success": False,
            "execution_status": "test_failed",
            "file_edits": [{"path": "pkg.py"}],
            "command_results": [
                {"phase": "test", "command": "pytest", "exit_code": 1}
            ],
            "intermediate_steps": [],
            "metrics": {"test_pass": 0.0},
            "cost": 0.0,
        }
    ]


def _dry_run_dev_evaluator(
    config: dict[str, Any],
    tasks: list[Any],
    round_index: int,
) -> EvaluationScore:
    return EvaluationScore(
        task_success_rate=0.0,
        patch_correctness=0.0,
        security_validity=0.0,
        test_pass_rate=0.0,
        cost_adjusted_performance=0.0,
        aggregate_score=0.0,
        task_count=len(tasks),
    )


def _normalize_train_evaluation(
    output: list[dict[str, Any]] | EvolutionEvaluationResult,
    tasks: list[Any],
    *,
    aggregate_metric: str,
) -> EvolutionEvaluationResult:
    if isinstance(output, EvolutionEvaluationResult):
        return output
    if not isinstance(output, list):
        raise TypeError(
            "train_runner must return records or EvolutionEvaluationResult"
        )
    return EvolutionEvaluationResult(
        score=score_records(output, aggregate_metric=aggregate_metric),
        records=output,
        task_ids=_task_ids(tasks),
        source="legacy_callback",
    )


def _normalize_dev_evaluation(
    output: EvaluationScore | EvolutionEvaluationResult,
    tasks: list[Any],
) -> EvolutionEvaluationResult:
    if isinstance(output, EvolutionEvaluationResult):
        return output
    if not isinstance(output, EvaluationScore):
        raise TypeError(
            "dev_evaluator must return EvaluationScore or "
            "EvolutionEvaluationResult"
        )
    return EvolutionEvaluationResult(
        score=output,
        task_ids=_task_ids(tasks),
        source="legacy_callback",
    )


def _promotion_score_samples(
    evaluation: EvolutionEvaluationResult,
) -> EvaluationScore | list[EvaluationScore]:
    raw = evaluation.metadata.get("repeat_scores")
    if not isinstance(raw, list) or not raw:
        return evaluation.score
    samples = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            samples.append(EvaluationScore(**item))
        except (TypeError, ValueError):
            return evaluation.score
    return samples or evaluation.score


def _load_reusable_v2_optimizer_result(
    path: Path,
    *,
    expected_messages: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot reuse incomplete optimizer artifact: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("Cannot reuse incomplete optimizer artifact: expected object")
    if result.get("optimizer_messages") != expected_messages:
        raise ValueError(
            "Cannot reuse incomplete optimizer artifact: optimizer input changed"
        )
    structured = result.get("structured_output") or {}
    if not bool(structured.get("schema_valid", False)):
        raise ValueError(
            "Cannot reuse incomplete optimizer artifact: output was not schema-valid"
        )
    if not bool(result.get("fixture_gate_passed", False)):
        raise ValueError(
            "Cannot reuse incomplete optimizer artifact: fixture gate did not pass"
        )
    if not isinstance(result.get("compiled_updates"), list):
        raise ValueError(
            "Cannot reuse incomplete optimizer artifact: compiled updates missing"
        )
    return result


def _taxonomy_comparison(
    *,
    training_diagnosis: dict[str, Any],
    current_evaluation: EvolutionEvaluationResult | None,
    candidate_evaluation: EvolutionEvaluationResult | None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if (
        current_evaluation is not None
        and candidate_evaluation is not None
        and current_evaluation.diagnosis
        and candidate_evaluation.diagnosis
    ):
        return (
            current_evaluation.diagnosis.get("failure_taxonomy", {}),
            candidate_evaluation.diagnosis.get("failure_taxonomy", {}),
            "dev_same_task_set",
        )
    return (
        {},
        training_diagnosis.get("failure_taxonomy", {}),
        "legacy_train_only",
    )


def _round_incurred_cost(
    *,
    diagnosis: dict[str, Any],
    train_evaluation: EvolutionEvaluationResult,
    current_evaluation: EvolutionEvaluationResult | None,
    candidate_evaluation: EvolutionEvaluationResult | None,
) -> dict[str, Any]:
    values: list[dict[str, Any]] = [_diagnosis_critic_cost(diagnosis)]
    for evaluation in (
        train_evaluation,
        current_evaluation,
        candidate_evaluation,
    ):
        if evaluation is not None and not evaluation.cache_hit:
            values.append(evaluation.cost)
    return merge_evaluation_costs(*values)


def _diagnosis_critic_cost(diagnosis: dict[str, Any]) -> dict[str, Any]:
    audit = diagnosis.get("aggregation_audit", {})
    if not isinstance(audit, dict):
        return default_evaluation_cost()
    prompt_tokens = _int_or_zero(audit.get("total_prompt_tokens"))
    completion_tokens = _int_or_zero(audit.get("total_completion_tokens"))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": _float_or_zero(audit.get("cost_usd")),
    }


def _merge_artifact_paths(
    destination: dict[str, str],
    prefix: str,
    source: dict[str, str],
) -> None:
    for key, value in sorted(source.items()):
        destination[f"{prefix}_{key}"] = str(value)


def _prompt_behavior_fixture_summary(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {
            "available": False,
            "all_passed": False,
            "proposal_count": 0,
            "passed_count": 0,
            "failed_count": 0,
        }
    return {
        "available": True,
        "all_passed": bool(report.get("all_passed", False)),
        "proposal_count": _int_or_zero(report.get("proposal_count")),
        "passed_count": _int_or_zero(report.get("passed_count")),
        "failed_count": _int_or_zero(report.get("failed_count")),
    }


def _task_ids(tasks: list[Any]) -> list[str]:
    ids: list[str] = []
    for task in tasks:
        task_id = getattr(task, "task_id", None)
        if task_id is None and isinstance(task, dict):
            task_id = task.get("task_id") or task.get("id")
        if task_id is not None:
            ids.append(str(task_id))
    return ids


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


def _write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
