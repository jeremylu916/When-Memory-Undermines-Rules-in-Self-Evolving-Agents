"""Production signature bindings and deterministic Gate F/R calibration tooling."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence

from self_evolving_agent.smp_contracts import (
    CandidateScore,
    EvidenceEvent,
    EvidenceSource,
    FieldStatus,
    FrozenCalibrationProfile,
    GateFScore,
    MechanismSignature,
    SIGNATURE_FIELDS,
    SIGNATURE_VERSION,
    SignatureField,
    SMPMemoryItem,
    canonical_hash,
)
from self_evolving_agent.smp_protocol import CandidateValidationError
from self_evolving_agent.smp_structured import StrictStructuredClient


GATE_F_FEATURES = (
    "required_field_coverage",
    "observable_event_ratio",
    "evidence_reference_coverage",
    "one_minus_critical_unknown_rate",
)
GATE_R_FEATURES = (
    "ownership",
    "operation",
    "observed_violation",
    "required_relation",
    "preconditions",
    "code_structure",
    "semantic",
)
SHADOW_GATE_R_WEIGHTS = (0.10, 0.20, 0.25, 0.25, 0.10, 0.05, 0.05)


class LiteLLMTokenCounter:
    """Validated accounting on the same model path; absence fails closed."""

    def __init__(self, model_name: str):
        if not model_name:
            raise ValueError("token counter model_name is required")
        self.model_name = model_name

    def __call__(self, text: str) -> int:
        try:
            from litellm import token_counter
        except ImportError as exc:  # pragma: no cover - exercised by preflight
            raise RuntimeError("litellm token accounting is unavailable") from exc
        value = token_counter(model=self.model_name, text=text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("model token accounting returned an invalid count")
        return value


class LiteLLMRequestTokenCounter:
    """Count a complete model-bound chat request for the solver cost guard."""

    def __init__(self, model_name: str):
        if not model_name:
            raise ValueError("request token counter model_name is required")
        self.model_name = model_name

    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> int:
        if model != self.model_name:
            raise RuntimeError("request token counter model binding mismatch")
        try:
            from litellm import token_counter
        except ImportError as exc:  # pragma: no cover - exercised by preflight
            raise RuntimeError("litellm request token accounting is unavailable") from exc
        value = token_counter(model=self.model_name, messages=messages, tools=tools)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("model request token accounting returned an invalid count")
        return value


@dataclass(frozen=True)
class ExtractorBinding:
    model_hash: str
    config_hash: str
    prompt_hash: str
    schema_hash: str
    version: str


class StructuredSignatureExtractor:
    """Schema-constrained adapter around a frozen structured-output generator."""

    def __init__(
        self,
        *,
        client: StrictStructuredClient,
    ):
        self.client = client
        schema_hash = canonical_hash(client.schema)
        self.binding = ExtractorBinding(
            model_hash=canonical_hash({"model": client.model}),
            config_hash=canonical_hash(client.binding_config),
            prompt_hash=canonical_hash({"prompt": client.prompt}),
            schema_hash=schema_hash,
            version="smp.extractor." + canonical_hash(
                {"model": client.model, "config": client.binding_config, "prompt": client.prompt, "schema": schema_hash}
            )[7:23],
        )
        self.prompt = client.prompt

    def __call__(self, evidence: tuple[EvidenceEvent, ...]) -> MechanismSignature:
        visible = [
            event.to_dict()
            for event in evidence
            if event.observable and event.source is not EvidenceSource.MODEL_THOUGHT
        ]
        result = self.client({"signature_version": SIGNATURE_VERSION, "evidence": visible})
        payload = result.payload
        return validate_signature_payload(payload, evidence)


def validate_signature_payload(payload: Mapping[str, Any], evidence: tuple[EvidenceEvent, ...]) -> MechanismSignature:
        visible = [
            event.to_dict()
            for event in evidence
            if event.observable and event.source is not EvidenceSource.MODEL_THOUGHT
        ]
        if not isinstance(payload, Mapping):
            raise CandidateValidationError("candidate_signature_value_type_invalid", field="signature")
        valid_refs = {event["event_id"] for event in visible}
        if set(payload) != set(SIGNATURE_FIELDS):
            raise CandidateValidationError("candidate_signature_value_type_invalid", field="signature")
        fields: dict[str, SignatureField] = {}
        for name in SIGNATURE_FIELDS:
            row = payload.get(name)
            if not isinstance(row, Mapping) or set(row) != {"value", "status", "evidence_refs"}:
                raise CandidateValidationError("candidate_signature_value_type_invalid", field=name)
            raw_status = row.get("status")
            if not isinstance(raw_status, str):
                raise CandidateValidationError("candidate_signature_status_invalid", field=name)
            try:
                status = FieldStatus(raw_status)
            except ValueError as exc:
                raise CandidateValidationError("candidate_signature_status_invalid", field=name) from exc
            raw_value = row.get("value")
            raw_refs = row.get("evidence_refs")
            if not isinstance(raw_refs, list) or any(not isinstance(ref, str) or not ref for ref in raw_refs):
                raise CandidateValidationError("candidate_signature_evidence_ref_invalid", field=name)
            refs = tuple(raw_refs)
            if name == "preconditions":
                if raw_value is not None and (
                    not isinstance(raw_value, list)
                    or any(not isinstance(value, str) or not value for value in raw_value)
                ):
                    raise CandidateValidationError("candidate_signature_value_type_invalid", field=name)
                value = tuple(raw_value) if isinstance(raw_value, list) else None
            else:
                if raw_value is not None and (not isinstance(raw_value, str) or not raw_value):
                    raise CandidateValidationError("candidate_signature_value_type_invalid", field=name)
                value = raw_value
            if status is FieldStatus.UNKNOWN and (value is not None or refs):
                raise CandidateValidationError("candidate_signature_status_value_incoherent", field=name)
            if status is not FieldStatus.UNKNOWN and value in (None, "", ()):
                raise CandidateValidationError("candidate_signature_status_value_incoherent", field=name)
            if status is not FieldStatus.UNKNOWN and (not refs or not set(refs).issubset(valid_refs)):
                safe_hashes = [canonical_hash(ref) for ref in refs][:24]
                raise CandidateValidationError(
                    "candidate_signature_evidence_ref_invalid",
                    field=name,
                    allowed_evidence_ref_set_hash=canonical_hash(sorted(valid_refs)),
                    safe_submitted_ref_hashes=safe_hashes,
                    validator_version="openmemgov.signature_validator.v5",
                )
            fields[name] = SignatureField(value, status, refs)
        return MechanismSignature(**fields)


def gate_f_features(signature: MechanismSignature, evidence: tuple[EvidenceEvent, ...]) -> tuple[float, ...]:
    visible = [event for event in evidence if event.observable and event.source is not EvidenceSource.MODEL_THOUGHT]
    valid = {event.event_id for event in visible}
    refs = signature.evidence_refs
    return (
        sum(getattr(signature, name).status is not FieldStatus.UNKNOWN for name in SIGNATURE_FIELDS[:4]) / 4,
        len(visible) / max(1, len(evidence)),
        sum(ref in valid for ref in refs) / max(1, len(refs)),
        1 - len(signature.critical_unknowns) / 4,
    )


def production_gate_f_scorer(
    signature: MechanismSignature,
    evidence: tuple[EvidenceEvent, ...],
    profile: FrozenCalibrationProfile,
) -> GateFScore:
    weights = tuple(float(value) for value in profile.certification_metadata.get("gate_f_weights", ()))
    if len(weights) != len(GATE_F_FEATURES) or any(value < 0 for value in weights):
        raise ValueError("calibrated Gate F weights are invalid")
    features = gate_f_features(signature, evidence)
    denominator = sum(weights) or 1.0
    return GateFScore(sum(weight * feature for weight, feature in zip(weights, features)) / denominator)


def _field_similarity(left: SignatureField, right: SignatureField) -> float:
    if left.status is FieldStatus.UNKNOWN or right.status is FieldStatus.UNKNOWN:
        return 0.0
    if isinstance(left.value, tuple) or isinstance(right.value, tuple):
        a = set(left.value if isinstance(left.value, tuple) else (left.value,))
        b = set(right.value if isinstance(right.value, tuple) else (right.value,))
        return len(a & b) / max(1, len(a | b))
    return float(left.value == right.value)


def production_gate_r_scorer(
    signature: MechanismSignature,
    memories: tuple[SMPMemoryItem, ...],
    profile: FrozenCalibrationProfile,
) -> list[CandidateScore]:
    weights = tuple(float(value) for value in profile.certification_metadata.get("gate_r_weights", ()))
    if len(weights) != len(GATE_R_FEATURES) or any(value < 0 for value in weights):
        raise ValueError("calibrated Gate R weights are invalid")
    denominator = sum(weights) or 1.0
    scores: list[CandidateScore] = []
    for memory in memories:
        typed = tuple(
            _field_similarity(getattr(signature, name), getattr(memory.signature, name))
            for name in ("ownership", "operation", "observed_violation", "required_relation", "preconditions")
        )
        features = (*typed, 0.0, 0.0)
        scores.append(CandidateScore(memory.memory_id, sum(w * f for w, f in zip(weights, features)) / denominator))
    return scores


def clopper_pearson_upper(errors: int, total: int, *, alpha: float) -> float:
    if total <= 0 or errors < 0 or errors > total or not 0 < alpha < 1:
        raise ValueError("invalid binomial certification inputs")
    if errors == total:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(100):
        midpoint = (low + high) / 2
        cdf = sum(
            math.comb(total, index) * midpoint**index * (1 - midpoint) ** (total - index)
            for index in range(errors + 1)
        )
        if cdf > alpha:
            low = midpoint
        else:
            high = midpoint
    return high


def fit_nonnegative_weights(rows: Sequence[Mapping[str, Any]], *, feature_names: tuple[str, ...]) -> tuple[float, ...]:
    if not rows:
        raise ValueError("training records are required")
    positives = [row for row in rows if bool(row.get("label"))]
    negatives = [row for row in rows if not bool(row.get("label"))]
    if not positives or not negatives:
        raise ValueError("weight fitting requires positive and negative records")
    weights = []
    for name in feature_names:
        positive = sum(_validated_features(row, feature_names, "features")[feature_names.index(name)] for row in positives) / len(positives)
        negative = sum(_validated_features(row, feature_names, "features")[feature_names.index(name)] for row in negatives) / len(negatives)
        weights.append(max(0.0, positive - negative))
    if not any(weights):
        raise ValueError("training records produce no nonnegative discrimination")
    total = sum(weights)
    return tuple(value / total for value in weights)


def _validate_disjoint(*groups: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for group in groups:
        identifiers = {str(row.get("record_id")) for row in group}
        if "None" in identifiers or len(identifiers) != len(group) or seen & identifiers:
            raise ValueError("calibration records overlap or lack unique record_id")
        seen |= identifiers


def _validate_record_contract(
    rows: Sequence[Mapping[str, Any]],
    *,
    split: str,
    schema_hash: str,
    manifest_hash: str | None = None,
) -> None:
    for row in rows:
        if row.get("split") != split or row.get("schema_hash") != schema_hash:
            raise ValueError(f"calibration record split/schema mismatch for {split}")
        if manifest_hash is not None and row.get("manifest_hash") != manifest_hash:
            raise ValueError(f"calibration record manifest mismatch for {split}")


def _validated_features(row: Mapping[str, Any], names: tuple[str, ...], key: str) -> tuple[float, ...]:
    raw = row.get(key)
    if not isinstance(raw, Mapping) or set(raw) != set(names):
        raise ValueError(f"calibration record {key} must exactly match the frozen feature schema")
    values = tuple(float(raw[name]) for name in names)
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in values):
        raise ValueError(f"calibration record {key} values must be finite in [0, 1]")
    return values


def _weighted_score(features: tuple[float, ...], weights: tuple[float, ...]) -> float:
    denominator = sum(weights)
    if denominator <= 0:
        raise ValueError("calibration scorer weights have no positive mass")
    return sum(weight * feature for weight, feature in zip(weights, features)) / denominator


def _score_gate_f_records(
    rows: Sequence[Mapping[str, Any]], weights: tuple[float, ...]
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for row in rows:
        score = _weighted_score(_validated_features(row, GATE_F_FEATURES, "features"), weights)
        if "score" in row and not math.isclose(float(row["score"]), score, abs_tol=1e-12):
            raise ValueError("caller-supplied Gate F score does not match the frozen scorer")
        scored.append({**row, "score": score})
    return scored


def _score_gate_r_records(
    rows: Sequence[Mapping[str, Any]], weights: tuple[float, ...]
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for row in rows:
        top = _weighted_score(_validated_features(row, GATE_R_FEATURES, "top_features"), weights)
        runner_up = _weighted_score(
            _validated_features(row, GATE_R_FEATURES, "runner_up_features"), weights
        )
        margin = top - runner_up
        if margin < 0:
            raise ValueError("Gate R top candidate scores below its runner-up")
        if "top_score" in row and not math.isclose(float(row["top_score"]), top, abs_tol=1e-12):
            raise ValueError("caller-supplied Gate R score does not match the fitted scorer")
        if "margin" in row and not math.isclose(float(row["margin"]), margin, abs_tol=1e-12):
            raise ValueError("caller-supplied Gate R margin does not match the fitted scorer")
        scored.append({**row, "top_score": top, "margin": margin})
    return scored


def _select_threshold(rows: Sequence[Mapping[str, Any]], score_key: str) -> float:
    candidates = sorted({float(row[score_key]) for row in rows}, reverse=True)
    if not candidates:
        raise ValueError("tuning records are required")
    best = candidates[0]
    best_coverage = -1
    for threshold in candidates:
        accepted = [row for row in rows if float(row[score_key]) >= threshold]
        errors = sum(not bool(row["correct"]) for row in accepted)
        if errors == 0 and len(accepted) > best_coverage:
            best, best_coverage = threshold, len(accepted)
    return best


def _select_joint_retrieval_thresholds(rows: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    score_candidates = sorted({float(row["top_score"]) for row in rows})
    margin_candidates = sorted({float(row["margin"]) for row in rows})
    if not score_candidates or not margin_candidates:
        raise ValueError("retrieval tuning records are required")
    choices: list[tuple[int, float, float]] = []
    for tau_r in score_candidates:
        for gamma in margin_candidates:
            accepted = [row for row in rows if float(row["top_score"]) >= tau_r and float(row["margin"]) >= gamma]
            if accepted and all(bool(row["correct"]) for row in accepted):
                choices.append((len(accepted), tau_r, gamma))
    if not choices:
        raise ValueError("no jointly error-free Gate R threshold/margin pair")
    _coverage, tau_r, gamma = max(choices, key=lambda row: (row[0], -row[1], -row[2]))
    return tau_r, gamma


def calibrate_frozen_profile(
    *,
    train_r: Sequence[Mapping[str, Any]],
    tune_f: Sequence[Mapping[str, Any]],
    tune_r: Sequence[Mapping[str, Any]],
    cert_f: Sequence[Mapping[str, Any]],
    cert_r: Sequence[Mapping[str, Any]],
    extractor: StructuredSignatureExtractor,
    domain: str,
    tune_manifest_hash: str,
    cert_manifest_hash: str,
    issued_at: str,
    alpha: float = 0.05,
    maximum_error: float = 0.05,
    test_only: bool = False,
    used_certification_record_hashes: set[str] | None = None,
    certification_attempt_id: str = "",
    certification_attempt_registry: dict[str, Any] | None = None,
) -> FrozenCalibrationProfile:
    _validate_disjoint(train_r, tune_f, tune_r, cert_f, cert_r)
    schema_hash = extractor.binding.schema_hash
    _validate_record_contract(train_r, split="train-source", schema_hash=schema_hash)
    _validate_record_contract(tune_f, split="dev-tune", schema_hash=schema_hash, manifest_hash=tune_manifest_hash)
    _validate_record_contract(tune_r, split="dev-tune", schema_hash=schema_hash, manifest_hash=tune_manifest_hash)
    _validate_record_contract(cert_f, split="dev-cert", schema_hash=schema_hash, manifest_hash=cert_manifest_hash)
    _validate_record_contract(cert_r, split="dev-cert", schema_hash=schema_hash, manifest_hash=cert_manifest_hash)
    r_weights = fit_nonnegative_weights(train_r, feature_names=GATE_R_FEATURES)
    f_weights = (0.35, 0.20, 0.25, 0.20)
    scored_tune_f = _score_gate_f_records(tune_f, f_weights)
    scored_tune_r = _score_gate_r_records(tune_r, r_weights)
    scored_cert_f = _score_gate_f_records(cert_f, f_weights)
    scored_cert_r = _score_gate_r_records(cert_r, r_weights)
    certification_records_hash = canonical_hash(
        {"gate_f": scored_cert_f, "gate_r": scored_cert_r, "gate_f_weights": f_weights, "gate_r_weights": r_weights}
    )
    if certification_attempt_registry is not None:
        if not certification_attempt_id:
            raise ValueError("certification attempt ID is required with a persistent registry")
        if certification_attempt_id in certification_attempt_registry:
            raise ValueError("certification attempt ID was already consumed")
    if used_certification_record_hashes is not None and certification_records_hash in used_certification_record_hashes:
        raise ValueError("dev-cert records were already consumed by an earlier certification attempt")
    tau_f = _select_threshold(scored_tune_f, "score")
    tau_r, gamma = _select_joint_retrieval_thresholds(scored_tune_r)
    accepted_f = [row for row in scored_cert_f if float(row["score"]) >= tau_f]
    accepted_r = [row for row in scored_cert_r if float(row["top_score"]) >= tau_r and float(row["margin"]) >= gamma]
    if not accepted_f or not accepted_r:
        raise ValueError("certification has zero accepted records")
    f_upper = clopper_pearson_upper(sum(not bool(row["correct"]) for row in accepted_f), len(accepted_f), alpha=alpha)
    r_upper = clopper_pearson_upper(sum(not bool(row["correct"]) for row in accepted_r), len(accepted_r), alpha=alpha)
    if f_upper > maximum_error or r_upper > maximum_error:
        raise ValueError("dev-cert error upper bound exceeds policy")
    bindings = {
        "extractor_model_hash": extractor.binding.model_hash,
        "extractor_config_hash": extractor.binding.config_hash,
        "extractor_prompt_hash": extractor.binding.prompt_hash,
        "signature_schema_hash": extractor.binding.schema_hash,
        "gate_f_feature_hash": canonical_hash(GATE_F_FEATURES),
        "gate_r_feature_hash": canonical_hash(GATE_R_FEATURES),
        "gate_r_weights_hash": canonical_hash(r_weights),
        "tune_manifest_hash": tune_manifest_hash,
        "cert_manifest_hash": cert_manifest_hash,
        "certification_records_hash": certification_records_hash,
    }
    metadata = {
        "gate_f_weights": f_weights,
        "gate_r_weights": r_weights,
        "gate_f_error_upper": f_upper,
        "gate_r_error_upper": r_upper,
        "confidence_level": 1 - alpha,
        "maximum_error": maximum_error,
        "accepted_f": len(accepted_f),
        "accepted_r": len(accepted_r),
    }
    profile = FrozenCalibrationProfile(
        artifact_id="smp-cal-" + canonical_hash({"bindings": bindings, "metadata": metadata})[7:23],
        signature_scorer_version="smp.gate_f.v1",
        retrieval_scorer_version="smp.gate_r.v1",
        tau_f=tau_f,
        tau_r=tau_r,
        gamma=gamma,
        applicable_domain=domain,
        applicable_memory_schema_version="smp.memory.v1",
        certification_split_id="dev-cert",
        certification_method="clopper_pearson_fixed_certification",
        certification_metadata=metadata,
        issued_at=issued_at,
        test_only=test_only,
        production_bindings=bindings,
    )
    if used_certification_record_hashes is not None:
        used_certification_record_hashes.add(certification_records_hash)
    if certification_attempt_registry is not None:
        certification_attempt_registry[certification_attempt_id] = {
            "certification_records_hash": certification_records_hash,
            "profile_hash": profile.artifact_hash,
            "tune_manifest_hash": tune_manifest_hash,
            "cert_manifest_hash": cert_manifest_hash,
            "gate_r_joint_threshold_hash": canonical_hash({"tau_r": tau_r, "gamma": gamma}),
        }
    return profile
