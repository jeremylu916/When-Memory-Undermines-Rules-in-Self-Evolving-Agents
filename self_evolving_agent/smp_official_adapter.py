"""Deterministic delayed-write and lifecycle contracts for SMP."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from enum import Enum
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Mapping

from self_evolving_agent.smp_contracts import (
    MEMORY_SCHEMA_VERSION,
    MemoryExposure,
    MemoryState,
    MechanismSignature,
    NarrowScope,
    OfficialOutcome,
    SubmissionStatus,
    SMPMemoryItem,
    UtilityCertificate,
    canonical_hash,
)
from self_evolving_agent.smp_runtime import SMPRuntime
from self_evolving_agent.smp_protocol import CandidateValidationError, FrameworkInvariantError
from self_evolving_agent.harness_report import load_aggregate_report, load_detail_reports, load_prediction_records
from self_evolving_agent.python_runtime import default_harness_python, validate_python_runtime
from self_evolving_agent.smp_artifacts import (
    SMPArtifactStore,
    atomic_write_json,
    file_hash,
    load_validated_seal,
)


class SourceAdmission(str, Enum):
    PRESCRIPTIVE_ELIGIBLE = "prescriptive_eligible"
    ARCHIVE_ONLY = "archive_only"
    INVALID_OR_INCOMPLETE = "invalid_or_incomplete"


def classify_source_outcome(outcome: OfficialOutcome) -> tuple[SourceAdmission, str]:
    """Pure v5 success-only source admission; never task-configurable."""
    if not outcome.evaluation_terminal:
        return SourceAdmission.INVALID_OR_INCOMPLETE, "official_evaluation_incomplete"
    if outcome.submission_status is SubmissionStatus.EMPTY_PATCH:
        return SourceAdmission.ARCHIVE_ONLY, "empty_submission_no_candidate"
    if outcome.submission_status is SubmissionStatus.MISSING:
        return SourceAdmission.INVALID_OR_INCOMPLETE, "missing_submission_no_candidate"
    if not outcome.patch_applied:
        return SourceAdmission.ARCHIVE_ONLY, "materialization_failed_no_candidate"
    if (
        not outcome.resolved
        or outcome.fail_to_pass["failed"] != 0
        or outcome.pass_to_pass["failed"] != 0
    ):
        return SourceAdmission.ARCHIVE_ONLY, "official_failure_archive_only"
    return SourceAdmission.PRESCRIPTIVE_ELIGIBLE, "prescriptive"
LIFECYCLE_SCHEMA_VERSION = "smp.lifecycle.v1"
UTILITY_POLICY_VERSION = "smp.utility_policy.v1"
ALLOWED_TRANSITIONS = {
    MemoryState.RAW: MemoryState.CANDIDATE,
    MemoryState.CANDIDATE: MemoryState.SOURCE_AUDITED,
    MemoryState.SOURCE_AUDITED: MemoryState.FACT_CERTIFIED,
    MemoryState.FACT_CERTIFIED: MemoryState.SHADOW,
    MemoryState.SHADOW: MemoryState.ACTIVE,
    MemoryState.ACTIVE: MemoryState.NARROW,
    MemoryState.NARROW: MemoryState.SUPPRESSED,
    MemoryState.SUPPRESSED: MemoryState.RETIRED,
}


@dataclass(frozen=True)
class OfficialAdaptationContext:
    legal_split_or_past_stream: bool
    contains_gold_patch: bool
    contains_hidden_test_content: bool
    contains_current_or_future_outcome: bool
    provenance_complete: bool
    artifact_linkage_valid: bool
    schema_versions_compatible: bool

    def __post_init__(self) -> None:
        if any(not isinstance(value, bool) for value in asdict(self).values()):
            raise ValueError("Gate L context fields must be boolean")


@dataclass(frozen=True)
class MemoryCandidateDraft:
    signature: MechanismSignature
    exposure: MemoryExposure


@dataclass(frozen=True)
class LifecycleMemory:
    memory_id: str
    source_task_id: str
    polarity: str
    signature: MechanismSignature
    exposure: MemoryExposure
    state: MemoryState
    source_outcome_hash: str
    prediction_hash: str
    trajectory_hash: str
    report_hash: str
    available_from_task_index: int
    available_from_bank_version: int
    memory_schema_version: str = MEMORY_SCHEMA_VERSION
    narrow_scope: NarrowScope | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "source_task_id": self.source_task_id,
            "polarity": self.polarity,
            "signature": self.signature.to_dict(),
            "exposure": asdict(self.exposure),
            "state": self.state.value,
            "source_outcome_hash": self.source_outcome_hash,
            "prediction_hash": self.prediction_hash,
            "trajectory_hash": self.trajectory_hash,
            "report_hash": self.report_hash,
            "available_from_task_index": self.available_from_task_index,
            "available_from_bank_version": self.available_from_bank_version,
            "memory_schema_version": self.memory_schema_version,
            "narrow_scope": self.narrow_scope.to_dict() if self.narrow_scope else None,
        }


@dataclass(frozen=True)
class AdaptationResult:
    status: str
    reason_code: str
    memory_id: str | None
    earliest_future_task_index: int | None
    earliest_future_bank_version: int | None


class LifecycleLedger:
    def __init__(self, *, router_version: str, test_mode: bool = False):
        if not router_version:
            raise ValueError("router_version is required")
        self.router_version = router_version
        self.test_mode = test_mode
        self._memories: dict[str, LifecycleMemory] = {}
        self._events: list[dict[str, Any]] = []
        self._certificates: dict[str, UtilityCertificate] = {}

    @property
    def memories(self) -> Mapping[str, LifecycleMemory]:
        return deepcopy(self._memories)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(self._events))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "smp.lifecycle_ledger.v1",
            "router_version": self.router_version,
            "test_mode": self.test_mode,
            "memories": [memory.to_dict() for _, memory in sorted(self._memories.items())],
            "events": list(self.events),
            "activation_certificates": [certificate.to_dict() for _, certificate in sorted(self._certificates.items())],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LifecycleLedger":
        from self_evolving_agent.smp_io import certificate_from_dict, signature_from_dict

        if value.get("schema_version") != "smp.lifecycle_ledger.v1" or value.get("test_mode") is not False:
            raise ValueError("invalid production lifecycle ledger")
        ledger = cls(router_version=str(value.get("router_version", "")), test_mode=False)
        for row in value.get("activation_certificates", []):
            certificate = certificate_from_dict(row)
            if not certificate.compatible(
                subject_id=certificate.subject_id,
                schema_version=certificate.applicable_schema_version,
                router_version=ledger.router_version,
                test_mode=False,
            ):
                raise ValueError("persisted lifecycle activation certificate is invalid")
            ledger._certificates[certificate.certificate_hash] = certificate
        events = value.get("events", [])
        if not isinstance(events, list):
            raise ValueError("lifecycle events must be a list")
        previous = None
        for index, event in enumerate(events):
            if not isinstance(event, Mapping) or event.get("event_index") != index or event.get("previous_event_hash") != previous:
                raise ValueError("lifecycle event chain is invalid")
            expected = canonical_hash({key: child for key, child in event.items() if key != "event_hash"})
            if event.get("event_hash") != expected:
                raise ValueError("lifecycle event hash is invalid")
            previous = expected
        memories: dict[str, LifecycleMemory] = {}
        for row in value.get("memories", []):
            if not isinstance(row, Mapping):
                raise ValueError("lifecycle memory must be an object")
            scope_row = row.get("narrow_scope")
            scope = None
            if scope_row is not None:
                scope_data = dict(scope_row)
                for name in ("preconditions_all", "preconditions_any"):
                    if isinstance(scope_data.get(name), list):
                        scope_data[name] = tuple(scope_data[name])
                scope_hash = str(scope_data.pop("scope_hash", ""))
                scope = NarrowScope(**scope_data, scope_hash=scope_hash)
            memory = LifecycleMemory(
                memory_id=str(row["memory_id"]),
                source_task_id=str(row["source_task_id"]),
                polarity=str(row["polarity"]),
                signature=signature_from_dict(row["signature"]),
                exposure=MemoryExposure(**row["exposure"]),
                state=MemoryState(str(row["state"])),
                source_outcome_hash=str(row["source_outcome_hash"]),
                prediction_hash=str(row["prediction_hash"]),
                trajectory_hash=str(row["trajectory_hash"]),
                report_hash=str(row["report_hash"]),
                available_from_task_index=int(row["available_from_task_index"]),
                available_from_bank_version=int(row["available_from_bank_version"]),
                memory_schema_version=str(row.get("memory_schema_version", MEMORY_SCHEMA_VERSION)),
                narrow_scope=scope,
            )
            memories[memory.memory_id] = memory
        ledger._events = [dict(event) for event in events]
        ledger._memories = memories
        replayed: dict[str, MemoryState] = {}
        for event in events:
            memory_id = str(event["memory_id"])
            from_state = event.get("from_state")
            to_state = MemoryState(str(event["to_state"]))
            current = replayed.get(memory_id)
            if (current.value if current else None) != from_state:
                raise ValueError("lifecycle semantic replay from-state mismatch")
            if current is None:
                if to_state is not MemoryState.RAW:
                    raise ValueError("lifecycle must begin at raw")
            elif ALLOWED_TRANSITIONS.get(current) is not to_state:
                if not (to_state is MemoryState.SUPPRESSED and current in {MemoryState.ACTIVE, MemoryState.NARROW} and event.get("reason") == "security_harm"):
                    raise ValueError("lifecycle semantic replay transition is invalid")
            if to_state is MemoryState.ACTIVE:
                certificate_hash = event.get("certificate_hash")
                certificate = ledger._certificates.get(certificate_hash)
                if certificate is None or certificate.subject_id != memory_id:
                    raise ValueError("active lifecycle event lacks its valid certificate")
            replayed[memory_id] = to_state
        if set(replayed) != set(memories) or any(replayed[memory_id] is not memory.state for memory_id, memory in memories.items()):
            raise ValueError("persisted lifecycle terminal state does not match event replay")
        for memory_id, memory in memories.items():
            terminal = next(event for event in reversed(events) if event.get("memory_id") == memory_id)
            if terminal.get("memory_hash") != canonical_hash(memory.to_dict()):
                raise ValueError("persisted lifecycle terminal memory hash is invalid")
        for memory_id, memory in memories.items():
            if memory.state is MemoryState.NARROW or (
                memory.state is MemoryState.SUPPRESSED and memory.narrow_scope is not None
            ):
                if memory.narrow_scope is None or not any(
                    event.get("memory_id") == memory_id
                    and event.get("to_state") == MemoryState.NARROW.value
                    and event.get("narrow_scope_hash") == memory.narrow_scope.scope_hash
                    for event in events
                ):
                    raise ValueError("narrow lifecycle scope does not match its event")
        return ledger

    def _append_event(self, *, memory_id: str, from_state: str | None, to_state: MemoryState, reason: str, evidence_id: str = "", certificate: UtilityCertificate | None = None, narrow_scope: NarrowScope | None = None, memory_hash: str = "") -> None:
        event = {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "event_index": len(self._events),
            "previous_event_hash": self._events[-1]["event_hash"] if self._events else None,
            "memory_id": memory_id,
            "from_state": from_state,
            "to_state": to_state.value,
            "reason": reason,
            "evidence_id": evidence_id,
            "certificate_id": certificate.certificate_id if certificate else None,
            "certificate_hash": certificate.certificate_hash if certificate else None,
            "narrow_scope_hash": narrow_scope.scope_hash if narrow_scope else None,
            "memory_hash": memory_hash or None,
        }
        event["event_hash"] = canonical_hash(event)
        self._events.append(event)

    def add_candidate(self, memory: LifecycleMemory) -> None:
        if memory.memory_id in self._memories or memory.state is not MemoryState.CANDIDATE:
            raise ValueError("candidate creation is invalid")
        self._append_event(memory_id=memory.memory_id, from_state=None, to_state=MemoryState.RAW, reason="official_adaptation_raw")
        self._append_event(memory_id=memory.memory_id, from_state=MemoryState.RAW.value, to_state=MemoryState.CANDIDATE, reason="gate_l_s_candidate", memory_hash=canonical_hash(memory.to_dict()))
        self._memories[memory.memory_id] = memory

    def transition(self, memory_id: str, to_state: MemoryState, *, reason: str, certificate: UtilityCertificate | None = None, narrow_scope: NarrowScope | None = None) -> bool:
        if memory_id not in self._memories:
            raise ValueError("unknown lifecycle memory")
        current = self._memories[memory_id]
        to_state = MemoryState(to_state)
        if ALLOWED_TRANSITIONS.get(current.state) is not to_state:
            raise ValueError("invalid lifecycle transition")
        if to_state is MemoryState.ACTIVE:
            if certificate is None or not certificate.compatible(
                subject_id=memory_id,
                schema_version=current.memory_schema_version,
                router_version=self.router_version,
                test_mode=self.test_mode,
            ):
                return False
            self._certificates[certificate.certificate_hash] = certificate
        if to_state is MemoryState.NARROW:
            if (
                narrow_scope is None
                or narrow_scope.memory_id != memory_id
                or narrow_scope.signature_hash != current.signature.signature_hash
            ):
                return False
        elif narrow_scope is not None:
            raise ValueError("NarrowScope is valid only for active-to-narrow transition")
        updated = replace(current, state=to_state, narrow_scope=narrow_scope)
        self._append_event(
            memory_id=memory_id,
            from_state=current.state.value,
            to_state=to_state,
            reason=reason,
            certificate=certificate,
            narrow_scope=narrow_scope,
            memory_hash=canonical_hash(updated.to_dict()),
        )
        self._memories[memory_id] = updated
        return True

    def record_security_harm(self, memory_id: str, *, event_id: str) -> None:
        if not event_id:
            raise ValueError("security harm event_id is required")
        if memory_id not in self._memories or self._memories[memory_id].state not in {MemoryState.ACTIVE, MemoryState.NARROW}:
            raise ValueError("security harm suppression requires active or narrow memory")
        current = self._memories[memory_id]
        self._append_event(
            memory_id=memory_id,
            from_state=current.state.value,
            to_state=MemoryState.SUPPRESSED,
            reason="security_harm",
            evidence_id=event_id,
            memory_hash=canonical_hash(replace(current, state=MemoryState.SUPPRESSED).to_dict()),
        )
        self._memories[memory_id] = replace(current, state=MemoryState.SUPPRESSED)

    def retrievable(self, memory_id: str) -> bool:
        return memory_id in self._memories and self._memories[memory_id].state in {MemoryState.ACTIVE, MemoryState.NARROW}

    def activation_certificate(self, memory_id: str) -> UtilityCertificate | None:
        event = next(
            (
                event
                for event in reversed(self._events)
                if event.get("memory_id") == memory_id and event.get("to_state") == MemoryState.ACTIVE.value
            ),
            None,
        )
        if event is None:
            return None
        certificate = self._certificates.get(str(event.get("certificate_hash")))
        return deepcopy(certificate) if certificate is not None else None

    def runtime_memory(self, memory_id: str, *, task_index: int, bank_version: int) -> SMPMemoryItem | None:
        if not self.retrievable(memory_id):
            return None
        memory = self._memories[memory_id]
        if task_index < memory.available_from_task_index or bank_version < memory.available_from_bank_version:
            return None
        return SMPMemoryItem(
            memory_id=memory.memory_id,
            signature=memory.signature,
            state=memory.state,
            exposure=memory.exposure,
            source_task_id=memory.source_task_id,
            available_from_task_index=memory.available_from_task_index,
            memory_schema_version=memory.memory_schema_version,
            narrow_scope=memory.narrow_scope,
        )


class OfficialOutcomeAdapter:
    def __init__(self, *, ledger: LifecycleLedger):
        self.ledger = ledger
        self.audit_events: list[dict[str, Any]] = []

    def _record(self, *, task_id: str, status: str, reason_code: str, outcome_hash: str) -> None:
        event = {
            "event_index": len(self.audit_events),
            "task_id": task_id,
            "status": status,
            "reason_code": reason_code,
            "outcome_hash": outcome_hash,
        }
        event["event_hash"] = canonical_hash(event)
        self.audit_events.append(event)

    def quarantine_candidate(self, *, outcome: OfficialOutcome, reason_code: str) -> AdaptationResult:
        self._record(
            task_id=outcome.task_id,
            status="quarantined",
            reason_code=reason_code,
            outcome_hash=outcome.outcome_hash,
        )
        return AdaptationResult("quarantined", reason_code, None, None, None)

    def archive_source(self, *, outcome: OfficialOutcome, reason_code: str, invalid: bool = False) -> AdaptationResult:
        status = "source_invalid" if invalid else "source_archived"
        self._record(task_id=outcome.task_id, status=status, reason_code=reason_code, outcome_hash=outcome.outcome_hash)
        return AdaptationResult(status, reason_code, None, None, None)

    def adapt(
        self,
        *,
        runtime: SMPRuntime,
        outcome: OfficialOutcome,
        context: OfficialAdaptationContext,
        draft: MemoryCandidateDraft,
    ) -> AdaptationResult:
        if not runtime.state.solver_done:
            return AdaptationResult("rejected", "solver_not_terminated", None, None, None)
        if not runtime.state.prediction_sealed or not runtime.state.trajectory_sealed:
            return AdaptationResult("rejected", "artifacts_not_sealed", None, None, None)
        if outcome.task_id != runtime.state.task_id:
            return AdaptationResult("rejected", "task_linkage_invalid", None, None, None)
        if outcome.prediction_hash != runtime.state.prediction_hash or outcome.trajectory_hash != runtime.state.trajectory_hash:
            return AdaptationResult("rejected", "artifact_linkage_invalid", None, None, None)
        gate_l_reason = self._gate_l_reason(context)
        if gate_l_reason:
            return self.quarantine_candidate(outcome=outcome, reason_code=gate_l_reason)
        admission, gate_s_reason = classify_source_outcome(outcome)
        if admission is not SourceAdmission.PRESCRIPTIVE_ELIGIBLE:
            return self.archive_source(
                outcome=outcome,
                reason_code=gate_s_reason,
                invalid=admission is SourceAdmission.INVALID_OR_INCOMPLETE,
            )
        polarity = "prescriptive"
        try:
            draft.exposure.render("candidate")
        except ValueError as exc:
            raise CandidateValidationError("unsafe_candidate_exposure") from exc
        except TypeError as exc:
            raise FrameworkInvariantError("unexpected_candidate_adapter_type_error") from exc
        future_task = runtime.state.snapshot.task_index + 1
        future_bank = runtime.state.snapshot.bank_version + 1
        payload = {
            "source_task_id": outcome.task_id,
            "polarity": polarity,
            "signature_hash": draft.signature.signature_hash,
            "outcome_hash": outcome.outcome_hash,
            "available_from_task_index": future_task,
            "available_from_bank_version": future_bank,
        }
        memory_id = "smpmem_" + canonical_hash(payload)[7:31]
        memory = LifecycleMemory(
            memory_id=memory_id,
            source_task_id=outcome.task_id,
            polarity=polarity,
            signature=draft.signature,
            exposure=draft.exposure,
            state=MemoryState.CANDIDATE,
            source_outcome_hash=outcome.outcome_hash,
            prediction_hash=outcome.prediction_hash,
            trajectory_hash=outcome.trajectory_hash,
            report_hash=outcome.report_hash,
            available_from_task_index=future_task,
            available_from_bank_version=future_bank,
        )
        self.ledger.add_candidate(memory)
        self._record(task_id=outcome.task_id, status="candidate_created", reason_code=polarity, outcome_hash=outcome.outcome_hash)
        return AdaptationResult("candidate_created", polarity, memory_id, future_task, future_bank)

    @staticmethod
    def _gate_l_reason(context: OfficialAdaptationContext) -> str | None:
        checks = (
            (context.legal_split_or_past_stream, "illegal_split_or_stream"),
            (not context.contains_gold_patch, "gold_patch_leakage"),
            (not context.contains_hidden_test_content, "hidden_test_leakage"),
            (not context.contains_current_or_future_outcome, "current_future_outcome_leakage"),
            (context.provenance_complete, "incomplete_provenance"),
            (context.artifact_linkage_valid, "invalid_artifact_linkage"),
            (context.schema_versions_compatible, "incompatible_schema"),
        )
        return next((reason for passed, reason in checks if not passed), None)

class OfficialHarnessAdapter:
    """Preview/invoke the existing SWE-bench harness and parse its raw reports."""

    schema_version = "smp.official_harness_adapter.v1"

    def __init__(self, *, harness_python: str | None = None, evaluator_version: str):
        self.harness_python = harness_python or default_harness_python()
        if not evaluator_version:
            raise ValueError("official evaluator version is required")
        self.evaluator_version = evaluator_version

    @property
    def evaluator_hash(self) -> str:
        return canonical_hash(
            {
                "schema_version": self.schema_version,
                "module": "swebench.harness.run_evaluation",
                "evaluator_version": self.evaluator_version,
            }
        )

    def command(
        self,
        *,
        predictions_path: str | Path,
        dataset: str,
        split: str,
        run_id: str,
        output_dir: str | Path,
        max_workers: int = 1,
    ) -> list[str]:
        return [
            self.harness_python,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            dataset,
            "--split",
            split,
            "--predictions_path",
            str(Path(predictions_path).resolve()),
            "--max_workers",
            str(max_workers),
            "--run_id",
            run_id,
            "--report_dir",
            str(Path(output_dir).resolve()),
        ]

    def invoke(
        self,
        *,
        store: SMPArtifactStore,
        dry_run: bool,
        run_command: Any = subprocess.run,
        **command_kwargs: Any,
    ) -> dict[str, Any]:
        if not store.is_resumable():
            raise RuntimeError("official evaluator cannot run before prediction/trajectory seals")
        if store.official_link_path.exists():
            raise RuntimeError("official-linked evaluation artifacts are immutable")
        if store.identity.evaluator_hash != self.evaluator_hash:
            raise RuntimeError("official evaluator does not match the frozen task identity")
        timeout = command_kwargs.pop("timeout", 7200)
        prediction = store.materialize_official_prediction()
        supplied_predictions = command_kwargs.pop("predictions_path", None)
        if supplied_predictions is not None and Path(supplied_predictions).resolve() != Path(prediction["path"]):
            raise ValueError("official evaluator input must be materialized from the sealed prediction")
        command_kwargs["predictions_path"] = prediction["path"]
        command = self.command(**command_kwargs)
        if store.evaluation_invocation_path.is_file():
            existing = json.loads(store.evaluation_invocation_path.read_text(encoding="utf-8"))
            existing_hash = existing.pop("invocation_hash", "")
            if (
                existing.get("status") == "invoked_success"
                and existing_hash == canonical_hash(existing)
                and existing.get("command_hash") == canonical_hash(command)
                and existing.get("prediction", {}).get("file_hash") == prediction["file_hash"]
            ):
                return {**existing, "invocation_hash": existing_hash, "resumed": True}
        preview = {
            "schema_version": "smp.evaluation_invocation.v1",
            "identity_hash": store.identity.identity_hash,
            "seal_hash": load_validated_seal(
                store.root, identity_hash=store.identity.identity_hash
            )["seal_hash"],
            "evaluator_hash": self.evaluator_hash,
            "prediction": prediction,
            "dataset": command_kwargs["dataset"],
            "split": command_kwargs["split"],
            "evaluation_run_id": command_kwargs["run_id"],
            "report_dir": str(Path(command_kwargs["output_dir"]).resolve()),
            "command": command,
            "command_hash": canonical_hash(command),
            "dry_run": dry_run,
            "status": "dry_run_preview" if dry_run else "invoking",
        }
        preview["invocation_hash"] = canonical_hash(preview)
        atomic_write_json(store.evaluation_invocation_path, preview)
        if dry_run:
            return preview
        report_directory = Path(command_kwargs["output_dir"])
        if report_directory.exists() and any(report_directory.rglob("*")):
            raise RuntimeError("official report directory must be empty before invocation")
        report_directory.mkdir(parents=True, exist_ok=True)
        validate_python_runtime(self.harness_python, purpose="SWE-bench harness", required_module="swebench.harness.run_evaluation")
        started = time.monotonic()
        completed = run_command(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=str(report_directory.resolve()),
        )
        result = {
            **{key: value for key, value in preview.items() if key != "invocation_hash"},
            "status": "invoked_success" if completed.returncode == 0 else "harness_error",
            "returncode": completed.returncode,
            "stdout_hash": canonical_hash({"stdout": str(completed.stdout)}),
            "stderr_hash": canonical_hash({"stderr": str(completed.stderr)}),
            "evaluator_accounting": {
                "duration_seconds": max(0.0, time.monotonic() - started),
                "max_workers": int(command_kwargs.get("max_workers", 1)),
                "task_count": 1,
            },
        }
        result["invocation_hash"] = canonical_hash(result)
        atomic_write_json(store.evaluation_invocation_path, result)
        return result

    def parse_outcome(
        self,
        *,
        store: SMPArtifactStore,
        task_id: str,
        aggregate_report_path: str | Path,
        detail_report_root: str | Path,
        evaluation_timestamp: str,
    ) -> OfficialOutcome:
        if not store.is_resumable() or store.identity.task_id != task_id:
            raise RuntimeError("official report linkage requires the matching sealed task")
        if store.identity.evaluator_hash != self.evaluator_hash:
            raise RuntimeError("official evaluator does not match the frozen task identity")
        if not store.evaluation_invocation_path.is_file():
            raise RuntimeError("official report has no persisted evaluator invocation")
        invocation = json.loads(store.evaluation_invocation_path.read_text(encoding="utf-8"))
        invocation_hash = invocation.pop("invocation_hash", "")
        if (
            invocation.get("status") != "invoked_success"
            or invocation_hash != canonical_hash(invocation)
            or invocation.get("identity_hash") != store.identity.identity_hash
            or invocation.get("evaluator_hash") != self.evaluator_hash
            or invocation.get("prediction", {}).get("file_hash") != file_hash(store.official_predictions_path)
        ):
            raise RuntimeError("official evaluator invocation provenance is invalid")
        report_root = Path(invocation["report_dir"]).resolve()
        aggregate_path = Path(aggregate_report_path).resolve()
        detail_root = Path(detail_report_root).resolve()
        if detail_root != report_root or report_root not in aggregate_path.parents:
            raise RuntimeError("official reports do not belong to the persisted invocation root")
        aggregate = load_aggregate_report(aggregate_report_path)
        details = load_detail_reports(detail_report_root)
        resolved_ids = set(aggregate.get("resolved_ids", ()))
        unresolved_ids = set(aggregate.get("unresolved_ids", ()))
        error_ids = set(aggregate.get("error_ids", ()))
        incomplete_ids = set(aggregate.get("incomplete_ids", ()))
        empty_patch_ids = set(aggregate.get("empty_patch_ids", ()))
        prediction_rows = load_prediction_records(store.official_predictions_path)
        matching_predictions = [row for row in prediction_rows if str(row.get("instance_id")) == task_id]
        if len(matching_predictions) != 1:
            submission_status = SubmissionStatus.MISSING
        elif str(matching_predictions[0].get("model_patch") or "").strip():
            submission_status = SubmissionStatus.MATERIAL_PATCH
        else:
            submission_status = SubmissionStatus.EMPTY_PATCH
        detail = details.get(task_id, {})
        bucket_memberships = sum(
            task_id in bucket
            for bucket in (resolved_ids, unresolved_ids, error_ids, incomplete_ids, empty_patch_ids)
        )
        bucket_invalid = bucket_memberships != 1 or task_id in error_ids or task_id in incomplete_ids
        if submission_status is SubmissionStatus.EMPTY_PATCH:
            evaluation_terminal = not bucket_invalid and task_id in empty_patch_ids
        elif submission_status is SubmissionStatus.MATERIAL_PATCH:
            expected_resolved = task_id in resolved_ids
            expected_unresolved = task_id in unresolved_ids
            evaluation_terminal = (
                not bucket_invalid
                and (expected_resolved or expected_unresolved)
                and isinstance(detail.get("resolved"), bool)
                and bool(detail["resolved"]) is expected_resolved
            )
        else:
            evaluation_terminal = False
        resolved = evaluation_terminal and task_id in resolved_ids
        patch_applied = (
            evaluation_terminal
            and submission_status is SubmissionStatus.MATERIAL_PATCH
            and bool(detail.get("patch_successfully_applied", False))
        )
        counts = self._counts(detail.get("tests_status", {}))
        failure_class = None
        if submission_status is SubmissionStatus.EMPTY_PATCH:
            failure_class = "empty_submission"
        elif evaluation_terminal and not resolved:
            if submission_status is SubmissionStatus.MATERIAL_PATCH and not patch_applied:
                failure_class = "materialization_failed"
            elif counts["pass_to_pass"]["failed"]:
                failure_class = "pass_to_pass_regression"
            elif counts["fail_to_pass"]["failed"]:
                failure_class = "fail_to_pass_failure"
        seal = load_validated_seal(store.root, identity_hash=store.identity.identity_hash)
        report_hash = canonical_hash(
            {
                "aggregate_file_hash": file_hash(aggregate_report_path),
                "detail": detail,
                "task_id": task_id,
                "invocation_hash": invocation_hash,
            }
        )
        return OfficialOutcome(
            task_id=task_id,
            evaluator_name="swebench_official",
            evaluator_version=self.evaluator_version,
            evaluation_terminal=evaluation_terminal,
            submission_status=submission_status,
            patch_applied=patch_applied,
            resolved=resolved,
            fail_to_pass=counts["fail_to_pass"],
            pass_to_pass=counts["pass_to_pass"],
            normalized_failure_class=failure_class,
            prediction_hash=seal["prediction_hash"],
            trajectory_hash=seal["trajectory_hash"],
            report_hash=report_hash,
            evaluation_timestamp=evaluation_timestamp,
        )

    @staticmethod
    def _counts(tests_status: Mapping[str, Any]) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for source, target in (("FAIL_TO_PASS", "fail_to_pass"), ("PASS_TO_PASS", "pass_to_pass")):
            row = tests_status.get(source, {}) if isinstance(tests_status, Mapping) else {}
            if not isinstance(row, Mapping):
                row = {}
            result[target] = {
                "passed": len(row.get("success", ()) or ()),
                "failed": len(row.get("failure", ()) or ()),
            }
        return result
