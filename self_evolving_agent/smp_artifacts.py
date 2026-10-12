"""Atomic, hash-linked SMP task-condition artifacts and safe resume."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from self_evolving_agent.smp_contracts import canonical_hash, canonical_json
from self_evolving_agent.smp_trace_adapter import sanitize_persistent_payload


@dataclass(frozen=True)
class TaskRunIdentity:
    run_id: str
    task_id: str
    split: str
    condition: str
    seed: int
    run_index: int
    manifest_hash: str
    model_hash: str
    config_hash: str
    prompt_hash: str
    budget_hash: str
    router_hash: str
    bank_hash: str
    profile_hash: str
    schema_hash: str
    code_hash: str
    evaluator_hash: str
    authorization_hash: str
    schema_version: str = "smp.task_run_identity.v1"

    def __post_init__(self) -> None:
        if self.condition not in {"nomem", "smp_utility_trial", "smp_active"}:
            raise ValueError("unsupported SMP condition")
        if (
            not self.run_id
            or not self.task_id
            or Path(self.run_id).name != self.run_id
            or Path(self.task_id).name != self.task_id
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.seed, self.run_index))
        ):
            raise ValueError("invalid task run identity")
        for name, value in asdict(self).items():
            if name.endswith("_hash") and (not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71):
                raise ValueError(f"invalid identity hash: {name}")

    @property
    def identity_hash(self) -> str:
        return canonical_hash(asdict(self))


def file_hash(path: str | Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def load_validated_seal(
    root_or_path: str | Path,
    *,
    identity_hash: str | None = None,
    validate_artifacts: bool = True,
) -> dict[str, Any]:
    """Load a task seal only after recomputing its root and bound file hashes."""

    supplied = Path(root_or_path)
    seal_path = supplied if supplied.name == "seal.json" else supplied / "seal.json"
    root = seal_path.parent
    value = json.loads(seal_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("task seal must be an object")
    seal = dict(value)
    claimed_hash = seal.pop("seal_hash", "")
    if claimed_hash != canonical_hash(seal):
        raise ValueError("task seal hash is invalid")
    if seal.get("schema_version") != "smp.task_seal.v1" or seal.get("status") != "sealed":
        raise ValueError("task seal schema/status is invalid")
    if identity_hash is not None and seal.get("identity_hash") != identity_hash:
        raise ValueError("task seal identity linkage is invalid")
    artifact_hashes = seal.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        raise ValueError("task seal artifact hashes are invalid")
    if validate_artifacts:
        for name, expected in artifact_hashes.items():
            if not isinstance(name, str) or not name or Path(name).name != name:
                raise ValueError("task seal artifact name is unsafe")
            if not isinstance(expected, str) or not expected.startswith("sha256:") or len(expected) != 71:
                raise ValueError("task seal artifact hash is invalid")
            path = root / name
            if not path.is_file() or file_hash(path) != expected:
                raise ValueError("task seal artifact hash mismatch")
    return {**seal, "seal_hash": claimed_hash}


def load_validated_official_link(
    root_or_path: str | Path,
    *,
    identity_hash: str | None = None,
    seal_hash: str | None = None,
) -> dict[str, Any]:
    """Load an official link only after recomputing its link hash."""

    supplied = Path(root_or_path)
    path = supplied if supplied.name == "official_link.json" else supplied / "official_link.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("official link must be an object")
    link = dict(value)
    claimed_hash = link.pop("link_hash", "")
    if claimed_hash != canonical_hash(link):
        raise ValueError("official link hash is invalid")
    if link.get("schema_version") != "smp.official_link.v1":
        raise ValueError("official link schema is invalid")
    if identity_hash is not None and link.get("identity_hash") != identity_hash:
        raise ValueError("official link identity linkage is invalid")
    if seal_hash is not None and link.get("seal_hash") != seal_hash:
        raise ValueError("official link seal linkage is invalid")
    for name in ("identity_hash", "seal_hash", "outcome_hash", "report_hash", "evaluator_hash", "invocation_hash"):
        digest = link.get(name)
        if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
            raise ValueError(f"official link {name} is invalid")
    return {**link, "link_hash": claimed_hash}


def utility_trial_artifact_root(
    output_root: str | Path,
    *,
    run_id: str,
    subject_id: str,
    task_id: str,
    seed: int,
    run_index: int,
) -> Path:
    for name, value in (("run_id", run_id), ("subject_id", subject_id), ("task_id", task_id)):
        if not value or Path(value).name != value:
            raise ValueError(f"unsafe utility-trial {name}")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (seed, run_index)):
        raise ValueError("invalid utility-trial seed/run index")
    return Path(output_root) / run_id / "authorized_trials" / subject_id / task_id / f"seed-{seed}" / f"run-index-{run_index}"


def static_snapshot_artifact_root(
    output_root: str | Path,
    *,
    run_id: str,
    task_id: str,
    seed: int,
    run_index: int,
) -> Path:
    if not run_id or Path(run_id).name != run_id or not task_id or Path(task_id).name != task_id:
        raise ValueError("unsafe static snapshot identity")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (seed, run_index)):
        raise ValueError("invalid static snapshot seed/run index")
    return Path(output_root) / run_id / "frozen_active_bank" / task_id / f"seed-{seed}" / f"run-index-{run_index}"


def utility_trial_authorization_id(
    *, subject_id: str, task_id: str, seed: int, run_index: int, run_id: str
) -> str:
    return "smp-auth-" + canonical_hash(
        {
            "subject": subject_id,
            "target": task_id,
            "seed": seed,
            "run_index": run_index,
            "run": run_id,
        }
    )[7:23]


class SMPArtifactStore:
    def __init__(self, output_root: str | Path, identity: TaskRunIdentity):
        self.identity = identity
        self.root = Path(output_root) / identity.run_id / identity.task_id / identity.condition / f"seed-{identity.seed}" / f"run-index-{identity.run_index}"
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def seal_path(self) -> Path:
        return self.root / "seal.json"

    @property
    def official_link_path(self) -> Path:
        return self.root / "official_link.json"

    @property
    def evaluation_invocation_path(self) -> Path:
        return self.root / "evaluation_invocation.json"

    @property
    def official_predictions_path(self) -> Path:
        return self.root / "official_predictions.jsonl"

    def is_resumable(self) -> bool:
        if not self.seal_path.exists():
            return False
        try:
            load_validated_seal(self.root, identity_hash=self.identity.identity_hash)
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def save_sealed(
        self,
        *,
        trajectory: Mapping[str, Any],
        prediction: Mapping[str, Any],
        audit: tuple[dict[str, Any], ...],
        evidence: list[dict[str, Any]],
        runtime_state: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.official_link_path.exists():
            raise RuntimeError("official-linked task artifacts are immutable")
        if self.seal_path.exists():
            raise RuntimeError("sealed task artifacts are immutable; use exact resume")
        if not runtime_state.get("solver_done") or not runtime_state.get("prediction_sealed") or not runtime_state.get("trajectory_sealed"):
            raise ValueError("partial run cannot be saved as sealed")
        trajectory = sanitize_persistent_payload(trajectory)
        audit = tuple(sanitize_persistent_payload(row) for row in audit)
        evidence = sanitize_persistent_payload(evidence)
        serialized = canonical_json({"trajectory": trajectory, "prediction": prediction, "audit": audit, "evidence": evidence}).lower()
        forbidden = ("<smp_memory>", "model_thought", "hidden_test", "gold_patch", "authorization: bearer", "thought:")
        if any(marker in serialized for marker in forbidden):
            raise ValueError("unsafe content in persistent SMP artifacts")
        paths = {
            "identity.json": atomic_write_json(self.root / "identity.json", {**asdict(self.identity), "identity_hash": self.identity.identity_hash}),
            "trajectory.json": atomic_write_json(self.root / "trajectory.json", trajectory),
            "prediction.json": atomic_write_json(self.root / "prediction.json", prediction),
            "smp_audit.json": atomic_write_json(self.root / "smp_audit.json", list(audit)),
            "evidence.json": atomic_write_json(self.root / "evidence.json", evidence),
            "runtime_state.json": atomic_write_json(self.root / "runtime_state.json", runtime_state),
            "snapshot.json": atomic_write_json(self.root / "snapshot.json", snapshot),
            "plan.json": atomic_write_json(self.root / "plan.json", plan),
        }
        protocol_context = self.root / "protocol_context.json"
        if protocol_context.is_file():
            paths["protocol_context.json"] = protocol_context
        hashes = {name: file_hash(path) for name, path in paths.items()}
        seal = {
            "schema_version": "smp.task_seal.v1",
            "status": "sealed",
            "identity_hash": self.identity.identity_hash,
            "prediction_hash": runtime_state["prediction_hash"],
            "trajectory_hash": runtime_state["trajectory_hash"],
            "artifact_hashes": hashes,
        }
        seal["seal_hash"] = canonical_hash(seal)
        atomic_write_json(self.seal_path, seal)
        return seal

    def materialize_official_prediction(self) -> dict[str, Any]:
        if not self.is_resumable():
            raise RuntimeError("official prediction requires an exact sealed task")
        prediction_path = self.root / "prediction.json"
        prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
        if prediction.get("instance_id") != self.identity.task_id:
            raise ValueError("sealed prediction task linkage is invalid")
        rendered = json.dumps(prediction, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        target = self.official_predictions_path
        if target.exists() and target.read_text(encoding="utf-8") != rendered:
            raise RuntimeError("official prediction materialization is immutable")
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(rendered)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        seal = load_validated_seal(self.root, identity_hash=self.identity.identity_hash)
        return {
            "path": str(target.resolve()),
            "file_hash": file_hash(target),
            "prediction_hash": seal["prediction_hash"],
            "source_prediction_file_hash": file_hash(prediction_path),
        }

    def link_official(self, *, outcome_hash: str, report_hash: str, evaluator_hash: str, invocation_hash: str) -> dict[str, Any]:
        if not self.is_resumable():
            raise RuntimeError("official outcome cannot link before an exact sealed run")
        if self.official_link_path.exists():
            raise RuntimeError("official outcome is already linked")
        for name, value in (("outcome_hash", outcome_hash), ("report_hash", report_hash), ("evaluator_hash", evaluator_hash), ("invocation_hash", invocation_hash)):
            if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
                raise ValueError(f"invalid official link {name}")
        if evaluator_hash != self.identity.evaluator_hash:
            raise ValueError("official link evaluator does not match frozen identity")
        link = {
            "schema_version": "smp.official_link.v1",
            "identity_hash": self.identity.identity_hash,
            "seal_hash": load_validated_seal(self.root, identity_hash=self.identity.identity_hash)["seal_hash"],
            "outcome_hash": outcome_hash,
            "report_hash": report_hash,
            "evaluator_hash": evaluator_hash,
            "invocation_hash": invocation_hash,
        }
        link["link_hash"] = canonical_hash(link)
        atomic_write_json(self.official_link_path, link)
        return link
