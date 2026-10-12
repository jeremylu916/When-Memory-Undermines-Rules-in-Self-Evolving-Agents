"""Evidence-grounded production candidate-draft extraction after official outcome."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from self_evolving_agent.smp_calibration import validate_signature_payload
from self_evolving_agent.smp_contracts import EvidenceEvent, MemoryExposure, OfficialOutcome, canonical_hash
from self_evolving_agent.smp_official_adapter import MemoryCandidateDraft
from self_evolving_agent.smp_protocol import CandidateValidationError, FrameworkInvariantError
from self_evolving_agent.smp_structured import StrictStructuredClient, safe_rejected_payload


@dataclass(frozen=True)
class PrescriptiveExposureDraft:
    mechanism_trigger: str
    action_guidance: str
    caution: str
    scope: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in asdict(self).values()):
            raise ValueError("prescriptive exposure fields must be non-empty strings")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class ProductionCandidateExtractor:
    def __init__(
        self,
        *,
        client: StrictStructuredClient,
    ):
        self.client = client

    @property
    def binding(self) -> Mapping[str, str]:
        return {
            "model_hash": canonical_hash({"model": self.client.model}),
            "config_hash": canonical_hash(self.client.binding_config),
            "prompt_hash": canonical_hash({"prompt": self.client.prompt}),
            "schema_hash": canonical_hash(self.client.schema),
        }

    def extract(
        self,
        *,
        evidence: tuple[EvidenceEvent, ...],
        outcome: OfficialOutcome,
    ) -> tuple[MemoryCandidateDraft, dict[str, Any]]:
        if not evidence or any(event.task_id != outcome.task_id for event in evidence):
            raise FrameworkInvariantError("candidate_evidence_task_linkage_invalid")
        visible = [event.to_dict() for event in evidence if event.observable]
        allowed_ref_set_hash = canonical_hash(sorted(event["event_id"] for event in visible))
        call = self.client(
            {
                "signature_version": "smp.signature.v1",
                "evidence": visible,
                "official_outcome": {
                    "outcome_hash": outcome.outcome_hash,
                    "evaluation_terminal": outcome.evaluation_terminal,
                    "submission_status": outcome.submission_status.value,
                    "patch_applied": outcome.patch_applied,
                    "resolved": outcome.resolved,
                    "fail_to_pass": outcome.fail_to_pass,
                    "pass_to_pass": outcome.pass_to_pass,
                    "normalized_failure_class": outcome.normalized_failure_class,
                },
                "required_output": {"signature": "typed signature fields", "exposure": "four bounded prescriptive fields"},
            }
        )
        result = call.payload
        call_metadata = {
            "raw_response_hash": call.raw_response_hash,
            "request_binding_hash": call.request_binding_hash,
            "provider_request_id_hash": call.provider_request_id_hash,
            "schema_hash": call.schema_hash,
            "model_hash": call.model_hash,
            "config_hash": call.config_hash,
            "prompt_hash": call.prompt_hash,
            "retry_count": call.retry_count,
            "retry_policy_hash": call.retry_policy_hash,
            "usage": asdict(call.usage),
        }
        if not isinstance(result.get("signature"), Mapping):
            raise CandidateValidationError(
                "candidate_signature_value_type_invalid", validation_status="invalid",
                allowed_evidence_ref_set_hash=allowed_ref_set_hash,
                validator_version="openmemgov.candidate_validator.v5",
                **safe_rejected_payload(result), structured_call=call_metadata,
            )
        if not isinstance(result.get("exposure"), Mapping):
            raise CandidateValidationError(
                "candidate_exposure_invalid", validation_status="invalid",
                allowed_evidence_ref_set_hash=allowed_ref_set_hash,
                validator_version="openmemgov.candidate_validator.v5",
                **safe_rejected_payload(result), structured_call=call_metadata,
            )
        try:
            signature = validate_signature_payload(result["signature"], evidence)
        except CandidateValidationError as exc:
            validation_details = {
                **exc.details,
                "validation_status": "invalid",
                "validator_version": "openmemgov.candidate_validator.v5",
                **safe_rejected_payload(result),
                "structured_call": call_metadata,
            }
            raise CandidateValidationError(
                exc.error_code,
                **validation_details,
            ) from exc
        exposure_row = result["exposure"]
        try:
            if set(exposure_row) != {"mechanism_trigger", "action_guidance", "caution", "scope"}:
                raise CandidateValidationError("candidate_exposure_invalid")
            model_draft = PrescriptiveExposureDraft(**dict(exposure_row))
        except CandidateValidationError as exc:
            validation_details = {
                **exc.details,
                "validation_status": "invalid",
                "validator_version": "openmemgov.candidate_validator.v5",
                **safe_rejected_payload(result),
                "structured_call": call_metadata,
            }
            raise CandidateValidationError(
                exc.error_code,
                **validation_details,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise CandidateValidationError(
                "candidate_exposure_invalid",
                allowed_evidence_ref_set_hash=allowed_ref_set_hash,
                validator_version="openmemgov.candidate_validator.v5",
                **safe_rejected_payload(result), structured_call=call_metadata,
            ) from exc
        exposure = MemoryExposure(**model_draft.to_dict(), provenance_class="sanitized_official")
        try:
            exposure.render("candidate")
        except ValueError as exc:
            raise CandidateValidationError(
                "candidate_exposure_unsafe", validation_status="invalid",
                allowed_evidence_ref_set_hash=allowed_ref_set_hash,
                validator_version="openmemgov.candidate_validator.v5",
                **safe_rejected_payload(result), structured_call=call_metadata,
            ) from exc
        draft = MemoryCandidateDraft(signature=signature, exposure=exposure)
        artifact = {
            "schema_version": "openmemgov.candidate_draft.v5",
            "outcome_hash": outcome.outcome_hash,
            "evidence_hash": canonical_hash(visible),
            "extractor_binding": dict(self.binding),
            "allowed_evidence_ref_set_hash": allowed_ref_set_hash,
            "validator_version": "openmemgov.candidate_validator.v5",
            "validation_status": "valid",
            "structured_call": call_metadata,
            "signature": signature.to_dict(),
            "exposure": asdict(exposure),
        }
        artifact["draft_hash"] = canonical_hash(artifact)
        return draft, artifact
