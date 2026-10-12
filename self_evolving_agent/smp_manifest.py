"""Versioned SMP split, leakage, relation, and static-freeze validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from self_evolving_agent.smp_contracts import UtilityCertificate, canonical_hash


SPLITS = ("train-source", "dev-tune", "dev-cert", "test-static")
LEAKAGE_KEYS = (
    "task_id",
    "repository",
    "time_boundary",
    "mechanism_cluster",
    "patch_hash",
    "gold_derived_hash",
    "official_outcome_provenance",
)


@dataclass(frozen=True)
class SMPExperimentManifest:
    manifest_id: str
    experiment_type: str
    splits: Mapping[str, Sequence[Mapping[str, Any]]]
    source_target_relations: tuple[Mapping[str, Any], ...]
    frozen_components: Mapping[str, str]
    bootstrap_seed: int
    fixed_checkpoint_id: str
    certification_attempt_id: str
    router_version: str = "smp.router.v1"
    online_prequential_stream: Sequence[Mapping[str, Any]] = ()
    test_static_schedule: tuple[Mapping[str, Any], ...] = ()
    checkpoint_subject_ids: tuple[str, ...] = ()
    checkpoint_task_seeds: tuple[Mapping[str, Any], ...] = ()
    utility_checkpoint_hash: str = ""
    schema_version: str = "smp.experiment_manifest.v1"
    manifest_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "smp.experiment_manifest.v1" or not self.manifest_id:
            raise ValueError("invalid SMP experiment manifest identity")
        if self.experiment_type not in {"engineering_pilot", "static_paired", "online_prequential"}:
            raise ValueError("invalid SMP experiment type")
        if set(self.splits) != set(SPLITS):
            raise ValueError("manifest must declare all four SMP splits")
        if isinstance(self.bootstrap_seed, bool) or not isinstance(self.bootstrap_seed, int):
            raise ValueError("bootstrap_seed must be an integer")
        if not self.fixed_checkpoint_id or not self.certification_attempt_id or not self.router_version:
            raise ValueError("checkpoint and certification attempt IDs are required")
        required_frozen = {
            "model_hash",
            "model_parameters_hash",
            "prompt_hash",
            "solver_budget_hash",
            "router_hash",
            "bank_hash",
            "schema_hash",
            "code_hash",
            "evaluator_hash",
            "target_manifest_hash",
        }
        if set(self.frozen_components) != required_frozen or any(not _hash(value) for value in self.frozen_components.values()):
            raise ValueError("static frozen component hashes are incomplete")
        self.validate()
        if bool(self.checkpoint_subject_ids) != bool(self.checkpoint_task_seeds):
            raise ValueError("utility checkpoint requires both subjects and task-seed membership")
        if self.checkpoint_subject_ids:
            if len(set(self.checkpoint_subject_ids)) != len(self.checkpoint_subject_ids):
                raise ValueError("utility checkpoint subjects must be unique")
            legal = {
                (str(task["task_id"]), split)
                for split in ("dev-tune", "dev-cert")
                for task in self.splits[split]
            }
            trials: set[tuple[str, str, int, int]] = set()
            for row in self.checkpoint_task_seeds:
                pair = (str(row.get("task_id", "")), str(row.get("split", "")))
                seed = row.get("seed")
                subject = str(row.get("subject_id", ""))
                run_index = row.get("run_index")
                if (
                    pair not in legal
                    or subject not in self.checkpoint_subject_ids
                    or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (seed, run_index))
                ):
                    raise ValueError("utility checkpoint task-seed membership is invalid")
                trial = (subject, pair[0], seed, run_index)
                if trial in trials:
                    raise ValueError("utility checkpoint trials must be unique")
                trials.add(trial)
            if set(self.checkpoint_subject_ids) != {trial[0] for trial in trials}:
                raise ValueError("every checkpoint subject requires frozen trial membership")
            expected_checkpoint = canonical_hash(self.checkpoint_payload())
            if self.utility_checkpoint_hash and self.utility_checkpoint_hash != expected_checkpoint:
                raise ValueError("utility checkpoint hash mismatch")
            object.__setattr__(self, "utility_checkpoint_hash", expected_checkpoint)
        elif self.utility_checkpoint_hash:
            raise ValueError("empty utility checkpoint cannot carry a hash")
        self._validate_test_static_schedule()
        expected = canonical_hash(self.identity_payload())
        if self.manifest_hash and self.manifest_hash != expected:
            raise ValueError("SMP manifest_hash mismatch")
        object.__setattr__(self, "manifest_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "experiment_type": self.experiment_type,
            "splits": {name: list(self.splits[name]) for name in SPLITS},
            "source_target_relations": list(self.source_target_relations),
            "frozen_components": dict(self.frozen_components),
            "bootstrap_seed": self.bootstrap_seed,
            "fixed_checkpoint_id": self.fixed_checkpoint_id,
            "certification_attempt_id": self.certification_attempt_id,
            "router_version": self.router_version,
            "online_prequential_stream": list(self.online_prequential_stream),
            "test_static_schedule": list(self.test_static_schedule),
            "checkpoint_subject_ids": list(self.checkpoint_subject_ids),
            "checkpoint_task_seeds": list(self.checkpoint_task_seeds),
            "utility_checkpoint_hash": self.utility_checkpoint_hash,
        }

    def checkpoint_payload(self) -> dict[str, Any]:
        return {
            "fixed_checkpoint_id": self.fixed_checkpoint_id,
            "subjects": sorted(self.checkpoint_subject_ids),
            "task_seeds": sorted(
                (str(row["subject_id"]), str(row["task_id"]), int(row["seed"]), int(row["run_index"]), str(row["split"]))
                for row in self.checkpoint_task_seeds
            ),
            "components": {
                name: self.frozen_components[name]
                for name in ("router_hash", "bank_hash", "schema_hash", "code_hash", "evaluator_hash")
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "manifest_hash": self.manifest_hash}

    def validate(self) -> None:
        task_split: dict[str, str] = {}
        observed: dict[str, dict[str, str]] = {key: {} for key in LEAKAGE_KEYS}
        for split in SPLITS:
            for task in self.splits[split]:
                if not isinstance(task, Mapping) or not task.get("task_id"):
                    raise ValueError(f"malformed task in {split}")
                task_id = str(task["task_id"])
                if task_id in task_split:
                    raise ValueError("task ID overlaps SMP splits")
                task_split[task_id] = split
                for key in LEAKAGE_KEYS:
                    value = task.get(key)
                    if value in (None, ""):
                        continue
                    marker = str(value)
                    previous = observed[key].get(marker)
                    if previous is not None and previous != split:
                        raise ValueError(f"split leakage through {key}: {marker}")
                    observed[key][marker] = split
        for relation in self.source_target_relations:
            source = str(relation.get("source_task_id", ""))
            target = str(relation.get("target_task_id", ""))
            if source not in task_split or target not in task_split:
                raise ValueError("source-target relation references an unknown task")
            if task_split[source] == "test-static" or source == target:
                raise ValueError("test-static/current target cannot be a source")
            if relation.get("constructed_before_split") is True:
                raise ValueError("source-target relations must be constructed after split freeze")
        for task in self.online_prequential_stream:
            if not isinstance(task, Mapping) or not task.get("task_id"):
                raise ValueError("malformed online prequential task")
            if str(task["task_id"]) in task_split:
                raise ValueError("online prequential stream must remain separate from static splits")

    def _validate_test_static_schedule(self) -> None:
        targets = list(self.splits["test-static"])
        if bool(targets) != bool(self.test_static_schedule):
            raise ValueError("test-static targets require a frozen trial schedule")
        positions = {str(task["task_id"]): index for index, task in enumerate(targets)}
        observed: list[tuple[int, int, int, int]] = []
        identifiers: set[tuple[str, int, int]] = set()
        covered: set[str] = set()
        for row in self.test_static_schedule:
            if not isinstance(row, Mapping) or set(row) != {"task_id", "seed", "run_index", "task_index"}:
                raise ValueError("test-static schedule row schema is invalid")
            task_id = str(row["task_id"])
            values = (row["seed"], row["run_index"], row["task_index"])
            if task_id not in positions or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
                raise ValueError("test-static schedule row is invalid")
            trial = (task_id, int(row["seed"]), int(row["run_index"]))
            if trial in identifiers:
                raise ValueError("test-static schedule contains a duplicate task/seed/run trial")
            identifiers.add(trial)
            covered.add(task_id)
            observed.append((positions[task_id], int(row["seed"]), int(row["run_index"]), int(row["task_index"])))
        if covered != set(positions) or observed != sorted(observed):
            raise ValueError("test-static schedule must completely cover targets in deterministic task/seed/run order")

    def assert_certification_fresh(self, used_attempt_ids: set[str]) -> None:
        if self.certification_attempt_id in used_attempt_ids:
            raise ValueError("dev-cert attempt cannot be reused after retuning")


def validate_static_cohort_freeze(
    manifest: SMPExperimentManifest,
    *,
    observed_components: Mapping[str, str],
    initial_bank_hash: str,
    current_bank_hash: str,
    official_writeback_enabled: bool,
) -> None:
    if manifest.experiment_type not in {"engineering_pilot", "static_paired"}:
        raise ValueError("online prequential stream is not a static cohort")
    if dict(observed_components) != dict(manifest.frozen_components):
        raise ValueError("static cohort component freeze mismatch")
    if initial_bank_hash != current_bank_hash or official_writeback_enabled:
        raise ValueError("static cohort bank must remain frozen without writeback")


def expected_static_target_manifest_hash(manifest: SMPExperimentManifest) -> str:
    return canonical_hash({"split": "test-static", "tasks": list(manifest.splits["test-static"])})


def validate_static_target_manifest(manifest: SMPExperimentManifest) -> None:
    if manifest.frozen_components["target_manifest_hash"] != expected_static_target_manifest_hash(manifest):
        raise ValueError("test-static target manifest hash does not match frozen target rows")


def validate_static_entry_schedule(
    manifest: SMPExperimentManifest, entries: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    validate_static_target_manifest(manifest)
    expected = tuple(manifest.test_static_schedule)
    if len(entries) != len(expected):
        raise ValueError("test-static entries do not completely cover the frozen trial schedule")
    targets = {str(row["task_id"]): row for row in manifest.splits["test-static"]}
    observed: set[tuple[str, int, int]] = set()
    for entry, schedule in zip(entries, expected):
        task_id = str(schedule["task_id"])
        expected_task = {**targets[task_id], "run_index": int(schedule["run_index"])}
        task = entry.get("task")
        trial = (str(task.get("task_id", "")) if isinstance(task, Mapping) else "", int(entry.get("seed", -1)), int(task.get("run_index", -1)) if isinstance(task, Mapping) else -1)
        expected_trial = (task_id, int(schedule["seed"]), int(schedule["run_index"]))
        if trial in observed:
            raise ValueError("test-static entries contain a duplicate task/seed/run trial")
        observed.add(trial)
        if (
            trial != expected_trial
            or canonical_hash(task) != canonical_hash(expected_task)
            or int(entry.get("task_index", schedule["task_index"])) != int(schedule["task_index"])
        ):
            raise ValueError("test-static entries do not match the frozen ordered trial schedule")
    return expected


def validate_frozen_bank_payload(
    manifest: SMPExperimentManifest, bank: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(bank, Mapping):
        raise ValueError("frozen active bank must be an object")
    claimed_hash = bank.get("bank_hash")
    payload = {key: value for key, value in bank.items() if key != "bank_hash"}
    if set(payload) != {"schema_version", "bank_version", "memories"}:
        raise ValueError("frozen active bank schema is invalid")
    if payload["schema_version"] != "smp.frozen_bank.v1" or not isinstance(payload["memories"], list):
        raise ValueError("frozen active bank payload is invalid")
    if isinstance(payload["bank_version"], bool) or not isinstance(payload["bank_version"], int) or payload["bank_version"] < 0:
        raise ValueError("frozen active bank version is invalid")
    computed = canonical_hash(payload)
    if claimed_hash != computed or computed != manifest.frozen_components["bank_hash"]:
        raise ValueError("frozen active bank content does not match manifest bank_hash")
    memories = tuple(payload["memories"])
    if any(not isinstance(row, Mapping) for row in memories):
        raise ValueError("frozen active bank memory is malformed")
    identifiers = [str(row.get("memory_id", "")) for row in memories]
    if not all(identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("frozen active bank memory IDs are invalid")
    return memories


def expected_production_linkage(manifest: SMPExperimentManifest) -> dict[str, str]:
    return {
        "data_manifest_hash": manifest.manifest_hash,
        "code_hash": manifest.frozen_components["code_hash"],
        "bank_hash": manifest.frozen_components["bank_hash"],
        "router_hash": manifest.frozen_components["router_hash"],
        "schema_hash": manifest.frozen_components["schema_hash"],
        "evaluator_hash": manifest.frozen_components["evaluator_hash"],
    }


def production_certificate_compatible(
    certificate: UtilityCertificate,
    *,
    manifest: SMPExperimentManifest,
    subject_id: str,
) -> bool:
    return (
        certificate.compatible(
            subject_id=subject_id,
            schema_version="smp.memory.v1",
            router_version=manifest.router_version,
            test_mode=False,
        )
        and dict(certificate.production_linkage) == expected_production_linkage(manifest)
        and certificate.fixed_checkpoint_id == manifest.fixed_checkpoint_id
        and tuple(certificate.checkpoint_subject_ids) == tuple(manifest.checkpoint_subject_ids)
        and subject_id in manifest.checkpoint_subject_ids
    )


def _hash(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71
