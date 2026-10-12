"""Default-safe, iterative SMP routing state for one solver task."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from self_evolving_agent.smp_contracts import (
    BankSnapshot,
    CandidateScore,
    EvidenceEvent,
    EvidenceSource,
    EvidenceType,
    FieldStatus,
    FrozenCalibrationProfile,
    GateFScore,
    GateMode,
    LeakageClass,
    MechanismSignature,
    MEMORY_SCHEMA_VERSION,
    MemoryState,
    SMPMemoryItem,
    TriggerCategory,
    UnknownReason,
    UtilityTrialSnapshot,
    canonical_hash,
)
from self_evolving_agent.smp_protocol import CandidateValidationError, FrameworkInvariantError, StructuredExtractionError


SignatureExtractor = Callable[[tuple[EvidenceEvent, ...]], MechanismSignature]
GateFScorer = Callable[[MechanismSignature, tuple[EvidenceEvent, ...], FrozenCalibrationProfile], GateFScore]
GateRScorer = Callable[[MechanismSignature, tuple[SMPMemoryItem, ...], FrozenCalibrationProfile], list[CandidateScore]]
TokenCounter = Callable[[str], int]


@dataclass(frozen=True)
class SMPBudgets:
    routing_checks: int = 4
    exposures: int = 3
    tokens_per_exposure: int = 350
    cumulative_visible_tokens: int = 1050

    def __post_init__(self) -> None:
        if (self.routing_checks, self.exposures, self.tokens_per_exposure, self.cumulative_visible_tokens) != (4, 3, 350, 1050):
            raise ValueError("the minimal SMP slice uses fixed default budgets")


@dataclass(frozen=True)
class PendingExposure:
    memory_id: str
    block: str
    token_count: int
    evidence_hash: str
    observable_evidence_hash: str
    signature_hash: str
    failure_state_hash: str
    trigger: TriggerCategory


@dataclass
class SMPTaskState:
    task_id: str
    snapshot: BankSnapshot | UtilityTrialSnapshot
    evidence: list[EvidenceEvent] = field(default_factory=list)
    routing_checks_used: int = 0
    exposures_used: int = 0
    visible_memory_tokens_used: int = 0
    pending_ephemeral_memory: PendingExposure | None = None
    last_signature_hash: str = ""
    last_relevant_failure_state_hash: str = ""
    exposed_memory_ids: list[str] = field(default_factory=list)
    exposure_links: list[dict[str, Any]] = field(default_factory=list)
    solver_done: bool = False
    trajectory_sealed: bool = False
    prediction_sealed: bool = False
    trajectory_hash: str = ""
    prediction_hash: str = ""
    audit_events: list[dict[str, Any]] = field(default_factory=list)


class SMPRuntime:
    def __init__(
        self,
        *,
        task_id: str,
        snapshot: BankSnapshot | UtilityTrialSnapshot,
        domain: str,
        gate_f_mode: GateMode = GateMode.OFF,
        gate_r_mode: GateMode = GateMode.OFF,
        calibration_profile: FrozenCalibrationProfile | None = None,
        signature_scorer_version: str = "",
        retrieval_scorer_version: str = "",
        extractor: SignatureExtractor | None = None,
        gate_f_scorer: GateFScorer | None = None,
        gate_r_scorer: GateRScorer | None = None,
        token_counter: TokenCounter | None = None,
        test_mode: bool = False,
        budgets: SMPBudgets = SMPBudgets(),
    ):
        if snapshot.task_id != task_id:
            raise ValueError("snapshot belongs to another task")
        self.state = SMPTaskState(task_id=task_id, snapshot=snapshot)
        self.domain = domain
        self.gate_f_mode = GateMode(gate_f_mode)
        self.gate_r_mode = GateMode(gate_r_mode)
        self.profile = calibration_profile
        self.signature_scorer_version = signature_scorer_version
        self.retrieval_scorer_version = retrieval_scorer_version
        self.extractor = extractor
        self.gate_f_scorer = gate_f_scorer
        self.gate_r_scorer = gate_r_scorer
        self.token_counter = token_counter
        self.test_mode = test_mode
        self.budgets = budgets
        self._trigger_keys: set[str] = set()
        self._last_exposure_by_memory: dict[str, PendingExposure] = {}
        self._audit("task_initialized", task_id=task_id)
        self._audit("bank_snapshot_sealed", snapshot_id=snapshot.snapshot_id, snapshot_hash=snapshot.snapshot_hash)
        if isinstance(snapshot, UtilityTrialSnapshot):
            self._audit(
                "utility_trial_authorized",
                authorization_id=snapshot.authorization.authorization_id,
                authorization_hash=snapshot.authorization.authorization_hash,
                condition=snapshot.authorization.condition,
            )

    def _audit(self, event_type: str, **fields: Any) -> None:
        safe = {key: deepcopy(value) for key, value in fields.items() if key not in {"payload", "thought", "raw_bank", "block"}}
        record = {"index": len(self.state.audit_events), "event_type": event_type, **safe}
        record["event_hash"] = canonical_hash(record)
        self.state.audit_events.append(record)

    @property
    def evidence_hash(self) -> str:
        return canonical_hash([event.to_dict() for event in self.state.evidence])

    @property
    def observable_evidence_hash(self) -> str:
        return canonical_hash(
            [
                event.to_dict()
                for event in self.state.evidence
                if event.observable and event.source is not EvidenceSource.MODEL_THOUGHT
            ]
        )

    def add_initial_evidence(self, visible_task_input: str) -> EvidenceEvent:
        event = EvidenceEvent(
            task_id=self.state.task_id,
            event_index=len(self.state.evidence),
            event_type=EvidenceType.TASK_INPUT,
            source=EvidenceSource.PUBLIC_CONTEXT,
            observable=True,
            leakage_class=LeakageClass.PUBLIC_TASK,
            payload={"visible_task_input": visible_task_input},
        )
        self.record_evidence(event)
        self._audit("initial_evidence_normalized", evidence_id=event.event_id, evidence_hash=self.evidence_hash)
        return event

    def record_evidence(self, event: EvidenceEvent) -> None:
        if self.state.solver_done:
            raise RuntimeError("completed task cannot receive new solver evidence")
        if event.task_id != self.state.task_id or event.event_index != len(self.state.evidence):
            raise ValueError("evidence task/index mismatch")
        self.state.evidence.append(event)

    def record_adapter_failure(self, *, error_type: str) -> None:
        """Record a sanitized adapter failure without interrupting the solver."""
        self._audit("trace_adapter_error", error_type=error_type)
        self._unknown(UnknownReason.CONTROLLER_ERROR, TriggerCategory.LOCALIZATION_MILESTONE)

    def consider_trigger(self, trigger: TriggerCategory, *, failure_state_hash: str = "") -> str | None:
        trigger = TriggerCategory(trigger)
        self._audit("trigger_checked", trigger=trigger.value, evidence_hash=self.evidence_hash)
        key = trigger.value
        if trigger in {TriggerCategory.DIAGNOSTIC_TEST_FAILURE, TriggerCategory.PATCH_APPLICATION_FAILURE}:
            key += ":" + failure_state_hash
        if key in self._trigger_keys:
            self._audit("trigger_skipped", trigger=trigger.value, reason="not_materially_changed")
            return None
        self._trigger_keys.add(key)
        self._audit("trigger_accepted", trigger=trigger.value)
        try:
            return self.route(trigger=trigger, failure_state_hash=failure_state_hash)
        except (CandidateValidationError, StructuredExtractionError) as exc:
            self._audit(
                "controller_error",
                trigger=trigger.value,
                error_type=type(exc).__name__,
            )
            return self._unknown(UnknownReason.CONTROLLER_ERROR, trigger)
        except FrameworkInvariantError:
            raise
        except (TypeError, ValueError) as exc:
            raise FrameworkInvariantError(
                "unexpected_online_controller_implementation_error",
                implementation_error_class=type(exc).__name__,
            ) from exc
        except Exception as exc:
            raise FrameworkInvariantError(
                "unexpected_online_controller_failure",
                implementation_error_class=type(exc).__name__,
            ) from exc

    def route(self, *, trigger: TriggerCategory, failure_state_hash: str = "") -> str | None:
        if self.state.solver_done:
            raise RuntimeError("completed task cannot route")
        if self.gate_f_mode is GateMode.OFF or self.gate_r_mode is GateMode.OFF:
            return self._unknown(UnknownReason.GATE_OFF, trigger)
        if self.state.routing_checks_used >= self.budgets.routing_checks:
            self._audit("budget_exhausted", budget="routing")
            return self._unknown(UnknownReason.ROUTING_BUDGET_EXHAUSTED, trigger)
        self.state.routing_checks_used += 1
        if not self.state.snapshot.memories:
            return self._unknown(
                UnknownReason.BANK_EMPTY if self.state.snapshot.bank_item_count == 0 else UnknownReason.NO_ACTIVE_MEMORY,
                trigger,
            )
        if not self._profile_valid():
            return self._unknown(UnknownReason.INVALID_CALIBRATION_PROFILE, trigger)
        if not (self.extractor and self.gate_f_scorer and self.gate_r_scorer):
            return self._unknown(UnknownReason.INVALID_CALIBRATION_PROFILE, trigger)

        evidence = tuple(self.state.evidence)
        signature = self.extractor(evidence)
        self.state.last_signature_hash = signature.signature_hash
        self.state.last_relevant_failure_state_hash = failure_state_hash
        self._audit("signature_proposed", signature_hash=signature.signature_hash, evidence_refs=list(signature.evidence_refs))
        valid_ids = {event.event_id for event in evidence if event.observable and event.source is not EvidenceSource.MODEL_THOUGHT}
        invalid_refs = sorted(set(signature.evidence_refs) - valid_ids)
        if signature.critical_unknowns:
            return self._gate_f_unknown(signature, 0.0, UnknownReason.CRITICAL_SIGNATURE_UNKNOWN, invalid_refs, trigger)
        if invalid_refs or not signature.evidence_refs:
            return self._gate_f_unknown(signature, 0.0, UnknownReason.EVIDENCE_CONTRADICTION, invalid_refs, trigger)
        f_result = self.gate_f_scorer(signature, evidence, self.profile)
        if not isinstance(f_result, GateFScore):
            raise TypeError("Gate F scorer must return GateFScore")
        f_score = f_result.score
        if f_result.contradiction_flags:
            return self._gate_f_unknown(
                signature,
                f_score,
                UnknownReason.EVIDENCE_CONTRADICTION,
                list(f_result.contradiction_flags),
                trigger,
            )
        f_pass = f_score >= self.profile.tau_f
        self._audit(
            "gate_f_result",
            proposed_score=f_score,
            effective_decision=f_pass and self.gate_f_mode is GateMode.ENFORCE,
            required_field_coverage=sum(not getattr(signature, name).status is FieldStatus.UNKNOWN for name in ("ownership", "operation", "observed_violation", "required_relation")) / 4,
            critical_unknowns=list(signature.critical_unknowns),
            contradiction_flags=[],
            evidence_refs=list(signature.evidence_refs),
            profile_id=self.profile.artifact_id,
            profile_hash=self.profile.artifact_hash,
            reason_code="pass" if f_pass else UnknownReason.GATE_F_LOW_CONFIDENCE.value,
        )
        if not f_pass:
            return self._unknown(UnknownReason.GATE_F_LOW_CONFIDENCE, trigger)

        eligible_memories = tuple(
            memory
            for memory in self.state.snapshot.memories
            if memory.state is not MemoryState.NARROW
            or (memory.narrow_scope is not None and memory.narrow_scope.matches(signature))
        )
        scores = sorted(self.gate_r_scorer(signature, eligible_memories, self.profile), key=lambda row: (-row.score, row.memory_id))
        eligible_ids = {memory.memory_id for memory in eligible_memories}
        scores = [score for score in scores if score.memory_id in eligible_ids]
        p1 = scores[0].score if scores else 0.0
        p2 = scores[1].score if len(scores) > 1 else None
        selected = scores[0] if scores else None
        reason = None
        if not selected or p1 < self.profile.tau_r:
            reason = UnknownReason.GATE_R_LOW_MATCH
        elif p2 is not None and p1 - p2 < self.profile.gamma:
            reason = UnknownReason.AMBIGUOUS_TOP_CANDIDATES
        elif selected.contradiction_flags:
            reason = UnknownReason.TYPED_CONTRADICTION
        shadow = self.gate_f_mode is GateMode.SHADOW or self.gate_r_mode is GateMode.SHADOW
        self._audit(
            "gate_r_result",
            ranked_candidate_ids=[score.memory_id for score in scores],
            p1=p1,
            p2=p2,
            ambiguity_margin=None if p2 is None else p1 - p2,
            contradiction_flags=list(selected.contradiction_flags) if selected else [],
            selected_top1_id=selected.memory_id if selected and reason is None else None,
            effective_decision=bool(selected and reason is None and not shadow),
            reason_code=(UnknownReason.SHADOW_MODE.value if shadow and reason is None else reason.value if reason else "pass"),
            snapshot_id=self.state.snapshot.snapshot_id,
            snapshot_hash=self.state.snapshot.snapshot_hash,
            profile_id=self.profile.artifact_id,
            profile_hash=self.profile.artifact_hash,
        )
        if reason:
            return self._unknown(reason, trigger)
        if shadow:
            return self._unknown(UnknownReason.SHADOW_MODE, trigger)
        memory = next(item for item in eligible_memories if item.memory_id == selected.memory_id)
        return self._schedule(memory, signature, trigger, failure_state_hash)

    def _gate_f_unknown(
        self,
        signature: MechanismSignature,
        score: float,
        reason: UnknownReason,
        invalid_refs: list[str],
        trigger: TriggerCategory,
    ) -> None:
        self._audit(
            "gate_f_result",
            proposed_score=score,
            effective_decision=False,
            required_field_coverage=sum(
                not getattr(signature, name).status is FieldStatus.UNKNOWN
                for name in ("ownership", "operation", "observed_violation", "required_relation")
            ) / 4,
            critical_unknowns=list(signature.critical_unknowns),
            contradiction_flags=invalid_refs,
            evidence_refs=list(signature.evidence_refs),
            profile_id=self.profile.artifact_id,
            profile_hash=self.profile.artifact_hash,
            reason_code=reason.value,
        )
        return self._unknown(reason, trigger)

    def _profile_valid(self) -> bool:
        return bool(
            self.profile
            and self.profile.compatible(
                domain=self.domain,
                memory_schema_version=MEMORY_SCHEMA_VERSION,
                test_mode=self.test_mode,
            )
            and self.signature_scorer_version == self.profile.signature_scorer_version
            and self.retrieval_scorer_version == self.profile.retrieval_scorer_version
        )

    def _schedule(self, memory: SMPMemoryItem, signature: MechanismSignature, trigger: TriggerCategory, failure_state_hash: str) -> str | None:
        if self.state.exposures_used >= self.budgets.exposures:
            self._audit("budget_exhausted", budget="exposure")
            return self._unknown(UnknownReason.EXPOSURE_BUDGET_EXHAUSTED, trigger)
        block = memory.exposure.render(memory.memory_id)
        if self.token_counter is None:
            self._audit("budget_exhausted", budget="tokens", reason="validated_counter_missing")
            return self._unknown(UnknownReason.TOKEN_BUDGET_EXHAUSTED, trigger)
        token_count = self.token_counter(block)
        if (
            isinstance(token_count, bool)
            or not isinstance(token_count, int)
            or token_count < 0
            or token_count > self.budgets.tokens_per_exposure
            or self.state.visible_memory_tokens_used + token_count > self.budgets.cumulative_visible_tokens
        ):
            self._audit("budget_exhausted", budget="tokens", token_count=token_count)
            return self._unknown(UnknownReason.TOKEN_BUDGET_EXHAUSTED, trigger)
        pending = PendingExposure(
            memory_id=memory.memory_id,
            block=block,
            token_count=token_count,
            evidence_hash=self.evidence_hash,
            observable_evidence_hash=self.observable_evidence_hash,
            signature_hash=signature.signature_hash,
            failure_state_hash=failure_state_hash,
            trigger=trigger,
        )
        previous = self._last_exposure_by_memory.get(memory.memory_id)
        if previous and not (
            previous.observable_evidence_hash != pending.observable_evidence_hash
            and (previous.signature_hash != pending.signature_hash or previous.failure_state_hash != pending.failure_state_hash)
        ):
            return self._unknown(UnknownReason.DUPLICATE_WITHOUT_EVIDENCE_CHANGE, trigger)
        self.state.pending_ephemeral_memory = pending
        self._audit(
            "exposure_scheduled",
            memory_id=memory.memory_id,
            evidence_hash=pending.evidence_hash,
            signature_hash=pending.signature_hash,
            snapshot_id=self.state.snapshot.snapshot_id,
            token_count=token_count,
        )
        return memory.memory_id

    def attach_pending_to_query(
        self,
        canonical_messages: list[dict[str, Any]],
        query_index: int,
        *,
        memory_message: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        messages = deepcopy(canonical_messages)
        pending = self.state.pending_ephemeral_memory
        if pending is None:
            return messages
        messages.append(deepcopy(memory_message) if memory_message is not None else {"role": "system", "content": pending.block})
        self.state.exposures_used += 1
        self.state.visible_memory_tokens_used += pending.token_count
        self.state.exposed_memory_ids.append(pending.memory_id)
        link = {
            "task_id": self.state.task_id,
            "query_index": query_index,
            "evidence_hash": pending.evidence_hash,
            "signature_hash": pending.signature_hash,
            "memory_id": pending.memory_id,
            "snapshot_id": self.state.snapshot.snapshot_id,
            "token_count": pending.token_count,
        }
        self.state.exposure_links.append(link)
        self._last_exposure_by_memory[pending.memory_id] = pending
        self._audit("exposure_attached_to_query", **link)
        return messages

    def clear_pending(self) -> None:
        pending = self.state.pending_ephemeral_memory
        if pending is None:
            return
        self.state.pending_ephemeral_memory = None
        self._audit("exposure_cleared", memory_id=pending.memory_id)

    def terminate(self, *, event: EvidenceEvent | None = None) -> None:
        if event is not None:
            self.record_evidence(event)
        self.state.solver_done = True
        self._audit("solver_terminated", evidence_hash=self.evidence_hash)

    def seal_prediction(self, prediction: Any) -> str:
        if not self.state.solver_done:
            raise RuntimeError("prediction cannot be sealed before solver termination")
        if self.state.prediction_sealed:
            raise RuntimeError("prediction is already sealed")
        self.state.prediction_hash = canonical_hash(prediction)
        self.state.prediction_sealed = True
        self._audit("prediction_sealed", prediction_hash=self.state.prediction_hash)
        return self.state.prediction_hash

    def seal_trajectory(self, trajectory: Any) -> str:
        if not self.state.solver_done:
            raise RuntimeError("trajectory cannot be sealed before solver termination")
        if self.state.trajectory_sealed:
            raise RuntimeError("trajectory is already sealed")
        self.state.trajectory_hash = canonical_hash(trajectory)
        self.state.trajectory_sealed = True
        self._audit("trajectory_sealed", trajectory_hash=self.state.trajectory_hash)
        return self.state.trajectory_hash

    def _unknown(self, reason: UnknownReason, trigger: TriggerCategory) -> None:
        self.state.pending_ephemeral_memory = None
        self._audit("unknown_decision", reason_code=reason.value, trigger=trigger.value, evidence_hash=self.evidence_hash)
        return None

    def audit_sidecar(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(self.state.audit_events))
