"""Strict JSON loaders for hash-bound SMP experiment artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from self_evolving_agent.smp_artifacts import TaskRunIdentity
from self_evolving_agent.smp_contracts import (
    BankSnapshot,
    EvidenceEvent,
    EvidenceSource,
    EvidenceType,
    FieldStatus,
    FrozenCalibrationProfile,
    MechanismSignature,
    MemoryExposure,
    MemoryState,
    NarrowScope,
    OfficialOutcome,
    SIGNATURE_FIELDS,
    SIGNATURE_VERSION,
    SMPMemoryItem,
    SignatureField,
    SubmissionStatus,
    UtilityCertificate,
    UtilityTrialAuthorization,
    UtilityTrialSnapshot,
    LeakageClass,
)
from self_evolving_agent.smp_manifest import SMPExperimentManifest


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_manifest(path: str | Path) -> SMPExperimentManifest:
    row = _mapping(load_json(path), "manifest")
    for name in ("source_target_relations", "online_prequential_stream", "test_static_schedule", "checkpoint_subject_ids", "checkpoint_task_seeds"):
        if isinstance(row.get(name), list):
            row[name] = tuple(row[name])
    return SMPExperimentManifest(**_without(row, "manifest_hash"), manifest_hash=str(row.get("manifest_hash", "")))


def load_profile(path: str | Path) -> FrozenCalibrationProfile:
    row = _mapping(load_json(path), "calibration profile")
    return FrozenCalibrationProfile(**_without(row, "artifact_hash"), artifact_hash=str(row.get("artifact_hash", "")))


def load_certificate(path: str | Path) -> UtilityCertificate:
    row = _mapping(load_json(path), "utility certificate")
    return certificate_from_dict(row)


def certificate_from_dict(value: Mapping[str, Any]) -> UtilityCertificate:
    row = dict(value)
    if isinstance(row.get("checkpoint_subject_ids"), list):
        row = {**row, "checkpoint_subject_ids": tuple(row["checkpoint_subject_ids"])}
    return UtilityCertificate(**_without(row, "certificate_hash"), certificate_hash=str(row.get("certificate_hash", "")))


def load_identity(path: str | Path) -> TaskRunIdentity:
    row = _mapping(load_json(path), "task run identity")
    return TaskRunIdentity(**_without(row, "identity_hash"))


def load_outcome(path: str | Path) -> OfficialOutcome:
    row = _mapping(load_json(path), "official outcome")
    raw_status = row.get("submission_status")
    if not isinstance(raw_status, str):
        raise ValueError("official outcome submission_status type is invalid")
    row["submission_status"] = SubmissionStatus(raw_status)
    return OfficialOutcome(**_without(row, "outcome_hash"), outcome_hash=str(row.get("outcome_hash", "")))


def evidence_from_dict(value: Mapping[str, Any]) -> EvidenceEvent:
    row = dict(value)
    row.pop("schema_version", None)
    event_id = str(row.pop("event_id", ""))
    return EvidenceEvent(
        task_id=str(row["task_id"]),
        event_index=int(row["event_index"]),
        event_type=EvidenceType(str(row["event_type"])),
        source=EvidenceSource(str(row["source"])),
        observable=bool(row["observable"]),
        leakage_class=LeakageClass(str(row["leakage_class"])),
        payload=row["payload"],
        event_id=event_id,
    )


def narrow_scope_from_dict(value: Mapping[str, Any]) -> NarrowScope:
    row = dict(value)
    for name in ("preconditions_all", "preconditions_any"):
        if isinstance(row.get(name), list):
            row[name] = tuple(row[name])
    scope_hash = str(row.pop("scope_hash", ""))
    return NarrowScope(**row, scope_hash=scope_hash)


def signature_from_dict(value: Mapping[str, Any]) -> MechanismSignature:
    fields: dict[str, SignatureField] = {}
    for name in SIGNATURE_FIELDS:
        row = _mapping(value.get(name), f"signature.{name}")
        raw = row.get("value")
        if isinstance(raw, list):
            raw = tuple(raw)
        raw_status = row.get("status")
        raw_refs = row.get("evidence_refs", ())
        if not isinstance(raw_status, str) or not isinstance(raw_refs, list):
            raise ValueError(f"signature.{name} status/evidence_refs types are invalid")
        fields[name] = SignatureField(
            value=raw,
            status=FieldStatus(raw_status),
            evidence_refs=tuple(raw_refs),
        )
    return MechanismSignature(
        **fields,
        signature_version=str(value.get("signature_version", SIGNATURE_VERSION)),
        signature_hash=str(value.get("signature_hash", "")),
    )


def memory_from_dict(value: Mapping[str, Any]) -> SMPMemoryItem:
    signature = signature_from_dict(_mapping(value.get("signature"), "memory.signature"))
    exposure = MemoryExposure(**_mapping(value.get("exposure"), "memory.exposure"))
    scope_row = value.get("narrow_scope")
    scope = None
    if scope_row is not None:
        scope = narrow_scope_from_dict(_mapping(scope_row, "memory.narrow_scope"))
    return SMPMemoryItem(
        memory_id=str(value["memory_id"]),
        signature=signature,
        state=MemoryState(str(value["state"])),
        exposure=exposure,
        source_task_id=str(value["source_task_id"]),
        available_from_task_index=int(value["available_from_task_index"]),
        memory_schema_version=str(value.get("memory_schema_version", "smp.memory.v1")),
        narrow_scope=scope,
    )


def load_snapshot(path: str | Path) -> BankSnapshot | UtilityTrialSnapshot:
    row = _mapping(load_json(path), "bank snapshot")
    memories = tuple(memory_from_dict(_mapping(item, "snapshot memory")) for item in row.get("memories", ()))
    common = {
        "task_id": str(row["task_id"]),
        "task_index": int(row["task_index"]),
        "bank_version": int(row["bank_version"]),
        "memories": memories,
        "bank_item_count": int(row.get("bank_item_count", len(memories))),
        "snapshot_id": str(row.get("snapshot_id", "")),
        "snapshot_hash": str(row.get("snapshot_hash", "")),
    }
    if "authorization" not in row:
        return BankSnapshot(**common)
    authorization_row = _mapping(row["authorization"], "utility trial authorization")
    authorization = UtilityTrialAuthorization(
        **_without(authorization_row, "authorization_hash"),
        authorization_hash=str(authorization_row.get("authorization_hash", "")),
    )
    if row.get("authorization_hash") not in (None, authorization.authorization_hash):
        raise ValueError("utility trial snapshot authorization hash mismatch")
    return UtilityTrialSnapshot(**common, authorization=authorization)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return dict(value)


def _without(value: Mapping[str, Any], *names: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in names}
