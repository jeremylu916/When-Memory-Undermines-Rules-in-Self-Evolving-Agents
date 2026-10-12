"""Minimal deterministic contracts for the SMP read path."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping


SMP_SCHEMA_VERSION = "smp.v1"
SIGNATURE_VERSION = "smp.signature.v1"
MEMORY_SCHEMA_VERSION = "smp.memory.v1"


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"non-serializable SMP value: {type(value).__name__}")


class EvidenceType(str, Enum):
    TASK_INPUT = "task_visible_input"
    CODE_SEARCH = "code_search_result"
    CODE_OBSERVATION = "code_file_observation"
    TEST_RESULT = "test_result"
    PATCH_OBSERVATION = "patch_diff_observation"
    PATCH_APPLICATION = "patch_apply_materialization_result"
    EXPLICIT_MEMORY_REQUEST = "explicit_solver_memory_request"
    SOLVER_TERMINATION = "solver_termination"


class EvidenceSource(str, Enum):
    OFFICIAL_SANITIZED_OUTCOME = "official_sanitized_outcome"
    EXECUTABLE_OUTPUT = "executable_command_test_output"
    VISIBLE_CODE_PATCH = "visible_code_patch_state"
    PUBLIC_CONTEXT = "public_task_repository_context"
    MODEL_THOUGHT = "model_thought"


class LeakageClass(str, Enum):
    PUBLIC_TASK = "public_task"
    PUBLIC_RUNTIME = "public_runtime"
    SANITIZED_OFFICIAL = "sanitized_official"
    MODEL_HINT = "model_hint"
    FORBIDDEN = "forbidden"


class FieldStatus(str, Enum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class SubmissionStatus(str, Enum):
    MATERIAL_PATCH = "material_patch"
    EMPTY_PATCH = "empty_patch"
    MISSING = "missing"


class MemoryState(str, Enum):
    RAW = "raw"
    CANDIDATE = "candidate"
    SOURCE_AUDITED = "source_audited"
    FACT_CERTIFIED = "fact_certified"
    SHADOW = "shadow"
    ACTIVE = "active"
    NARROW = "narrow"
    SUPPRESSED = "suppressed"
    RETIRED = "retired"


class GateMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ENFORCE = "ENFORCE"


class TriggerCategory(str, Enum):
    TASK_START = "TASK_START"
    LOCALIZATION_MILESTONE = "LOCALIZATION_MILESTONE"
    DIAGNOSTIC_TEST_FAILURE = "DIAGNOSTIC_TEST_FAILURE"
    PATCH_APPLICATION_FAILURE = "PATCH_APPLICATION_FAILURE"
    EXPLICIT_MEMORY_REQUEST = "EXPLICIT_MEMORY_REQUEST"


class UnknownReason(str, Enum):
    BANK_EMPTY = "bank_empty"
    NO_ACTIVE_MEMORY = "no_active_memory"
    GATE_F_LOW_CONFIDENCE = "gate_f_low_confidence"
    CRITICAL_SIGNATURE_UNKNOWN = "critical_signature_unknown"
    EVIDENCE_CONTRADICTION = "evidence_contradiction"
    GATE_R_LOW_MATCH = "gate_r_low_match"
    AMBIGUOUS_TOP_CANDIDATES = "ambiguous_top_candidates"
    TYPED_CONTRADICTION = "typed_contradiction"
    INVALID_CALIBRATION_PROFILE = "invalid_calibration_profile"
    SHADOW_MODE = "shadow_mode"
    GATE_OFF = "gate_off"
    ROUTING_BUDGET_EXHAUSTED = "routing_budget_exhausted"
    EXPOSURE_BUDGET_EXHAUSTED = "exposure_budget_exhausted"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    DUPLICATE_WITHOUT_EVIDENCE_CHANGE = "duplicate_without_evidence_change"
    CONTROLLER_ERROR = "controller_error"


@dataclass(frozen=True)
class EvidenceEvent:
    task_id: str
    event_index: int
    event_type: EvidenceType
    source: EvidenceSource
    observable: bool
    leakage_class: LeakageClass
    payload: Mapping[str, Any]
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.task_id or self.event_index < 0:
            raise ValueError("evidence task_id and non-negative event_index are required")
        if self.leakage_class is LeakageClass.FORBIDDEN:
            raise ValueError("forbidden evidence cannot enter SMP")
        if self.source is EvidenceSource.MODEL_THOUGHT and (
            self.observable or self.leakage_class is not LeakageClass.MODEL_HINT
        ):
            raise ValueError("model thought must remain a non-observable hint")
        payload = _jsonable(self.payload)
        object.__setattr__(self, "payload", payload)
        expected = "ev_" + canonical_hash(self.identity_payload())[7:27]
        if self.event_id and self.event_id != expected:
            raise ValueError("evidence event_id mismatch")
        object.__setattr__(self, "event_id", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "event_index": self.event_index,
            "event_type": self.event_type.value,
            "source": self.source.value,
            "observable": self.observable,
            "leakage_class": self.leakage_class.value,
            "payload": self.payload,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SMP_SCHEMA_VERSION, "event_id": self.event_id, **self.identity_payload()}


SIGNATURE_FIELDS = ("ownership", "operation", "observed_violation", "required_relation", "preconditions", "intervention")
CRITICAL_SIGNATURE_FIELDS = ("ownership", "operation", "observed_violation", "required_relation")


@dataclass(frozen=True)
class SignatureField:
    value: str | tuple[str, ...] | None
    status: FieldStatus
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, FieldStatus):
            raise ValueError("signature field status is invalid")
        if not isinstance(self.evidence_refs, tuple) or any(
            not isinstance(ref, str) or not ref for ref in self.evidence_refs
        ):
            raise ValueError("signature evidence refs must be non-empty strings")
        if self.status is FieldStatus.UNKNOWN:
            if self.value is not None or self.evidence_refs:
                raise ValueError("unknown signature field requires null value and no evidence refs")
        elif self.value in (None, "", ()) or not self.evidence_refs:
            raise ValueError("known signature field requires a value and evidence refs")


@dataclass(frozen=True)
class MechanismSignature:
    ownership: SignatureField
    operation: SignatureField
    observed_violation: SignatureField
    required_relation: SignatureField
    preconditions: SignatureField
    intervention: SignatureField
    signature_version: str = SIGNATURE_VERSION
    signature_hash: str = ""

    def __post_init__(self) -> None:
        if self.signature_version != SIGNATURE_VERSION:
            raise ValueError("unsupported mechanism signature version")
        for name in SIGNATURE_FIELDS:
            field = getattr(self, name)
            if name == "preconditions":
                if field.value is not None and (
                    not isinstance(field.value, tuple)
                    or any(not isinstance(value, str) or not value for value in field.value)
                ):
                    raise ValueError("preconditions signature value must be a string tuple or null")
            elif field.value is not None and (not isinstance(field.value, str) or not field.value):
                raise ValueError(f"{name} signature value must be a non-empty string or null")
        expected = canonical_hash(self.identity_payload())
        if self.signature_hash and self.signature_hash != expected:
            raise ValueError("signature_hash mismatch")
        object.__setattr__(self, "signature_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "signature_version": self.signature_version,
            **{name: _jsonable(getattr(self, name)) for name in SIGNATURE_FIELDS},
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "signature_hash": self.signature_hash}

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(ref for name in SIGNATURE_FIELDS for ref in getattr(self, name).evidence_refs))

    @property
    def critical_unknowns(self) -> tuple[str, ...]:
        return tuple(name for name in CRITICAL_SIGNATURE_FIELDS if getattr(self, name).status is FieldStatus.UNKNOWN)


@dataclass(frozen=True)
class NarrowScope:
    memory_id: str
    signature_hash: str
    ownership: str | None = None
    operation: str | None = None
    observed_violation: str | None = None
    required_relation: str | None = None
    preconditions_all: tuple[str, ...] = ()
    preconditions_any: tuple[str, ...] = ()
    schema_version: str = "smp.narrow_scope.v1"
    scope_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "smp.narrow_scope.v1" or not self.memory_id:
            raise ValueError("invalid narrow scope identity")
        _require_hash(self.signature_hash, "narrow signature_hash")
        exact = (self.ownership, self.operation, self.observed_violation, self.required_relation)
        if not any(value for value in (*exact, *self.preconditions_all, *self.preconditions_any)):
            raise ValueError("narrow scope requires at least one typed predicate")
        if any(value is not None and (not isinstance(value, str) or not value) for value in exact):
            raise ValueError("narrow exact predicates must be non-empty strings")
        for values in (self.preconditions_all, self.preconditions_any):
            if not isinstance(values, tuple) or any(not isinstance(value, str) or not value for value in values):
                raise ValueError("narrow precondition predicates must be non-empty string tuples")
        expected = canonical_hash(self.identity_payload())
        if self.scope_hash and self.scope_hash != expected:
            raise ValueError("narrow scope_hash mismatch")
        object.__setattr__(self, "scope_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {key: _jsonable(value) for key, value in asdict(self).items() if key != "scope_hash"}

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "scope_hash": self.scope_hash}

    def matches(self, signature: MechanismSignature) -> bool:
        for name in ("ownership", "operation", "observed_violation", "required_relation"):
            expected = getattr(self, name)
            if expected is None:
                continue
            field = getattr(signature, name)
            if field.status is FieldStatus.UNKNOWN or field.value != expected:
                return False
        preconditions = signature.preconditions
        if preconditions.status is FieldStatus.UNKNOWN:
            return not (self.preconditions_all or self.preconditions_any)
        observed = {str(value) for value in (preconditions.value if isinstance(preconditions.value, tuple) else (preconditions.value,))}
        return set(self.preconditions_all).issubset(observed) and (
            not self.preconditions_any or bool(set(self.preconditions_any) & observed)
        )


@dataclass(frozen=True)
class FrozenCalibrationProfile:
    artifact_id: str
    signature_scorer_version: str
    retrieval_scorer_version: str
    tau_f: float
    tau_r: float
    gamma: float
    applicable_domain: str
    applicable_memory_schema_version: str
    certification_split_id: str
    certification_method: str
    certification_metadata: Mapping[str, Any]
    issued_at: str
    test_only: bool
    schema_version: str = "smp.calibration.v1"
    expiry_or_invalidation: Mapping[str, Any] | None = None
    production_bindings: Mapping[str, Any] = ()
    artifact_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.test_only, bool):
            raise ValueError("calibration test_only must be boolean")
        object.__setattr__(self, "certification_metadata", _jsonable(self.certification_metadata))
        object.__setattr__(self, "expiry_or_invalidation", _jsonable(self.expiry_or_invalidation))
        object.__setattr__(self, "production_bindings", _jsonable(self.production_bindings))
        expected = canonical_hash(self.identity_payload())
        if self.artifact_hash and self.artifact_hash != expected:
            raise ValueError("calibration artifact_hash mismatch")
        object.__setattr__(self, "artifact_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {key: _jsonable(value) for key, value in asdict(self).items() if key != "artifact_hash"}

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "artifact_hash": self.artifact_hash}

    def compatible(self, *, domain: str, memory_schema_version: str, test_mode: bool) -> bool:
        required = (
            self.artifact_id,
            self.signature_scorer_version,
            self.retrieval_scorer_version,
            self.applicable_domain,
            self.certification_split_id,
            self.certification_method,
            self.issued_at,
        )
        certification_text = canonical_json(
            {
                "split": self.certification_split_id,
                "method": self.certification_method,
                "metadata": self.certification_metadata,
            }
        ).lower()
        synthetic_marked = any(marker in certification_text for marker in ("synthetic", "fixture", "test-only", "test_only"))
        return (
            self.schema_version == "smp.calibration.v1"
            and all(required)
            and isinstance(self.certification_metadata, Mapping)
            and bool(self.certification_metadata)
            and (
                self.expiry_or_invalidation is None
                or isinstance(self.expiry_or_invalidation, Mapping)
            )
            and 0 <= self.tau_f <= 1
            and 0 <= self.tau_r <= 1
            and 0 <= self.gamma <= 1
            and self.applicable_domain == domain
            and self.applicable_memory_schema_version == memory_schema_version
            and (
                self.test_only
                or (
                    isinstance(self.production_bindings, Mapping)
                    and set(self.production_bindings) == {
                        "extractor_model_hash",
                        "extractor_config_hash",
                        "extractor_prompt_hash",
                        "signature_schema_hash",
                        "gate_f_feature_hash",
                        "gate_r_feature_hash",
                        "gate_r_weights_hash",
                        "tune_manifest_hash",
                        "cert_manifest_hash",
                        "certification_records_hash",
                    }
                    and all(_is_hash(value) for value in self.production_bindings.values())
                )
            )
            and (test_mode or not self.test_only)
            and not (synthetic_marked and not self.test_only)
            and not (self.expiry_or_invalidation or {}).get("invalidated", False)
            and not (self.expiry_or_invalidation or {}).get("expired", False)
            and self.artifact_hash == canonical_hash(self.identity_payload())
        )


@dataclass(frozen=True)
class MemoryExposure:
    mechanism_trigger: str
    action_guidance: str
    caution: str
    scope: str
    provenance_class: str

    def render(self, memory_id: str) -> str:
        fields = {
            "memory_id": memory_id,
            "mechanism_trigger": self.mechanism_trigger,
            "action_guidance": self.action_guidance,
            "caution": self.caution,
            "scope": self.scope,
            "provenance_class": self.provenance_class,
        }
        text = "<smp_memory>\n" + "\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n</smp_memory>"
        forbidden = ("gold_patch", "hidden_test", "official_stdout", "official_stderr", "api_key", "sk-")
        secret_pattern = re.compile(r"(?i)\b(authorization|bearer|token|password|secret)\b|\bgh[pousr]_[A-Za-z0-9_]{8,}\b")
        if any(marker in text.lower() for marker in forbidden) or secret_pattern.search(text):
            raise ValueError("unsafe solver-visible memory")
        return text


@dataclass(frozen=True)
class SMPMemoryItem:
    memory_id: str
    signature: MechanismSignature
    state: MemoryState
    exposure: MemoryExposure
    source_task_id: str
    available_from_task_index: int
    memory_schema_version: str = MEMORY_SCHEMA_VERSION
    narrow_scope: NarrowScope | None = None

    def __post_init__(self) -> None:
        if not self.memory_id or not self.source_task_id or self.available_from_task_index < 0:
            raise ValueError("memory provenance fields are required")
        if self.memory_schema_version != MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported memory schema")
        if self.state is MemoryState.NARROW:
            if self.narrow_scope is None:
                raise ValueError("narrow memory requires NarrowScope")
            if self.narrow_scope.memory_id != self.memory_id or self.narrow_scope.signature_hash != self.signature.signature_hash:
                raise ValueError("NarrowScope binding mismatch")
        elif self.narrow_scope is not None:
            raise ValueError("only narrow memory may carry NarrowScope")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class BankSnapshot:
    task_id: str
    task_index: int
    bank_version: int
    memories: tuple[SMPMemoryItem, ...]
    bank_item_count: int = -1
    snapshot_id: str = ""
    snapshot_hash: str = ""

    def __post_init__(self) -> None:
        if not self.task_id or self.task_index < 0 or self.bank_version < 0:
            raise ValueError("invalid bank snapshot identity")
        if self.bank_item_count < 0:
            object.__setattr__(self, "bank_item_count", len(self.memories))
        if self.bank_item_count < len(self.memories):
            raise ValueError("bank_item_count cannot be smaller than active snapshot")
        for memory in self.memories:
            if memory.state not in {MemoryState.ACTIVE, MemoryState.NARROW}:
                raise ValueError("sealed production snapshot may contain active or valid narrow memory only")
            if memory.source_task_id == self.task_id or memory.available_from_task_index > self.task_index:
                raise ValueError("memory is not authorized before this task")
        payload = self.identity_payload()
        digest = canonical_hash(payload)
        expected_id = "snapshot_" + digest[7:27]
        if self.snapshot_hash and self.snapshot_hash != digest:
            raise ValueError("snapshot_hash mismatch")
        if self.snapshot_id and self.snapshot_id != expected_id:
            raise ValueError("snapshot_id mismatch")
        object.__setattr__(self, "snapshot_hash", digest)
        object.__setattr__(self, "snapshot_id", expected_id)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_index": self.task_index,
            "bank_version": self.bank_version,
            "bank_item_count": self.bank_item_count,
            "memories": [memory.to_dict() for memory in self.memories],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "snapshot_id": self.snapshot_id, "snapshot_hash": self.snapshot_hash}


def seal_bank_snapshot(*, task_id: str, task_index: int, bank_version: int, memories: tuple[SMPMemoryItem, ...] | list[SMPMemoryItem]) -> BankSnapshot:
    eligible = tuple(
        memory
        for memory in memories
        if memory.state in {MemoryState.ACTIVE, MemoryState.NARROW}
        and memory.source_task_id != task_id
        and memory.available_from_task_index <= task_index
    )
    return BankSnapshot(
        task_id=task_id,
        task_index=task_index,
        bank_version=bank_version,
        memories=eligible,
        bank_item_count=len(memories),
    )


@dataclass(frozen=True)
class CandidateScore:
    memory_id: str
    score: float
    contradiction_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.memory_id or not 0 <= self.score <= 1:
            raise ValueError("invalid candidate score")


@dataclass(frozen=True)
class GateFScore:
    score: float
    contradiction_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 1:
            raise ValueError("invalid Gate F score")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise ValueError(f"{field_name} must be a SHA-256 hash")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71


@dataclass(frozen=True)
class UtilityTrialAuthorization:
    authorization_id: str
    source_memory_id: str
    source_memory_hash: str
    target_task_id: str
    split: str
    experiment_id: str
    run_id: str
    manifest_hash: str
    bank_version: int
    router_hash: str
    profile_hash: str
    schema_hash: str
    checkpoint_hash: str
    condition: str
    seed: int
    run_index: int
    valid_through_run_index: int
    schema_version: str = "smp.utility_trial_authorization.v1"
    authorization_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "smp.utility_trial_authorization.v1":
            raise ValueError("unsupported utility trial authorization schema")
        if self.split not in {"dev-tune", "dev-cert"} or self.condition != "smp_utility_trial":
            raise ValueError("utility trials are dev-only and condition-specific")
        if not all((self.authorization_id, self.source_memory_id, self.target_task_id, self.experiment_id, self.run_id)):
            raise ValueError("utility trial authorization identity is incomplete")
        for name in ("source_memory_hash", "manifest_hash", "router_hash", "profile_hash", "schema_hash", "checkpoint_hash"):
            _require_hash(getattr(self, name), name)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.bank_version, self.seed, self.run_index, self.valid_through_run_index)):
            raise ValueError("utility trial numeric boundary is invalid")
        if self.run_index > self.valid_through_run_index:
            raise ValueError("utility trial authorization is expired")
        expected = canonical_hash(self.identity_payload())
        if self.authorization_hash and self.authorization_hash != expected:
            raise ValueError("utility trial authorization_hash mismatch")
        object.__setattr__(self, "authorization_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {key: _jsonable(value) for key, value in asdict(self).items() if key != "authorization_hash"}

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "authorization_hash": self.authorization_hash}


@dataclass(frozen=True)
class UtilityTrialSnapshot:
    task_id: str
    task_index: int
    bank_version: int
    memories: tuple[SMPMemoryItem, ...]
    authorization: UtilityTrialAuthorization
    bank_item_count: int = 1
    snapshot_id: str = ""
    snapshot_hash: str = ""

    def __post_init__(self) -> None:
        if self.task_id != self.authorization.target_task_id or self.bank_version != self.authorization.bank_version:
            raise ValueError("utility trial snapshot authorization binding mismatch")
        if len(self.memories) != 1 or self.bank_item_count != 1:
            raise ValueError("utility trial snapshot contains exactly one subject")
        memory = self.memories[0]
        if memory.state not in {MemoryState.CANDIDATE, MemoryState.SHADOW}:
            raise ValueError("utility trial subject must be candidate or shadow")
        if memory.memory_id != self.authorization.source_memory_id:
            raise ValueError("utility trial subject ID mismatch")
        if canonical_hash(memory.to_dict()) != self.authorization.source_memory_hash:
            raise ValueError("utility trial subject hash mismatch")
        if memory.source_task_id == self.task_id:
            raise ValueError("utility trial cannot replay memory on its source task")
        digest = canonical_hash(self.identity_payload())
        expected_id = "trial_snapshot_" + digest[7:27]
        if self.snapshot_hash and self.snapshot_hash != digest:
            raise ValueError("utility trial snapshot_hash mismatch")
        if self.snapshot_id and self.snapshot_id != expected_id:
            raise ValueError("utility trial snapshot_id mismatch")
        object.__setattr__(self, "snapshot_hash", digest)
        object.__setattr__(self, "snapshot_id", expected_id)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_index": self.task_index,
            "bank_version": self.bank_version,
            "bank_item_count": self.bank_item_count,
            "memories": [memory.to_dict() for memory in self.memories],
            "authorization_hash": self.authorization.authorization_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "authorization": self.authorization.to_dict(),
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
        }


@dataclass(frozen=True)
class OfficialOutcome:
    task_id: str
    evaluator_name: str
    evaluator_version: str
    evaluation_terminal: bool
    submission_status: SubmissionStatus
    patch_applied: bool
    resolved: bool
    fail_to_pass: Mapping[str, int]
    pass_to_pass: Mapping[str, int]
    normalized_failure_class: str | None
    prediction_hash: str
    trajectory_hash: str
    report_hash: str
    evaluation_timestamp: str
    schema_version: str = "smp.official_outcome.v2"
    outcome_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "smp.official_outcome.v2":
            raise ValueError("unsupported official outcome schema_version")
        if any(not isinstance(value, bool) for value in (self.evaluation_terminal, self.patch_applied, self.resolved)):
            raise ValueError("official outcome status fields must be boolean")
        if not isinstance(self.submission_status, SubmissionStatus):
            raise ValueError("official outcome submission_status is invalid")
        if self.normalized_failure_class is not None and not isinstance(self.normalized_failure_class, str):
            raise ValueError("normalized_failure_class must be null or string")
        if self.normalized_failure_class not in {
            None, "empty_submission", "materialization_failed",
            "fail_to_pass_failure", "pass_to_pass_regression",
        }:
            raise ValueError("unsupported normalized_failure_class")
        if not all((self.task_id, self.evaluator_name, self.evaluator_version, self.evaluation_timestamp)):
            raise ValueError("official outcome identity fields are required")
        for name in ("fail_to_pass", "pass_to_pass"):
            value = _jsonable(getattr(self, name))
            if set(value) != {"passed", "failed"} or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0
                for count in value.values()
            ):
                raise ValueError(f"{name} must contain non-negative passed/failed aggregates")
            object.__setattr__(self, name, value)
        for name in ("prediction_hash", "trajectory_hash", "report_hash"):
            _require_hash(getattr(self, name), name)
        if self.submission_status is not SubmissionStatus.MATERIAL_PATCH and (self.patch_applied or self.resolved):
            raise ValueError("non-material submission cannot be applied or resolved")
        if self.resolved and (not self.evaluation_terminal or not self.patch_applied):
            raise ValueError("resolved outcome must be terminal with an applied material patch")
        if self.submission_status is SubmissionStatus.EMPTY_PATCH and self.normalized_failure_class != "empty_submission":
            raise ValueError("empty submission requires empty_submission failure class")
        if self.submission_status is SubmissionStatus.MISSING and self.normalized_failure_class is not None:
            raise ValueError("missing submission cannot carry a failure class")
        if self.submission_status is SubmissionStatus.MATERIAL_PATCH and self.evaluation_terminal:
            if not self.patch_applied and self.normalized_failure_class != "materialization_failed":
                raise ValueError("unapplied material patch requires materialization_failed")
            if self.resolved and (
                self.normalized_failure_class is not None
                or self.fail_to_pass["failed"] != 0
                or self.pass_to_pass["failed"] != 0
            ):
                raise ValueError("resolved outcome requires zero official failures")
            if self.patch_applied and not self.resolved:
                expected_failure = (
                    "pass_to_pass_regression" if self.pass_to_pass["failed"]
                    else "fail_to_pass_failure" if self.fail_to_pass["failed"]
                    else None
                )
                if self.normalized_failure_class != expected_failure:
                    raise ValueError("applied unresolved outcome failure class mismatch")
        expected = canonical_hash(self.identity_payload())
        if self.outcome_hash and self.outcome_hash != expected:
            raise ValueError("official outcome_hash mismatch")
        object.__setattr__(self, "outcome_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {key: _jsonable(value) for key, value in asdict(self).items() if key != "outcome_hash"}

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "outcome_hash": self.outcome_hash}


@dataclass(frozen=True)
class UtilityCertificate:
    certificate_id: str
    subject_id: str
    applicable_schema_version: str
    applicable_router_version: str
    evaluation_split_or_stream_id: str
    paired_evaluation_protocol_id: str
    ordinary_utility_lower_bound: float
    ordinary_harm_upper_bound: float
    security_harm_upper_bound: float
    cost_overhead_upper_bound: float
    required_thresholds: Mapping[str, float]
    multiplicity_correction_metadata: Mapping[str, Any]
    sample_unit_definition: str
    independent_task_count: int
    issued_at: str
    test_only: bool
    production_linkage: Mapping[str, str] = ()
    checkpoint_subject_ids: tuple[str, ...] = ()
    report_hash: str = ""
    bootstrap_metadata: Mapping[str, Any] = ()
    penalties: Mapping[str, Any] = ()
    security_utility_lower_bound: float | None = None
    fixed_checkpoint_id: str = ""
    schema_version: str = "smp.utility_certificate.v1"
    certificate_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.test_only, bool):
            raise ValueError("utility certificate test_only must be boolean")
        object.__setattr__(self, "required_thresholds", _jsonable(self.required_thresholds))
        object.__setattr__(self, "multiplicity_correction_metadata", _jsonable(self.multiplicity_correction_metadata))
        object.__setattr__(self, "production_linkage", _jsonable(self.production_linkage))
        object.__setattr__(self, "bootstrap_metadata", _jsonable(self.bootstrap_metadata))
        object.__setattr__(self, "penalties", _jsonable(self.penalties))
        expected = canonical_hash(self.identity_payload())
        if self.certificate_hash and self.certificate_hash != expected:
            raise ValueError("utility certificate_hash mismatch")
        object.__setattr__(self, "certificate_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {key: _jsonable(value) for key, value in asdict(self).items() if key != "certificate_hash"}

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "certificate_hash": self.certificate_hash}

    def compatible(self, *, subject_id: str, schema_version: str, router_version: str, test_mode: bool) -> bool:
        thresholds = self.required_thresholds
        certification_text = canonical_json(
            {
                "split": self.evaluation_split_or_stream_id,
                "protocol": self.paired_evaluation_protocol_id,
                "multiplicity": self.multiplicity_correction_metadata,
            }
        ).lower()
        synthetic_marked = any(marker in certification_text for marker in ("synthetic", "fixture", "test-only", "test_only"))
        numeric = (
            self.ordinary_utility_lower_bound,
            self.ordinary_harm_upper_bound,
            self.security_harm_upper_bound,
            self.cost_overhead_upper_bound,
        )
        threshold_values_valid = isinstance(thresholds, Mapping) and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in thresholds.values()
        )
        return (
            self.schema_version == "smp.utility_certificate.v1"
            and all(
                (
                    self.certificate_id,
                    self.subject_id,
                    self.applicable_schema_version,
                    self.applicable_router_version,
                    self.evaluation_split_or_stream_id,
                    self.paired_evaluation_protocol_id,
                    self.sample_unit_definition,
                    self.issued_at,
                )
            )
            and self.subject_id == subject_id
            and self.applicable_schema_version == schema_version
            and self.applicable_router_version == router_version
            and isinstance(self.independent_task_count, int)
            and not isinstance(self.independent_task_count, bool)
            and self.independent_task_count > 0
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in numeric)
            and 0 <= self.ordinary_harm_upper_bound <= 1
            and 0 <= self.security_harm_upper_bound <= 1
            and self.cost_overhead_upper_bound >= 0
            and isinstance(thresholds, Mapping)
            and set(thresholds) == {
                "minimum_ordinary_utility",
                "maximum_ordinary_harm",
                "maximum_security_harm",
                "maximum_cost_overhead",
            }
            and threshold_values_valid
            and isinstance(self.multiplicity_correction_metadata, Mapping)
            and bool(self.multiplicity_correction_metadata)
            and (
                self.test_only
                or (
                    isinstance(self.production_linkage, Mapping)
                    and set(self.production_linkage) == {
                        "data_manifest_hash",
                        "code_hash",
                        "bank_hash",
                        "router_hash",
                        "schema_hash",
                        "evaluator_hash",
                    }
                    and all(_is_hash(value) for value in self.production_linkage.values())
                    and bool(self.checkpoint_subject_ids)
                    and self.subject_id in self.checkpoint_subject_ids
                    and bool(self.fixed_checkpoint_id)
                    and _is_hash(self.report_hash)
                    and isinstance(self.bootstrap_metadata, Mapping)
                    and bool(self.bootstrap_metadata)
                    and isinstance(self.penalties, Mapping)
                    and bool(self.penalties)
                    and isinstance(self.security_utility_lower_bound, (int, float))
                    and not isinstance(self.security_utility_lower_bound, bool)
                    and self.security_utility_lower_bound > 0
                )
            )
            and (test_mode or not self.test_only)
            and not (synthetic_marked and not self.test_only)
            and self.ordinary_utility_lower_bound > thresholds["minimum_ordinary_utility"]
            and self.ordinary_harm_upper_bound <= thresholds["maximum_ordinary_harm"]
            and self.security_harm_upper_bound <= thresholds["maximum_security_harm"]
            and self.cost_overhead_upper_bound <= thresholds["maximum_cost_overhead"]
            and self.certificate_hash == canonical_hash(self.identity_payload())
        )
