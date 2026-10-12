"""Production SMP condition construction, parity, paired orchestration, and resume."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from self_evolving_agent.smp_artifacts import (
    SMPArtifactStore,
    TaskRunIdentity,
    atomic_write_json,
    file_hash,
    load_validated_official_link,
    load_validated_seal,
)
from self_evolving_agent.smp_calibration import (
    GATE_F_FEATURES,
    GATE_R_FEATURES,
    LiteLLMRequestTokenCounter,
    LiteLLMTokenCounter,
    StructuredSignatureExtractor,
    production_gate_f_scorer,
    production_gate_r_scorer,
)
from self_evolving_agent.smp_structured import DEEPSEEK_DEPLOYMENT, OpenAIChatCompletionsProvider, SIGNATURE_SCHEMA, StrictStructuredClient
from self_evolving_agent.smp_contracts import (
    BankSnapshot,
    FrozenCalibrationProfile,
    GateMode,
    MemoryExposure,
    MemoryState,
    MechanismSignature,
    NarrowScope,
    SIGNATURE_FIELDS,
    SignatureField,
    SMPMemoryItem,
    UtilityTrialAuthorization,
    UtilityTrialSnapshot,
    canonical_hash,
    seal_bank_snapshot,
)
from self_evolving_agent.smp_io import memory_from_dict
from self_evolving_agent.smp_manifest import (
    SMPExperimentManifest,
    validate_frozen_bank_payload,
    validate_static_entry_schedule,
    validate_static_cohort_freeze,
)
from self_evolving_agent.smp_runtime import SMPBudgets, SMPRuntime
from self_evolving_agent.smp_trace_adapter import MiniTraceAdapter, sanitize_persistent_payload


CONDITIONS = {"nomem", "smp_utility_trial", "smp_active"}
PARITY_FIELDS = (
    "task_hash",
    "model_hash",
    "model_parameters_hash",
    "prompt_hash",
    "solver_budget_hash",
    "environment_hash",
    "tools_hash",
    "evaluator_hash",
)


@dataclass(frozen=True)
class ConditionPlan:
    task_id: str
    condition: str
    seed: int
    run_index: int
    parity: Mapping[str, str]
    treatment: Mapping[str, Any]
    plan_hash: str = ""

    def __post_init__(self) -> None:
        if self.condition not in CONDITIONS or set(self.parity) != set(PARITY_FIELDS):
            raise ValueError("invalid SMP condition plan")
        expected = canonical_hash(self.identity_payload())
        if self.plan_hash and self.plan_hash != expected:
            raise ValueError("condition plan_hash mismatch")
        object.__setattr__(self, "plan_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "condition": self.condition,
            "seed": self.seed,
            "run_index": self.run_index,
            "parity": dict(self.parity),
            "treatment": dict(self.treatment),
        }


def assert_pair_parity(left: ConditionPlan, right: ConditionPlan) -> None:
    if left.task_id != right.task_id or left.seed != right.seed or left.run_index != right.run_index:
        raise ValueError("paired arms must use the same target and seed")
    differences = [field for field in PARITY_FIELDS if left.parity[field] != right.parity[field]]
    if differences:
        raise ValueError("paired parity mismatch: " + ", ".join(differences))


class SMPProductionRunner:
    """Construct and execute one condition; external stages remain explicit."""

    def __init__(
        self,
        *,
        manifest: SMPExperimentManifest,
        output_root: str | Path,
        mini_config_paths: Sequence[str | Path],
        extractor_prompt: str,
        domain: str = "swe",
        model_factory: Callable[[Mapping[str, Any]], Any] | None = None,
        environment_factory: Callable[[Mapping[str, Any]], Any] | None = None,
        structured_provider_factory: Callable[[], Callable[[Mapping[str, Any]], Any]] | None = None,
        structured_response_format_mode: str = "json_schema",
        frozen_controller_pricing: Mapping[str, float] | None = None,
        structured_max_completion_tokens: int = 3000,
        frozen_solver_pricing: Mapping[str, float] | None = None,
        solver_max_completion_tokens: int | None = None,
    ):
        self.manifest = manifest
        self.output_root = Path(output_root)
        self.mini_config_paths = tuple(Path(path) for path in mini_config_paths)
        self.extractor_prompt = extractor_prompt
        self.domain = domain
        self.model_factory = model_factory
        self.environment_factory = environment_factory
        self.structured_provider_factory = structured_provider_factory
        self.structured_response_format_mode = structured_response_format_mode
        self.frozen_controller_pricing = dict(frozen_controller_pricing) if frozen_controller_pricing is not None else None
        self.structured_max_completion_tokens = structured_max_completion_tokens
        self.frozen_solver_pricing = dict(frozen_solver_pricing) if frozen_solver_pricing is not None else None
        self.solver_max_completion_tokens = solver_max_completion_tokens
        if not self.mini_config_paths or any(not path.is_file() for path in self.mini_config_paths):
            raise ValueError("all frozen mini-SWE config paths must exist")

    def load_config(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for path in self.mini_config_paths:
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            merged = _deep_merge(merged, value)
        return merged

    def build_plan(
        self,
        *,
        task: Mapping[str, Any],
        condition: str,
        seed: int,
        snapshot: BankSnapshot | UtilityTrialSnapshot,
        profile: FrozenCalibrationProfile | None,
    ) -> ConditionPlan:
        config = self.load_config()
        model_config = dict(config.get("model", {}))
        agent_config = dict(config.get("agent", {}))
        environment_config = _deep_merge(dict(config.get("environment", {})), dict(task.get("environment", {})))
        parity = {
            "task_hash": canonical_hash(task),
            "model_hash": canonical_hash({"model_class": model_config.get("model_class"), "model_name": model_config.get("model_name")}),
            "model_parameters_hash": canonical_hash(model_config.get("model_kwargs", {})),
            "prompt_hash": canonical_hash({"system": agent_config.get("system_template"), "instance": agent_config.get("instance_template")}),
            "solver_budget_hash": canonical_hash({key: agent_config.get(key) for key in ("step_limit", "cost_limit", "wall_time_limit_seconds")}),
            "environment_hash": canonical_hash(environment_config),
            "tools_hash": canonical_hash({"environment_class": environment_config.get("environment_class")}),
            "evaluator_hash": self.manifest.frozen_components["evaluator_hash"],
        }
        treatment = {
            "snapshot_hash": snapshot.snapshot_hash,
            "profile_hash": profile.artifact_hash if profile else canonical_hash({"profile": "none"}),
            "authorization_hash": (
                snapshot.authorization.authorization_hash if isinstance(snapshot, UtilityTrialSnapshot) else None
            ),
        }
        run_index = task.get("run_index")
        if isinstance(run_index, bool) or not isinstance(run_index, int) or run_index < 0:
            raise ValueError("task requires a fixed non-negative run_index")
        return ConditionPlan(
            task_id=str(task["task_id"]),
            condition=condition,
            seed=seed,
            run_index=run_index,
            parity=parity,
            treatment=treatment,
        )

    def make_identity(
        self,
        *,
        run_id: str,
        split: str,
        plan: ConditionPlan,
        profile: FrozenCalibrationProfile | None,
    ) -> TaskRunIdentity:
        return TaskRunIdentity(
            run_id=run_id,
            task_id=plan.task_id,
            split=split,
            condition=plan.condition,
            seed=plan.seed,
            run_index=plan.run_index,
            manifest_hash=self.manifest.manifest_hash,
            model_hash=plan.parity["model_hash"],
            config_hash=canonical_hash(self.load_config()),
            prompt_hash=plan.parity["prompt_hash"],
            budget_hash=plan.parity["solver_budget_hash"],
            router_hash=self.manifest.frozen_components["router_hash"],
            bank_hash=self.manifest.frozen_components["bank_hash"],
            profile_hash=profile.artifact_hash if profile else canonical_hash({"profile": "none"}),
            schema_hash=self.manifest.frozen_components["schema_hash"],
            code_hash=self.manifest.frozen_components["code_hash"],
            evaluator_hash=plan.parity["evaluator_hash"],
            authorization_hash=(
                str(plan.treatment["authorization_hash"])
                if plan.treatment["authorization_hash"] is not None
                else canonical_hash({"authorization": "none"})
            ),
        )

    def construct(
        self,
        *,
        task: Mapping[str, Any],
        condition: str,
        seed: int,
        snapshot: BankSnapshot | UtilityTrialSnapshot,
        profile: FrozenCalibrationProfile | None,
        test_mode: bool = False,
        usage_persist: Callable[[Sequence[Any]], None] | None = None,
    ) -> tuple[Any, SMPRuntime, ConditionPlan, dict[str, Any]]:
        if condition not in CONDITIONS:
            raise ValueError("unsupported SMP condition")
        if condition == "nomem" and snapshot.memories:
            raise ValueError("NoMem condition requires an empty production-shaped snapshot")
        if condition == "smp_utility_trial" and not isinstance(snapshot, UtilityTrialSnapshot):
            raise ValueError("utility-trial condition requires UtilityTrialSnapshot")
        if condition == "smp_active" and not isinstance(snapshot, BankSnapshot):
            raise ValueError("active condition requires production BankSnapshot")
        config = self.load_config()
        model_config = dict(config.get("model", {}))
        environment_config = _deep_merge(dict(config.get("environment", {})), dict(task.get("environment", {})))
        model = self.model_factory(model_config) if self.model_factory else self._get_model(model_config)
        environment = self.environment_factory(environment_config) if self.environment_factory else self._get_environment(environment_config)
        token_counter = LiteLLMTokenCounter(str(model_config.get("model_name", "")))
        request_token_counter = LiteLLMRequestTokenCounter(str(model_config.get("model_name", "")))
        plan = self.build_plan(task=task, condition=condition, seed=seed, snapshot=snapshot, profile=profile)
        if condition == "nomem":
            runtime = SMPRuntime(task_id=str(task["task_id"]), snapshot=snapshot, domain=self.domain)
        else:
            if profile is None:
                raise ValueError("SMP exposure requires a frozen calibration profile")
            pricing = self.frozen_controller_pricing if self.frozen_controller_pricing is not None else config.get("controller_pricing")
            if not isinstance(pricing, Mapping):
                raise ValueError("frozen controller_pricing is required")
            provider = self.structured_provider_factory() if self.structured_provider_factory else OpenAIChatCompletionsProvider()
            client = StrictStructuredClient(
                provider=provider,
                model=DEEPSEEK_DEPLOYMENT,
                model_config={},
                prompt=self.extractor_prompt,
                schema_name="openmemgov_signature_v4",
                schema=SIGNATURE_SCHEMA,
                pricing_contract=pricing,
                response_format_mode=self.structured_response_format_mode,
                max_completion_tokens=self.structured_max_completion_tokens,
            )
            extractor = StructuredSignatureExtractor(
                client=client,
            )
            self._validate_profile_binding(profile, extractor)
            runtime = SMPRuntime(
                task_id=str(task["task_id"]),
                snapshot=snapshot,
                domain=self.domain,
                gate_f_mode=GateMode.ENFORCE,
                gate_r_mode=GateMode.ENFORCE,
                calibration_profile=profile,
                signature_scorer_version="smp.gate_f.v1",
                retrieval_scorer_version="smp.gate_r.v1",
                extractor=extractor,
                gate_f_scorer=production_gate_f_scorer,
                gate_r_scorer=production_gate_r_scorer,
                token_counter=token_counter,
                test_mode=test_mode,
                budgets=SMPBudgets(),
            )
            runtime.controller_structured_client = client
        if condition == "nomem":
            runtime.controller_structured_client = None
        from self_evolving_agent.smp_mini_agent import SMPMiniAgent

        agent = SMPMiniAgent(
            model=model,
            env=environment,
            runtime=runtime,
            post_action_adapter=MiniTraceAdapter(),
            accounting_token_counter=token_counter,
            request_token_counter=request_token_counter,
            solver_pricing=self.frozen_solver_pricing,
            solver_max_completion_tokens=self.solver_max_completion_tokens,
            solver_cost_limit=config.get("agent", {}).get("cost_limit"),
            usage_persist=usage_persist,
            **{key: value for key, value in dict(config.get("agent", {})).items() if key != "agent_class"},
        )
        return agent, runtime, plan, config

    def solve_and_seal(
        self,
        *,
        task: Mapping[str, Any],
        condition: str,
        seed: int,
        snapshot: BankSnapshot | UtilityTrialSnapshot,
        profile: FrozenCalibrationProfile | None,
        identity: TaskRunIdentity,
        test_mode: bool = False,
    ) -> dict[str, Any]:
        store = SMPArtifactStore(self.output_root, identity)
        plan = self.build_plan(task=task, condition=condition, seed=seed, snapshot=snapshot, profile=profile)
        self._validate_run_identity(identity=identity, plan=plan, snapshot=snapshot, profile=profile, task=task)
        if store.official_link_path.exists():
            seal = load_validated_seal(store.root, identity_hash=identity.identity_hash)
            load_validated_official_link(
                store.root, identity_hash=identity.identity_hash, seal_hash=seal["seal_hash"]
            )
            persisted = json.loads((store.root / "runtime_state.json").read_text(encoding="utf-8"))
            return {"status": "official_finalized", "root": str(store.root), "new_model_calls": 0, **persisted}
        if store.is_resumable():
            persisted = json.loads((store.root / "runtime_state.json").read_text(encoding="utf-8"))
            return {"status": "resumed", "root": str(store.root), "new_model_calls": 0, **persisted}
        usage_path = store.root / "solver_usage.json"

        def persist_usage(rows: Sequence[Any]) -> None:
            payload = {
                "schema_version": "openmemgov.solver_usage_journal.v1",
                "identity_hash": identity.identity_hash,
                "usage": [asdict(row) for row in rows],
            }
            payload["journal_hash"] = canonical_hash(payload)
            atomic_write_json(usage_path, payload)

        agent, runtime, plan, _config = self.construct(
            task=task,
            condition=condition,
            seed=seed,
            snapshot=snapshot,
            profile=profile,
            test_mode=test_mode,
            usage_persist=persist_usage,
        )
        try:
            result = agent.run(str(task["problem_statement"]))
        except Exception as exc:
            accounting = agent.smp_accounting()
            uncertain = bool(getattr(exc, "details", {}).get("uncertain_paid_response", False))
            reconciled_cost = (
                float((self.frozen_solver_pricing or {})["maximum_task_cost"])
                if uncertain else float(accounting["cost"])
            )
            abort = {
                "schema_version": "openmemgov.solver_abort.v1",
                "status": "aborted",
                "identity_hash": identity.identity_hash,
                "task_id": str(task["task_id"]),
                "error_code": str(getattr(exc, "error_code", type(exc).__name__)),
                "solver_accounting": accounting,
                "completed_calls": int(accounting["calls"]),
                "uncertain_paid_response": uncertain,
                "reconciled_cost_usd": reconciled_cost,
                "usage_journal_hash": file_hash(usage_path) if usage_path.is_file() else "",
            }
            abort["abort_hash"] = canonical_hash(abort)
            atomic_write_json(store.root / "abort.json", abort)
            raise
        trajectory = sanitize_persistent_payload(agent.serialize())
        prediction = {
            "instance_id": task["task_id"],
            "model_name_or_path": "smp",
            "model_patch": str(result.get("submission", "")),
        }
        runtime.seal_prediction(prediction)
        runtime.seal_trajectory(trajectory)
        controller = getattr(runtime, "controller_structured_client", None)
        solver_accounting = agent.smp_accounting()
        controller_accounting = {
            "calls": controller.calls if controller else 0,
            "cost": controller.cumulative_cost if controller else 0.0,
            "input_tokens": controller.input_tokens if controller else 0,
            "output_tokens": controller.output_tokens if controller else 0,
        }
        state = {
            "solver_done": runtime.state.solver_done,
            "prediction_sealed": runtime.state.prediction_sealed,
            "trajectory_sealed": runtime.state.trajectory_sealed,
            "prediction_hash": runtime.state.prediction_hash,
            "trajectory_hash": runtime.state.trajectory_hash,
            "snapshot_hash": runtime.state.snapshot.snapshot_hash,
            "authorization_hash": identity.authorization_hash,
            "task_id": runtime.state.task_id,
            "task_index": runtime.state.snapshot.task_index,
            "bank_version": runtime.state.snapshot.bank_version,
            "condition": condition,
            "plan_hash": plan.plan_hash,
            "plan": {**plan.identity_payload(), "plan_hash": plan.plan_hash},
            "model_calls": agent.n_calls,
            "solver_cost": agent.cost,
            "solver_accounting": solver_accounting,
            "controller_accounting": controller_accounting,
            "total_calls": int(solver_accounting["calls"]) + int(controller_accounting["calls"]),
            "total_cost": float(agent.cost) + float(controller_accounting["cost"]),
            "total_input_tokens": int(solver_accounting["input_tokens"]) + int(controller_accounting["input_tokens"]),
            "total_output_tokens": int(solver_accounting["output_tokens"]) + int(controller_accounting["output_tokens"]),
            "memory_visible_tokens": runtime.state.visible_memory_tokens_used,
        }
        seal = store.save_sealed(
            trajectory=trajectory,
            prediction=prediction,
            audit=runtime.audit_sidecar(),
            evidence=[event.to_dict() for event in runtime.state.evidence],
            runtime_state=state,
            snapshot=snapshot.to_dict(),
            plan={**plan.identity_payload(), "plan_hash": plan.plan_hash},
        )
        return {"status": "sealed", "root": str(store.root), "new_model_calls": state["total_calls"], "seal": seal, **state}

    def _validate_profile_binding(
        self,
        profile: FrozenCalibrationProfile,
        extractor: StructuredSignatureExtractor,
    ) -> None:
        bindings = profile.production_bindings
        expected = {
            "extractor_model_hash": extractor.binding.model_hash,
            "extractor_config_hash": extractor.binding.config_hash,
            "extractor_prompt_hash": extractor.binding.prompt_hash,
            "signature_schema_hash": extractor.binding.schema_hash,
            "gate_f_feature_hash": canonical_hash(GATE_F_FEATURES),
            "gate_r_feature_hash": canonical_hash(GATE_R_FEATURES),
            "gate_r_weights_hash": canonical_hash(profile.certification_metadata.get("gate_r_weights", ())),
        }
        if any(bindings.get(name) != value for name, value in expected.items()):
            raise ValueError("calibration profile does not bind the production extractor/scorers")

    def _validate_run_identity(
        self,
        *,
        identity: TaskRunIdentity,
        plan: ConditionPlan,
        snapshot: BankSnapshot | UtilityTrialSnapshot,
        profile: FrozenCalibrationProfile | None,
        task: Mapping[str, Any],
    ) -> None:
        config = self.load_config()
        frozen_parity = {
            "model_hash": "model_hash",
            "model_parameters_hash": "model_parameters_hash",
            "prompt_hash": "prompt_hash",
            "solver_budget_hash": "solver_budget_hash",
            "evaluator_hash": "evaluator_hash",
        }
        parity_mismatches = [
            field
            for field, frozen_name in frozen_parity.items()
            if plan.parity[field] != self.manifest.frozen_components[frozen_name]
        ]
        if parity_mismatches:
            raise ValueError("run configuration violates frozen manifest: " + ", ".join(parity_mismatches))
        expected_profile_hash = profile.artifact_hash if profile else canonical_hash({"profile": "none"})
        expected = {
            "task_id": plan.task_id,
            "condition": plan.condition,
            "seed": plan.seed,
            "run_index": plan.run_index,
            "manifest_hash": self.manifest.manifest_hash,
            "model_hash": plan.parity["model_hash"],
            "config_hash": canonical_hash(config),
            "prompt_hash": plan.parity["prompt_hash"],
            "budget_hash": plan.parity["solver_budget_hash"],
            "router_hash": self.manifest.frozen_components["router_hash"],
            "bank_hash": self.manifest.frozen_components["bank_hash"],
            "profile_hash": expected_profile_hash,
            "schema_hash": self.manifest.frozen_components["schema_hash"],
            "code_hash": self.manifest.frozen_components["code_hash"],
            "evaluator_hash": plan.parity["evaluator_hash"],
            "authorization_hash": (
                str(plan.treatment["authorization_hash"])
                if plan.treatment["authorization_hash"] is not None
                else canonical_hash({"authorization": "none"})
            ),
        }
        mismatches = [name for name, value in expected.items() if getattr(identity, name) != value]
        if identity.split != str(task.get("split", identity.split)):
            mismatches.append("split")
        if mismatches:
            raise ValueError("task run identity mismatch: " + ", ".join(sorted(set(mismatches))))
        if isinstance(snapshot, UtilityTrialSnapshot):
            authorization = snapshot.authorization
            trial_expected = {
                "experiment_id": self.manifest.manifest_id,
                "run_id": identity.run_id,
                "manifest_hash": self.manifest.manifest_hash,
                "router_hash": identity.router_hash,
                "profile_hash": identity.profile_hash,
                "schema_hash": identity.schema_hash,
                "condition": identity.condition,
                "seed": identity.seed,
                "run_index": identity.run_index,
                "bank_version": snapshot.bank_version,
                "split": identity.split,
                "checkpoint_hash": self.manifest.utility_checkpoint_hash,
            }
            if any(getattr(authorization, name) != value for name, value in trial_expected.items()):
                raise ValueError("utility trial authorization does not bind this exact run")
            if authorization.run_index != int(task["run_index"]) or authorization.valid_through_run_index != authorization.run_index:
                raise ValueError("utility trial authorization run boundary mismatch")

    @staticmethod
    def _get_model(config: Mapping[str, Any]) -> Any:
        from minisweagent.models import get_model

        return get_model(config=dict(config))

    @staticmethod
    def _get_environment(config: Mapping[str, Any]) -> Any:
        from minisweagent.environments import get_environment

        return get_environment(dict(config), default_type="docker")


class PairedExperimentRunner:
    def __init__(self, *, manifest: SMPExperimentManifest, production_runner: SMPProductionRunner):
        self.manifest = manifest
        self.runner = production_runner
        self._initial_bank_hash = manifest.frozen_components["bank_hash"]

    def validate_pair(self, plans: Sequence[ConditionPlan]) -> None:
        if len(plans) != 2:
            raise ValueError("a paired comparison requires exactly two arms")
        assert_pair_parity(plans[0], plans[1])

    def assert_cohort_frozen(self, *, observed_components: Mapping[str, str], current_bank_hash: str) -> None:
        validate_static_cohort_freeze(
            self.manifest,
            observed_components=observed_components,
            initial_bank_hash=self._initial_bank_hash,
            current_bank_hash=current_bank_hash,
            official_writeback_enabled=False,
        )

    def run_cohort(
        self,
        *,
        run_id: str,
        split: str,
        entries: Sequence[Mapping[str, Any]],
        current_bank_hash: str,
        frozen_bank: Mapping[str, Any] | None = None,
        test_mode: bool = False,
    ) -> dict[str, Any]:
        if split not in {"dev-tune", "dev-cert", "test-static"}:
            raise ValueError("paired cohort split must be prospective dev or test-static")
        frozen_memories: tuple[SMPMemoryItem, ...] = ()
        if split == "test-static":
            if frozen_bank is None:
                raise ValueError("test-static paired execution requires the frozen active bank artifact")
            frozen_rows = validate_frozen_bank_payload(self.manifest, frozen_bank)
            frozen_memories = tuple(memory_from_dict(row) for row in frozen_rows)
            if current_bank_hash != frozen_bank["bank_hash"]:
                raise ValueError("test-static current bank hash is not derived from the frozen bank artifact")
            static_schedule = validate_static_entry_schedule(self.manifest, entries)
        records: list[dict[str, Any]] = []
        for entry_index, entry in enumerate(entries):
            self.assert_cohort_frozen(
                observed_components=self.manifest.frozen_components,
                current_bank_hash=current_bank_hash,
            )
            task = entry["task"]
            seed = int(entry["seed"])
            profile = entry.get("profile")
            treatment = str(entry["treatment_condition"])
            if split == "test-static" and treatment != "smp_active":
                raise ValueError("test-static paired treatment must be smp_active")
            if split != "test-static" and treatment != "smp_utility_trial":
                raise ValueError("dev paired treatment must be smp_utility_trial")
            if split == "test-static":
                nomem_snapshot = entry["nomem_snapshot"]
                treatment_snapshot = entry["treatment_snapshot"]
                if not isinstance(nomem_snapshot, BankSnapshot) or isinstance(nomem_snapshot, UtilityTrialSnapshot):
                    raise ValueError("test-static NoMem snapshot is invalid")
                if not isinstance(treatment_snapshot, BankSnapshot) or isinstance(treatment_snapshot, UtilityTrialSnapshot):
                    raise ValueError("test-static treatment snapshot is invalid")
                expected_memories = tuple(
                    memory
                    for memory in frozen_memories
                    if memory.source_task_id != str(task["task_id"])
                    and memory.available_from_task_index <= treatment_snapshot.task_index
                )
                if (
                    nomem_snapshot.task_id != str(task["task_id"])
                    or treatment_snapshot.task_id != str(task["task_id"])
                    or treatment_snapshot.task_index != int(static_schedule[entry_index]["task_index"])
                    or nomem_snapshot.task_index != treatment_snapshot.task_index
                    or nomem_snapshot.bank_version != int(frozen_bank["bank_version"])
                    or treatment_snapshot.bank_version != int(frozen_bank["bank_version"])
                    or nomem_snapshot.bank_item_count != len(frozen_memories)
                    or treatment_snapshot.bank_item_count != len(frozen_memories)
                    or nomem_snapshot.memories
                    or tuple(memory.to_dict() for memory in treatment_snapshot.memories)
                    != tuple(memory.to_dict() for memory in expected_memories)
                ):
                    raise ValueError("test-static snapshots do not exactly derive from the frozen active bank")
            arms = (
                ("nomem", entry["nomem_snapshot"], None),
                (treatment, entry["treatment_snapshot"], profile),
            )
            plans = [
                self.runner.build_plan(task=task, condition=condition, seed=seed, snapshot=snapshot, profile=arm_profile)
                for condition, snapshot, arm_profile in arms
            ]
            self.validate_pair(plans)
            for (condition, snapshot, arm_profile), plan in zip(arms, plans):
                identity = self.runner.make_identity(run_id=run_id, split=split, plan=plan, profile=arm_profile)
                result = self.runner.solve_and_seal(
                    task=task,
                    condition=condition,
                    seed=seed,
                    snapshot=snapshot,
                    profile=arm_profile,
                    identity=identity,
                    test_mode=test_mode,
                )
                records.append(
                    {
                        "task_id": plan.task_id,
                        "seed": seed,
                        "condition": condition,
                        "plan_hash": plan.plan_hash,
                        **result,
                    }
                )
        report = {
            "schema_version": "smp.paired_generation.v1",
            "run_id": run_id,
            "manifest_hash": self.manifest.manifest_hash,
            "split": split,
            "records": records,
            "model_calls": sum(int(record.get("model_calls", 0)) for record in records),
            "solver_cost": sum(float(record.get("solver_cost", 0.0)) for record in records),
            "total_cost": sum(float(record.get("total_cost", 0.0)) for record in records),
            "total_input_tokens": sum(int(record.get("total_input_tokens", 0)) for record in records),
            "total_output_tokens": sum(int(record.get("total_output_tokens", 0)) for record in records),
            "memory_visible_tokens": sum(int(record.get("memory_visible_tokens", 0)) for record in records),
        }
        report["report_hash"] = canonical_hash(report)
        return report

    @staticmethod
    def aggregate_pairs(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        pairs: dict[tuple[str, int], dict[str, Mapping[str, Any]]] = {}
        for outcome in outcomes:
            key = (str(outcome["task_id"]), int(outcome["seed"]))
            pairs.setdefault(key, {})[str(outcome["condition"])] = outcome
        for key, arms in pairs.items():
            if "nomem" not in arms or not ({"smp_utility_trial", "smp_active"} & set(arms)):
                raise ValueError(f"incomplete official pair: {key}")
        report = {
            "schema_version": "smp.paired_report.v1",
            "pairs": [
                {"task_id": task_id, "seed": seed, "arms": arms}
                for (task_id, seed), arms in sorted(pairs.items())
            ],
        }
        report["report_hash"] = canonical_hash(report)
        return report


def _deep_merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
