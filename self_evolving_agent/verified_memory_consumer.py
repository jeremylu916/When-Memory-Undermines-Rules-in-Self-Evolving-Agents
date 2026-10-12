"""Thin, deterministic consumer path for narrowly verified GEM memory.

This module performs no target selection, model calls, or harness execution.  It
only materializes sanitized producer metadata and builds auditable consumer
artifacts from public task features and caller-supplied synthetic/run records.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "gem.verified_memory_consumer.v1"
SOURCE_TASK = "astropy__astropy-13033"
MAXIMUM_CLAIM = "later-required-column-missing diagnostic only"
LESSON = (
    "When a public TimeSeries problem reports removal of a required column, "
    "check whether validation diagnoses the later required column that is "
    "actually missing. This is a narrow diagnostic hint, not a repair recipe."
)
FORBIDDEN_KEYS = {
    "model_patch", "gold_patch", "reference_patch", "test_patch", "hidden_tests",
    "official_output", "official_outcome", "resolved", "harness_result",
}
FORBIDDEN_TEXT = (
    "gold patch", "reference patch", "hidden test", "official output",
    "model_patch", "gold_patch", "reference_patch", "test_patch",
)
REQUIRED_FEATURES = ("timeseries", "required_column", "column_removal", "diagnostic")
REQUIRED_EVIDENCE_HASHES = {
    "verified_memory_sidecar.json", "admission_event.json", "candidate_identity",
    "coverage_identity", "base_observation.json", "candidate_observation.json",
    "exec_result.json",
}
FALSE_REAL_CLAIM_KEYS = {
    "real_execution", "task_solved", "official_harness_pass",
    "benchmark_improvement", "swe_bench_resolved",
}


class ConsumerValidationError(ValueError):
    """Raised when consumer evidence violates the frozen thin-slice contract."""


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def materialize_package(
    *, sidecar: dict[str, Any], admission: dict[str, Any], evidence_hashes: dict[str, str]
) -> dict[str, Any]:
    """Build one versioned package without copying producer evidence content."""
    if sidecar.get("task_id") != SOURCE_TASK or admission.get("task_id") != SOURCE_TASK:
        raise ConsumerValidationError("unexpected source task")
    if not sidecar.get("verified_memory_created"):
        raise ConsumerValidationError("verified memory was not created")
    if sidecar.get("retrieval_policy") != "narrow_only":
        raise ConsumerValidationError("retrieval ceiling must be narrow_only")
    if sidecar.get("publication_state") != "admission_verified":
        raise ConsumerValidationError("source publication is not admission_verified")
    if admission.get("decision") != "admitted" or admission.get("policy") != "Exec":
        raise ConsumerValidationError("complete Exec admission is required")
    for key in ("candidate_id", "admission_event_id"):
        if not sidecar.get(key) or sidecar.get(key) != admission.get(key):
            raise ConsumerValidationError(f"broken {key} linkage")
    if not sidecar.get("coverage_id") or sidecar.get("coverage_id") != admission.get("coverage_id"):
        raise ConsumerValidationError("broken or empty coverage_id linkage")
    if not evidence_hashes or any(not str(value).startswith("sha256:") for value in evidence_hashes.values()):
        raise ConsumerValidationError("source evidence hashes are required")
    missing_hashes = REQUIRED_EVIDENCE_HASHES - set(evidence_hashes)
    if missing_hashes:
        raise ConsumerValidationError(f"missing evidence hashes: {sorted(missing_hashes)}")
    item = {
        "schema_version": SCHEMA,
        "memory_id": "",
        "source_task_id": SOURCE_TASK,
        "candidate_id": sidecar["candidate_id"],
        "coverage_id": sidecar.get("coverage_id", ""),
        "admission_event_id": sidecar["admission_event_id"],
        "source_evidence_hashes": dict(sorted(evidence_hashes.items())),
        "verified_memory_created": True,
        "maximum_claim": MAXIMUM_CLAIM,
        "applicability": {
            "domain": "astropy.timeseries.required_column_diagnostic",
            "public_features_all": list(REQUIRED_FEATURES),
        },
        "retrieval_policy": "narrow_only",
        "sanitized_lesson": LESSON,
        "source_task_exclusion": True,
    }
    item["memory_id"] = "vmmem_" + canonical_hash(item)[7:27]
    package = {"schema_version": SCHEMA, "items": [item]}
    package["package_hash"] = canonical_hash(package)
    assert_safe(package)
    return package


def retrieve(
    package: dict[str, Any], *, target_task: dict[str, Any], condition: str
) -> dict[str, Any]:
    """Apply exact public-feature applicability and emit a deterministic trace."""
    if condition not in {"NoMem", "VerifiedMem"}:
        raise ConsumerValidationError("unknown condition")
    validate_package(package)
    task_id = str(target_task.get("task_id") or "")
    features = sorted(set(target_task.get("public_features") or []))
    eligible, rejected = [], []
    for item in package.get("items", []):
        reason = None
        if condition == "NoMem":
            reason = "condition_disables_memory"
        elif task_id == item.get("source_task_id"):
            reason = "source_task_excluded"
        elif not item.get("verified_memory_created"):
            reason = "not_verified"
        elif item.get("retrieval_policy") != "narrow_only":
            reason = "policy_blocked"
        elif not set(item["applicability"]["public_features_all"]).issubset(features):
            reason = "applicability_mismatch"
        if reason:
            rejected.append({"memory_id": item.get("memory_id", ""), "reason": reason})
        else:
            eligible.append(item["memory_id"])
    selected = eligible[:1]
    trace = {
        "schema_version": SCHEMA,
        "target_task": task_id,
        "condition": condition,
        "query_public_features": features,
        "eligible_memory_ids": eligible,
        "rejected": rejected,
        "selected_memory_ids": selected,
        "selected_count": len(selected),
        "policy_decision": "selected" if selected else "none",
        "package_hash": package["package_hash"],
    }
    trace["retrieval_trace_id"] = "vmret_" + canonical_hash(trace)[7:27]
    return trace


def build_prompt_linkage(
    *, base_prompt: str, package: dict[str, Any], retrieval_trace: dict[str, Any]
) -> dict[str, Any]:
    validate_package(package)
    selected = set(retrieval_trace["selected_memory_ids"])
    if retrieval_trace["condition"] == "NoMem" and selected:
        raise ConsumerValidationError("NoMem cannot carry memory")
    lessons = [item for item in package["items"] if item["memory_id"] in selected]
    block_payload = [
        {"memory_id": item["memory_id"], "maximum_claim": item["maximum_claim"],
         "lesson": item["sanitized_lesson"]}
        for item in lessons
    ]
    memory_block = json.dumps(block_payload, sort_keys=True, separators=(",", ":"))
    final_prompt = base_prompt + "\n<verified_memory>" + memory_block + "</verified_memory>"
    linkage = {
        "schema_version": SCHEMA,
        "target_task": retrieval_trace["target_task"],
        "condition": retrieval_trace["condition"],
        "base_prompt_hash": canonical_hash(base_prompt),
        "memory_block_hash": canonical_hash(memory_block),
        "final_prompt_hash": canonical_hash(final_prompt),
        "selected_memory_ids": retrieval_trace["selected_memory_ids"],
        "retrieval_trace_id": retrieval_trace["retrieval_trace_id"],
        "memory_block_version": SCHEMA,
        "package_hash": package["package_hash"],
        "final_prompt": final_prompt,
    }
    assert_safe(linkage)
    return linkage


def audit_prompt_pair(nomem: dict[str, Any], verified: dict[str, Any]) -> dict[str, Any]:
    """Prove a condition pair differs only in the versioned memory block."""
    if nomem.get("target_task") != verified.get("target_task"):
        raise ConsumerValidationError("prompt pair task drift")
    if nomem.get("base_prompt_hash") != verified.get("base_prompt_hash"):
        raise ConsumerValidationError("prompt base drift")
    if nomem.get("condition") != "NoMem" or verified.get("condition") != "VerifiedMem":
        raise ConsumerValidationError("invalid prompt condition pair")
    if nomem.get("selected_memory_ids"):
        raise ConsumerValidationError("NoMem cannot carry memory")
    nomem_base, nomem_block = _split_prompt(nomem.get("final_prompt", ""))
    verified_base, verified_block = _split_prompt(verified.get("final_prompt", ""))
    if nomem_base != verified_base or nomem_block != "[]":
        raise ConsumerValidationError("final prompt differs outside memory block")
    if bool(verified.get("selected_memory_ids")) != (verified_block != "[]"):
        raise ConsumerValidationError("VerifiedMem block does not match retrieval")
    audit = {
        "schema_version": SCHEMA, "target_task": nomem["target_task"],
        "base_prompt_hash": nomem["base_prompt_hash"],
        "nomem_final_prompt_hash": nomem["final_prompt_hash"],
        "verifiedmem_final_prompt_hash": verified["final_prompt_hash"],
        "memory_block_version": verified.get("memory_block_version"),
        "package_hash": verified.get("package_hash"),
        "only_difference": (
            "versioned_verified_memory_block" if verified_block != "[]" else "none_empty_memory_block"
        ),
        "pass": True,
    }
    return audit


def build_prediction(
    *, task_id: str, condition: str, linkage: dict[str, Any], ordinal: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create clearly synthetic adapter artifacts; never claims execution success."""
    common = {
        "schema_version": SCHEMA, "task_id": task_id, "condition": condition,
        "model": "synthetic/no-provider", "config_id": "synthetic_fixture_v1",
        "prompt_hash": linkage["final_prompt_hash"], "synthetic": True,
    }
    trajectory = {**common, "trajectory_id": f"syntraj_{ordinal}_{condition.lower()}",
                  "status": "fixture_not_executed", "tool_failures": 0,
                  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                            "estimated_cost_usd": 0.0, "provider_invoice": False}}
    prediction = {**common, "prediction_id": f"synpred_{ordinal}_{condition.lower()}",
                  "model_patch": "", "status": "fixture_not_executed",
                  "trajectory_id": trajectory["trajectory_id"]}
    validate_artifact(trajectory)
    validate_artifact(prediction)
    return trajectory, prediction


def build_solver_adapter_plan(
    *, task: dict[str, Any], condition: str, linkage: dict[str, Any]
) -> dict[str, Any]:
    """Return a deliberately non-executable synthetic solver command plan."""
    if task.get("synthetic") is not True or task.get("real") not in (None, False):
        raise ConsumerValidationError("solver adapter accepts synthetic fixtures only")
    if linkage.get("target_task") != task.get("task_id") or linkage.get("condition") != condition:
        raise ConsumerValidationError("solver adapter linkage drift")
    plan = {
        "schema_version": SCHEMA,
        "adapter": "synthetic_solver_plan",
        "task_id": task["task_id"],
        "condition": condition,
        "provider": "synthetic/no-provider",
        "api_enabled": False,
        "real_execution": False,
        "executable": False,
        "argv": ["synthetic-solver-adapter", "--not-executable", task["task_id"], condition],
        "prompt_hash": linkage["final_prompt_hash"],
        "status": "fixture_not_executed",
        "synthetic": True,
    }
    validate_artifact(plan)
    return plan


def evaluate_fixture_adapter(
    *, task: dict[str, Any], prediction: dict[str, Any], trajectory: dict[str, Any],
    retrieval_trace: dict[str, Any]
) -> dict[str, Any]:
    """Adapt synthetic linkage records into integration-only paired metrics."""
    if task.get("synthetic") is not True or task.get("real") not in (None, False):
        raise ConsumerValidationError("evaluator adapter accepts synthetic fixtures only")
    validate_artifact(prediction)
    validate_artifact(trajectory)
    ids = {task.get("task_id"), prediction.get("task_id"), trajectory.get("task_id"),
           retrieval_trace.get("target_task")}
    conditions = {prediction.get("condition"), trajectory.get("condition"),
                  retrieval_trace.get("condition")}
    if len(ids) != 1 or len(conditions) != 1:
        raise ConsumerValidationError("evaluator adapter lineage drift")
    if prediction.get("trajectory_id") != trajectory.get("trajectory_id"):
        raise ConsumerValidationError("prediction trajectory linkage drift")
    usage = trajectory.get("usage", {})
    metrics = {
        "resolved": False, "non_empty_patch": False, "patch_apply": False,
        "generation_error": False, "harness_error": False,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "estimated_cost_usd": usage.get("estimated_cost_usd", 0.0),
        "wall_time_seconds": 0.0, "tool_failures": trajectory.get("tool_failures", 0),
        "retrieval_selected_count": retrieval_trace.get("selected_count", 0),
        "memory_ids": retrieval_trace.get("selected_memory_ids", []),
        "integration_only": True, "synthetic": True,
    }
    return {
        "schema_version": SCHEMA,
        "adapter": "synthetic_evaluator_adapter",
        "task_id": task["task_id"],
        "condition": prediction["condition"],
        "prediction_id": prediction["prediction_id"],
        "trajectory_id": trajectory["trajectory_id"],
        "retrieval_trace_id": retrieval_trace["retrieval_trace_id"],
        "integration_only": True,
        "official_harness": False,
        "synthetic": True,
        "metrics": metrics,
    }


def build_synthetic_fixture_manifest() -> dict[str, Any]:
    fixtures = [
        {"task_id": "synthetic__timeseries-required-column-1", "role": "eligible",
         "public_statement": "A synthetic TimeSeries required-column removal diagnostic issue.",
         "public_features": list(REQUIRED_FEATURES)},
        {"task_id": "synthetic__web-routing-1", "role": "negative_control",
         "public_statement": "A synthetic web routing issue unrelated to columns.",
         "public_features": ["web", "routing"]},
        {"task_id": "synthetic__plot-colors-1", "role": "negative_control",
         "public_statement": "A synthetic plot color issue unrelated to required columns.",
         "public_features": ["plotting", "colors"]},
    ]
    for item in fixtures:
        item.update({"synthetic": True, "real": False, "source_disjoint": True})
    return {"schema_version": SCHEMA, "fixture_manifest_version": 1,
            "source_task_id": SOURCE_TASK, "items": fixtures,
            "public_fields_only": True, "t10_enabled": False}


def analyze_pairs(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    for row in rows:
        pair = {
            "task_id": row["task_id"], "role": row["role"],
            "nomem": _condition_metrics(row["NoMem"]),
            "verifiedmem": _condition_metrics(row["VerifiedMem"]),
        }
        pair["regression"] = bool(pair["nomem"]["resolved"] and not pair["verifiedmem"]["resolved"])
        pairs.append(pair)
    return {
        "schema_version": SCHEMA, "synthetic": True, "pairs": pairs,
        "aggregate": {"task_count": len(pairs),
                      "eligible_target_count": sum(p["role"] == "eligible" for p in pairs),
                      "negative_control_regressions": sum(p["role"] == "negative_control" and p["regression"] for p in pairs)},
        "evidence_limitations": "Synthetic integration evidence only; no performance conclusion.",
        "larger_experiment_decision": "NO_GO_SYNTHETIC_ONLY",
    }


def build_loopback_queue(trajectories: Iterable[dict[str, Any]]) -> dict[str, Any]:
    candidates = [{
        "candidate_id": "next_" + canonical_hash(t)[7:23], "target_task": t["task_id"],
        "condition": t["condition"], "trajectory_id": t["trajectory_id"],
        "status": "quarantined", "reason": "oracle_unavailable",
        "verified_memory_created": False,
    } for t in trajectories]
    return {"schema_version": SCHEMA, "queue_version": 1, "candidates": candidates,
            "automatic_admission": False}


def assert_safe(payload: Any) -> None:
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in FORBIDDEN_KEYS:
                    raise ConsumerValidationError(f"forbidden field: {key}")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            lowered = value.lower()
            if any(marker in lowered for marker in FORBIDDEN_TEXT):
                raise ConsumerValidationError("forbidden content")
    walk(payload)


def validate_package(package: dict[str, Any]) -> None:
    if package.get("schema_version") != SCHEMA or len(package.get("items", [])) != 1:
        raise ConsumerValidationError("invalid thin package")
    item = package["items"][0]
    if tuple(item.get("applicability", {}).get("public_features_all", [])) != REQUIRED_FEATURES:
        raise ConsumerValidationError("narrow applicability predicate drift")
    if item.get("source_task_id") != SOURCE_TASK or not item.get("source_task_exclusion"):
        raise ConsumerValidationError("source exclusion drift")
    if item.get("retrieval_policy") != "narrow_only" or not item.get("verified_memory_created"):
        raise ConsumerValidationError("memory is not narrowly retrievable")


def validate_artifact(artifact: dict[str, Any]) -> None:
    """Reject fixture artifacts that could be mistaken for empirical results."""
    if artifact.get("synthetic") is not True:
        raise ConsumerValidationError("consumer fixture must be explicitly synthetic")
    for key in FALSE_REAL_CLAIM_KEYS:
        if artifact.get(key) not in (None, False):
            raise ConsumerValidationError(f"forbidden real-result claim: {key}")
    if "resolved" in artifact and artifact["resolved"] not in (None, False):
        raise ConsumerValidationError("SWE-bench resolved claim is forbidden")
    if "model_patch" in artifact and artifact["model_patch"] != "":
        raise ConsumerValidationError("synthetic model_patch must remain empty")
    if artifact.get("status") not in {"fixture_not_executed", "synthetic_integration_complete"}:
        raise ConsumerValidationError("fixture cannot claim execution")


def _split_prompt(prompt: str) -> tuple[str, str]:
    start, end = "\n<verified_memory>", "</verified_memory>"
    if prompt.count(start) != 1 or prompt.count(end) != 1 or not prompt.endswith(end):
        raise ConsumerValidationError("invalid versioned memory block")
    base, remainder = prompt.split(start, 1)
    return base, remainder[:-len(end)]


def _condition_metrics(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in (
        "resolved", "non_empty_patch", "patch_apply", "generation_error",
        "harness_error", "prompt_tokens", "completion_tokens", "total_tokens",
        "estimated_cost_usd", "wall_time_seconds", "tool_failures",
        "retrieval_selected_count", "memory_ids",
        "integration_only", "synthetic",
    )}
