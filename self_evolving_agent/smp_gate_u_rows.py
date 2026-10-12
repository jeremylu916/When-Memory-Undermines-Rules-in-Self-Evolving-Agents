"""Derive Gate U records only from sealed, officially linked paired artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from self_evolving_agent.smp_artifacts import (
    file_hash,
    load_validated_official_link,
    load_validated_seal,
)
from self_evolving_agent.smp_contracts import UtilityTrialSnapshot, canonical_hash
from self_evolving_agent.smp_experiment import ConditionPlan, assert_pair_parity
from self_evolving_agent.smp_io import load_identity, load_outcome, load_snapshot
from self_evolving_agent.smp_manifest import SMPExperimentManifest


def build_gate_u_rows(
    *,
    manifest: SMPExperimentManifest,
    paired_report_paths: Sequence[str | Path],
) -> list[dict[str, Any]]:
    if not manifest.utility_checkpoint_hash:
        raise ValueError("Gate U requires a frozen utility checkpoint in the manifest")
    arms_by_key: dict[tuple[str, int, int], dict[str, dict[str, Any]]] = {}
    for report_path in paired_report_paths:
        report = _json(report_path)
        report_hash = report.pop("report_hash", "")
        if report_hash != canonical_hash(report) or report.get("manifest_hash") != manifest.manifest_hash:
            raise ValueError("paired generation report hash/manifest linkage is invalid")
        for record in report.get("records", []):
            arm = load_official_arm(Path(record["root"]), manifest=manifest, required_split="dev-cert")
            key = (arm["identity"].task_id, arm["identity"].seed, arm["identity"].run_index)
            condition = arm["identity"].condition
            if condition in arms_by_key.setdefault(key, {}):
                raise ValueError("duplicate task-seed-run condition in Gate U artifacts")
            arms_by_key[key][condition] = arm

    rows: list[dict[str, Any]] = []
    observed_trials: set[tuple[str, str, int, int]] = set()
    for (task_id, seed, run_index), arms in sorted(arms_by_key.items()):
        if set(arms) != {"nomem", "smp_utility_trial"}:
            raise ValueError("Gate U requires exactly NoMem and authorized utility-trial arms")
        nomem, trial = arms["nomem"], arms["smp_utility_trial"]
        assert_pair_parity(nomem["plan"], trial["plan"])
        trial_snapshot = trial["snapshot"]
        if not isinstance(trial_snapshot, UtilityTrialSnapshot):
            raise ValueError("Gate U SMP arm lacks UtilityTrialSnapshot")
        authorization = trial_snapshot.authorization
        subject_id = authorization.source_memory_id
        checkpoint_trial = (subject_id, task_id, seed, run_index)
        if checkpoint_trial in observed_trials:
            raise ValueError("duplicate frozen checkpoint trial")
        observed_trials.add(checkpoint_trial)
        if (
            authorization.authorization_hash != trial["identity"].authorization_hash
            or authorization.checkpoint_hash != manifest.utility_checkpoint_hash
            or authorization.split != "dev-cert"
            or authorization.run_index != run_index
            or authorization.valid_through_run_index != run_index
        ):
            raise ValueError("utility authorization is not bound to the frozen Gate U run")
        trial_audit = trial["audit"]
        if not any(
            event.get("event_type") == "utility_trial_authorized"
            and event.get("authorization_hash") == authorization.authorization_hash
            for event in trial_audit
        ):
            raise ValueError("utility trial authorization was not audited")
        exposed = [event.get("memory_id") for event in trial_audit if event.get("event_type") == "exposure_attached_to_query"]
        if any(memory_id != subject_id for memory_id in exposed):
            raise ValueError("utility trial exposed memory outside its authorization")
        if any(event.get("event_type") in {"utility_trial_authorized", "exposure_attached_to_query"} for event in nomem["audit"]):
            raise ValueError("NoMem arm contains trial authorization or exposure")
        no_outcome, smp_outcome = nomem["outcome"], trial["outcome"]
        repository = str(trial["task_metadata"].get("repository", ""))
        mechanism = str(trial["task_metadata"].get("mechanism_cluster", ""))
        if not repository or not mechanism:
            raise ValueError("Gate U task metadata requires repository and mechanism_cluster strata")
        rows.append(
            {
                "subject_id": subject_id,
                "task_id": task_id,
                "seed": seed,
                "run_index": run_index,
                "split": "dev-cert",
                "repository": repository,
                "mechanism": mechanism,
                "nomem_resolved": no_outcome.resolved,
                "smp_resolved": smp_outcome.resolved,
                "nomem_security_regression": no_outcome.pass_to_pass["failed"] > 0,
                "smp_security_regression": smp_outcome.pass_to_pass["failed"] > 0,
                "nomem_cost": float(nomem["runtime"].get("total_cost", nomem["runtime"].get("solver_cost", 0.0))),
                "smp_cost": float(trial["runtime"].get("total_cost", trial["runtime"].get("solver_cost", 0.0))),
                "nomem_outcome_hash": no_outcome.outcome_hash,
                "smp_outcome_hash": smp_outcome.outcome_hash,
                "nomem_identity_hash": nomem["identity"].identity_hash,
                "smp_identity_hash": trial["identity"].identity_hash,
                "authorization_hash": authorization.authorization_hash,
                "authorized_exposure_count": len(exposed),
                "data_manifest_hash": manifest.manifest_hash,
                "code_hash": manifest.frozen_components["code_hash"],
                "bank_hash": manifest.frozen_components["bank_hash"],
                "router_hash": manifest.frozen_components["router_hash"],
                "schema_hash": manifest.frozen_components["schema_hash"],
                "evaluator_hash": manifest.frozen_components["evaluator_hash"],
                "checkpoint_hash": manifest.utility_checkpoint_hash,
            }
        )
    expected = {
        (str(row["subject_id"]), str(row["task_id"]), int(row["seed"]), int(row["run_index"]))
        for row in manifest.checkpoint_task_seeds
        if row["split"] == "dev-cert"
    }
    if observed_trials != expected:
        missing = sorted(expected - observed_trials)
        extra = sorted(observed_trials - expected)
        raise ValueError(f"Gate U artifacts do not equal frozen checkpoint; missing={missing}, extra={extra}")
    return rows


def load_official_arm(root: Path, *, manifest: SMPExperimentManifest, required_split: str) -> dict[str, Any]:
    identity = load_identity(root / "identity.json")
    if identity.manifest_hash != manifest.manifest_hash or identity.split != required_split:
        raise ValueError("official arm identity is outside the required frozen manifest split")
    seal = load_validated_seal(root, identity_hash=identity.identity_hash)
    invocation = _json(root / "evaluation_invocation.json")
    invocation_hash = invocation.pop("invocation_hash", "")
    if invocation.get("status") != "invoked_success" or invocation_hash != canonical_hash(invocation):
        raise ValueError("Gate U arm has no successful hash-bound evaluator invocation")
    if (
        invocation.get("identity_hash") != identity.identity_hash
        or invocation.get("evaluator_hash") != identity.evaluator_hash
        or invocation.get("prediction", {}).get("file_hash") != file_hash(root / "official_predictions.jsonl")
        or invocation.get("prediction", {}).get("prediction_hash") != seal.get("prediction_hash")
    ):
        raise ValueError("Gate U evaluator invocation does not bind the sealed prediction")
    outcome = load_outcome(root / "official_outcome.json")
    if not outcome.evaluation_terminal:
        raise ValueError("Gate U requires completed official outcomes for both arms")
    link = load_validated_official_link(
        root, identity_hash=identity.identity_hash, seal_hash=seal["seal_hash"]
    )
    if (
        link.get("identity_hash") != identity.identity_hash
        or link.get("seal_hash") != seal.get("seal_hash")
        or link.get("outcome_hash") != outcome.outcome_hash
        or link.get("report_hash") != outcome.report_hash
        or link.get("evaluator_hash") != identity.evaluator_hash
        or link.get("invocation_hash") != invocation_hash
    ):
        raise ValueError("Gate U official outcome link is invalid")
    runtime = _json(root / "runtime_state.json")
    if runtime.get("prediction_hash") != outcome.prediction_hash or runtime.get("trajectory_hash") != outcome.trajectory_hash:
        raise ValueError("Gate U official outcome does not bind sealed runtime artifacts")
    plan_row = _json(root / "plan.json")
    plan = ConditionPlan(
        task_id=str(plan_row["task_id"]),
        condition=str(plan_row["condition"]),
        seed=int(plan_row["seed"]),
        run_index=int(plan_row["run_index"]),
        parity=plan_row["parity"],
        treatment=plan_row["treatment"],
        plan_hash=str(plan_row["plan_hash"]),
    )
    snapshot = load_snapshot(root / "snapshot.json")
    audit = _json(root / "smp_audit.json")
    task_metadata = next(
        (row for row in manifest.splits[required_split] if str(row["task_id"]) == identity.task_id),
        {},
    )
    return {"identity": identity, "seal": seal, "runtime": runtime, "plan": plan, "snapshot": snapshot, "audit": audit, "outcome": outcome, "task_metadata": task_metadata}


def _json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
