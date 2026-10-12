"""Strict real-evidence contracts for the frozen GEM T=3 chain.

The dry-run runner does not import builders from this module.  Real records can
only validate against two authorized, executed public-oracle observations and
the complete frozen candidate/coverage/provenance/specification/source graph.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from self_evolving_agent.gem_public_oracle import (
    CLEAN_WORKTREE_HASH,
    EMPTY_PATCH_HASH,
    OBSERVATION_SCHEMA_VERSION,
    validate_observation_record,
)
REAL_ORACLE_RESULT_SCHEMA_VERSION = "gem.t3_real_oracle_result.v1"
REAL_ADMISSION_EVENT_SCHEMA_VERSION = "gem.t3_real_admission_event.v1"
VERIFIED_MEMORY_ITEM_SCHEMA_VERSION = "gem.t3_verified_memory_item.v1"

REAL_AUTHORIZED = "real_authorized"
EXECUTED_COMPLETE = "executed_complete"

_RESULT_FIELDS = {
    "schema_version",
    "oracle_run_id",
    "created_at",
    "execution_mode",
    "evidence_status",
    "authorization_ref",
    "task_id",
    "candidate_id",
    "coverage_id",
    "provenance_id",
    "oracle_specification_id",
    "repo",
    "base_commit",
    "public_source_content_hash",
    "oracle_command_hash",
    "oracle_source_hash",
    "candidate_payload_hash",
    "candidate_patch_hash",
    "pre_worktree_hash",
    "post_worktree_hash",
    "pre_observed_patch_hash",
    "post_observed_patch_hash",
    "maximum_claim",
    "maximum_retrieval_policy",
    "pre_observation_hash",
    "post_observation_hash",
    "infrastructure_available",
    "oracle_executed",
    "reproduction_observed",
    "candidate_passed",
    "task_solved",
    "partial",
    "interrupted",
    "outcome",
}
_ADMISSION_FIELDS = {
    "schema_version",
    "admission_event_id",
    "created_at",
    "authorization_ref",
    "policy",
    "decision",
    "task_id",
    "candidate_id",
    "coverage_id",
    "provenance_id",
    "oracle_specification_id",
    "oracle_run_id",
    "public_source_content_hash",
    "oracle_command_hash",
    "oracle_source_hash",
    "maximum_claim",
    "maximum_retrieval_policy",
    "partial",
    "interrupted",
}
_MEMORY_FIELDS = {
    "schema_version",
    "verified_memory_item_id",
    "created_at",
    "authorization_ref",
    "publication_state",
    "retrieval_policy",
    "task_id",
    "candidate_id",
    "coverage_id",
    "provenance_id",
    "oracle_specification_id",
    "oracle_run_id",
    "admission_event_id",
    "verified_claim",
    "maximum_claim",
    "maximum_retrieval_policy",
}


class T3RuntimeEvidenceError(ValueError):
    """Raised when a real-evidence graph is incomplete, forged, or overbroad."""


def validate_real_oracle_result(
    result: dict[str, Any],
    *,
    pre_observation: dict[str, Any],
    post_observation: dict[str, Any],
    package: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> None:
    """Validate a complete real result; previews and partial runs fail closed."""

    _closed(result, _RESULT_FIELDS, "real oracle result")
    if result["schema_version"] != REAL_ORACLE_RESULT_SCHEMA_VERSION:
        raise T3RuntimeEvidenceError("preview or unsupported oracle result schema")
    if result["execution_mode"] != REAL_AUTHORIZED:
        raise T3RuntimeEvidenceError("real oracle result lacks authorized mode")
    if result["evidence_status"] != EXECUTED_COMPLETE:
        raise T3RuntimeEvidenceError("real oracle result is not executed_complete")
    if not result["authorization_ref"]:
        raise T3RuntimeEvidenceError("real oracle result lacks authorization_ref")
    if result["partial"] is not False or result["interrupted"] is not False:
        raise T3RuntimeEvidenceError("partial/interrupted result is inadmissible")
    if (
        result["infrastructure_available"] is not True
        or result["oracle_executed"] is not True
        or result["reproduction_observed"] is not True
        or result["candidate_passed"] is not True
        or result["task_solved"] is not False
        or result["outcome"] != "fail_before_pass_after"
    ):
        raise T3RuntimeEvidenceError("real result status distinctions are invalid")

    coverage = _one(package["coverage_records"], "coverage_id", result["coverage_id"])
    implementation = _one(
        package["implementation_records"], "coverage_id", result["coverage_id"]
    )
    candidate = _one(candidates, "candidate_id", result["candidate_id"])
    for key in ("task_id", "candidate_id", "repo", "base_commit"):
        if result.get(key) != coverage.get(key):
            raise T3RuntimeEvidenceError(f"real result {key} does not match coverage")
    if candidate.get("task_id") != result["task_id"] or candidate.get("repo") != result["repo"]:
        raise T3RuntimeEvidenceError("real result candidate linkage mismatch")
    intervention = candidate.get("intervention")
    if not isinstance(intervention, dict):
        raise T3RuntimeEvidenceError("candidate intervention linkage is missing")
    if result["candidate_payload_hash"] != _candidate_payload_hash(candidate):
        raise T3RuntimeEvidenceError("candidate_payload_hash mismatch")
    if result["candidate_patch_hash"] != intervention.get("patch_hash"):
        raise T3RuntimeEvidenceError("candidate_patch_hash mismatch")
    if result["pre_worktree_hash"] != CLEAN_WORKTREE_HASH:
        raise T3RuntimeEvidenceError("pre worktree is not the clean frozen base")
    if result["post_worktree_hash"] == CLEAN_WORKTREE_HASH:
        raise T3RuntimeEvidenceError("post worktree lacks candidate patch state")
    if result["pre_observed_patch_hash"] != EMPTY_PATCH_HASH:
        raise T3RuntimeEvidenceError("pre observed patch is not empty")
    if result["post_observed_patch_hash"] != result["candidate_patch_hash"]:
        raise T3RuntimeEvidenceError(
            "post observed patch does not match frozen candidate patch hash"
        )
    for key in (
        "provenance_id",
        "oracle_specification_id",
        "oracle_source_hash",
    ):
        if result.get(key) != implementation.get(key):
            raise T3RuntimeEvidenceError(f"real result {key} mismatch")
    expected = {
        "public_source_content_hash": coverage["public_source_content_hash"],
        "oracle_command_hash": coverage["proposed_oracle_command_hash"],
        "maximum_claim": coverage["maximum_claim"],
        "maximum_retrieval_policy": coverage["maximum_retrieval_policy"],
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise T3RuntimeEvidenceError(f"real result {key} exceeds or differs from ceiling")

    _validate_observation_pair(
        result,
        pre_observation=pre_observation,
        post_observation=post_observation,
    )
    if result["oracle_run_id"] != real_oracle_run_id(result):
        raise T3RuntimeEvidenceError("oracle_run_id mismatch")


def build_real_admission_event(
    result: dict[str, Any],
    *,
    pre_observation: dict[str, Any],
    post_observation: dict[str, Any],
    package: dict[str, Any],
    candidates: list[dict[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    """Build an admission only after validating real, complete evidence."""

    validate_real_oracle_result(
        result,
        pre_observation=pre_observation,
        post_observation=post_observation,
        package=package,
        candidates=candidates,
    )
    event = {
        "schema_version": REAL_ADMISSION_EVENT_SCHEMA_VERSION,
        "admission_event_id": "",
        "created_at": created_at,
        "authorization_ref": result["authorization_ref"],
        "policy": "Exec",
        "decision": "admitted",
        "task_id": result["task_id"],
        "candidate_id": result["candidate_id"],
        "coverage_id": result["coverage_id"],
        "provenance_id": result["provenance_id"],
        "oracle_specification_id": result["oracle_specification_id"],
        "oracle_run_id": result["oracle_run_id"],
        "public_source_content_hash": result["public_source_content_hash"],
        "oracle_command_hash": result["oracle_command_hash"],
        "oracle_source_hash": result["oracle_source_hash"],
        "maximum_claim": result["maximum_claim"],
        "maximum_retrieval_policy": result["maximum_retrieval_policy"],
        "partial": False,
        "interrupted": False,
    }
    event["admission_event_id"] = _stable_id("gemt3adm", event, "admission_event_id")
    validate_real_admission_event(event, result=result)
    return event


def validate_real_admission_event(
    event: dict[str, Any], *, result: dict[str, Any]
) -> None:
    _closed(event, _ADMISSION_FIELDS, "real admission event")
    if event["schema_version"] != REAL_ADMISSION_EVENT_SCHEMA_VERSION:
        raise T3RuntimeEvidenceError("preview or unsupported admission schema")
    if event["policy"] != "Exec" or event["decision"] != "admitted":
        raise T3RuntimeEvidenceError("only real Exec admission is accepted")
    if event["partial"] is not False or event["interrupted"] is not False:
        raise T3RuntimeEvidenceError("partial/interrupted admission is forbidden")
    mapping = {
        "authorization_ref": "authorization_ref",
        "task_id": "task_id",
        "candidate_id": "candidate_id",
        "coverage_id": "coverage_id",
        "provenance_id": "provenance_id",
        "oracle_specification_id": "oracle_specification_id",
        "oracle_run_id": "oracle_run_id",
        "public_source_content_hash": "public_source_content_hash",
        "oracle_command_hash": "oracle_command_hash",
        "oracle_source_hash": "oracle_source_hash",
        "maximum_claim": "maximum_claim",
        "maximum_retrieval_policy": "maximum_retrieval_policy",
    }
    for event_key, result_key in mapping.items():
        if event.get(event_key) != result.get(result_key):
            raise T3RuntimeEvidenceError(f"admission {event_key} linkage mismatch")
    if event["admission_event_id"] != _stable_id(
        "gemt3adm", event, "admission_event_id"
    ):
        raise T3RuntimeEvidenceError("admission_event_id mismatch")


def build_verified_memory_item(
    event: dict[str, Any],
    *,
    result: dict[str, Any],
    pre_observation: dict[str, Any],
    post_observation: dict[str, Any],
    package: dict[str, Any],
    candidates: list[dict[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    """Build narrow retrieval only from a validated admitted event.

    Matplotlib remains locked until a distinct, future scope-review contract is
    implemented; no boolean argument can bypass that lock.
    """

    validate_real_oracle_result(
        result,
        pre_observation=pre_observation,
        post_observation=post_observation,
        package=package,
        candidates=candidates,
    )
    validate_real_admission_event(event, result=result)
    if result["maximum_retrieval_policy"] != "narrow_only":
        raise T3RuntimeEvidenceError("retrieval remains locked pending scope review")
    item = {
        "schema_version": VERIFIED_MEMORY_ITEM_SCHEMA_VERSION,
        "verified_memory_item_id": "",
        "created_at": created_at,
        "authorization_ref": event["authorization_ref"],
        "publication_state": "admission_verified",
        "retrieval_policy": "narrow_only",
        "task_id": event["task_id"],
        "candidate_id": event["candidate_id"],
        "coverage_id": event["coverage_id"],
        "provenance_id": event["provenance_id"],
        "oracle_specification_id": event["oracle_specification_id"],
        "oracle_run_id": event["oracle_run_id"],
        "admission_event_id": event["admission_event_id"],
        "verified_claim": event["maximum_claim"],
        "maximum_claim": event["maximum_claim"],
        "maximum_retrieval_policy": event["maximum_retrieval_policy"],
    }
    item["verified_memory_item_id"] = _stable_id(
        "gemt3mem", item, "verified_memory_item_id"
    )
    validate_verified_memory_item(
        item,
        event=event,
        result=result,
        pre_observation=pre_observation,
        post_observation=post_observation,
        package=package,
        candidates=candidates,
    )
    return item


def validate_verified_memory_item(
    item: dict[str, Any],
    *,
    event: dict[str, Any],
    result: dict[str, Any],
    pre_observation: dict[str, Any],
    post_observation: dict[str, Any],
    package: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> None:
    validate_real_oracle_result(
        result,
        pre_observation=pre_observation,
        post_observation=post_observation,
        package=package,
        candidates=candidates,
    )
    validate_real_admission_event(event, result=result)
    _closed(item, _MEMORY_FIELDS, "verified memory item")
    if item["schema_version"] != VERIFIED_MEMORY_ITEM_SCHEMA_VERSION:
        raise T3RuntimeEvidenceError("unsupported verified memory schema")
    if item["publication_state"] != "admission_verified":
        raise T3RuntimeEvidenceError("sidecar did not consume admission")
    if item["retrieval_policy"] != "narrow_only":
        raise T3RuntimeEvidenceError("sidecar retrieval policy is not narrow_only")
    if item["maximum_retrieval_policy"] != "narrow_only":
        raise T3RuntimeEvidenceError("sidecar exceeds frozen retrieval ceiling")
    for key in (
        "authorization_ref",
        "task_id",
        "candidate_id",
        "coverage_id",
        "provenance_id",
        "oracle_specification_id",
        "oracle_run_id",
        "maximum_claim",
        "maximum_retrieval_policy",
    ):
        if item.get(key) != event.get(key):
            raise T3RuntimeEvidenceError(f"sidecar {key} linkage mismatch")
    if item["admission_event_id"] != event["admission_event_id"]:
        raise T3RuntimeEvidenceError("sidecar admission_event_id mismatch")
    if item["verified_claim"] != item["maximum_claim"]:
        raise T3RuntimeEvidenceError("sidecar claim exceeds frozen maximum_claim")
    if item["verified_memory_item_id"] != _stable_id(
        "gemt3mem", item, "verified_memory_item_id"
    ):
        raise T3RuntimeEvidenceError("verified_memory_item_id mismatch")


def real_oracle_run_id(result: dict[str, Any]) -> str:
    return _stable_id("gemt3orun", result, "oracle_run_id")


def observation_hash(observation: dict[str, Any]) -> str:
    encoded = json.dumps(
        observation, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_observation_pair(
    result: dict[str, Any],
    *,
    pre_observation: dict[str, Any],
    post_observation: dict[str, Any],
) -> None:
    try:
        validate_observation_record(pre_observation)
        validate_observation_record(post_observation)
    except ValueError as exc:
        raise T3RuntimeEvidenceError(f"invalid public observation: {exc}") from exc
    for observation in (pre_observation, post_observation):
        if observation["schema_version"] != OBSERVATION_SCHEMA_VERSION:
            raise T3RuntimeEvidenceError("preview cannot substitute for real observation")
        mapping = {
            "task_id": "task_id",
            "coverage_id": "coverage_id",
            "provenance_id": "provenance_id",
            "oracle_specification_id": "oracle_specification_id",
            "repo": "repo",
            "expected_base_commit": "base_commit",
            "observed_base_commit": "base_commit",
            "public_source_content_hash": "public_source_content_hash",
            "oracle_command_hash": "oracle_command_hash",
            "oracle_source_hash": "oracle_source_hash",
            "candidate_payload_hash": "candidate_payload_hash",
            "candidate_patch_hash": "candidate_patch_hash",
            "authorization_ref": "authorization_ref",
        }
        for observation_key, result_key in mapping.items():
            if observation.get(observation_key) != result.get(result_key):
                raise T3RuntimeEvidenceError(
                    f"observation {observation_key} linkage mismatch"
                )
    if not (
        pre_observation["subject_role"] == "frozen_base"
        and pre_observation["subject_worktree_hash"] == result["pre_worktree_hash"]
        and pre_observation["observed_patch_hash"] == result["pre_observed_patch_hash"]
        and
        pre_observation["reproduction_observed"] is True
        and pre_observation["public_observable_satisfied"] is False
        and pre_observation["outcome"] == "fail"
    ):
        raise T3RuntimeEvidenceError("base observation did not reproduce public issue")
    if not (
        post_observation["subject_role"] == "candidate"
        and post_observation["subject_worktree_hash"] == result["post_worktree_hash"]
        and post_observation["observed_patch_hash"] == result["post_observed_patch_hash"]
        and
        post_observation["reproduction_observed"] is False
        and post_observation["public_observable_satisfied"] is True
        and post_observation["outcome"] == "pass"
    ):
        raise T3RuntimeEvidenceError("candidate observation did not pass public oracle")
    if result["pre_observation_hash"] != observation_hash(pre_observation):
        raise T3RuntimeEvidenceError("pre observation hash mismatch")
    if result["post_observation_hash"] != observation_hash(post_observation):
        raise T3RuntimeEvidenceError("post observation hash mismatch")


def _stable_id(prefix: str, record: dict[str, Any], id_field: str) -> str:
    payload = deepcopy(record)
    payload[id_field] = ""
    payload.pop("created_at", None)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:20]}"


def _candidate_payload_hash(candidate: dict[str, Any]) -> str:
    encoded = json.dumps(
        candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _closed(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise T3RuntimeEvidenceError(f"{label} fields are not closed")


def _one(records: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    matches = [record for record in records if record.get(key) == value]
    if len(matches) != 1:
        raise T3RuntimeEvidenceError(f"expected exactly one {key} linkage")
    return matches[0]
