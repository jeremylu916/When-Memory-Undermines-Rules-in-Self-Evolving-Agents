"""No-API runtime integration contract for the frozen GEM T=3 package.

The records in this module are previews, not oracle/admission/memory evidence.
They exist only to prove that the frozen public coverage package can be linked
through the future runtime graph without executing a command.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from self_evolving_agent.gem_memory_candidate import validate_gem_memory_candidate
from self_evolving_agent.gem_public_oracle import stable_record_id
from self_evolving_agent.gem_oracle_coverage import (
    T3_TASK_IDS,
    validate_t3_oracle_coverage_package,
)


RUNTIME_GRAPH_SCHEMA_VERSION = "gem.coverage_runtime_preview.v1"
ORACLE_PREVIEW_SCHEMA_VERSION = "gem.coverage_oracle_preview.v1"
ADMISSION_PREVIEW_SCHEMA_VERSION = "gem.coverage_admission_preview.v1"
SIDECAR_PREVIEW_SCHEMA_VERSION = "gem.coverage_sidecar_preview.v1"

FIXTURE_DRY_RUN = "fixture_dry_run"
SIMULATED_NOT_EXECUTED = "simulated_not_executed"
PREVIEW_INELIGIBLE = "preview_ineligible_for_admission"
PREVIEW_NOT_RETRIEVABLE = "preview_not_retrievable"

APPLICATION_ROOT = Path(__file__).resolve().parents[1]
FROZEN_PACKAGE_DIR = (
    APPLICATION_ROOT / "runs" / "gem_t3_public_oracle_coverage_preparation"
)
FROZEN_PUBLIC_SUBSET_CONFIG = APPLICATION_ROOT / "configs/swe_verified_subset_40.json"

_GRAPH_FIELDS = {
    "schema_version", "created_at", "execution_mode", "evidence_status",
    "real_execution_authorized", "task_ids", "package_fingerprint",
    "oracle_previews", "admission_previews", "sidecar_previews",
}
_ORACLE_FIELDS = {
    "schema_version", "oracle_preview_id", "created_at", "execution_mode",
    "evidence_status", "coverage_id", "task_id", "candidate_id", "repo",
    "base_commit", "public_source_content_hash", "oracle_command_hash",
    "oracle_source_hash", "provenance_id", "oracle_specification_id",
    "candidate_payload_hash", "candidate_patch_hash",
    "maximum_claim", "maximum_retrieval_policy", "infrastructure_available",
    "oracle_executed", "reproduction_observed", "candidate_passed",
    "task_solved", "authorization_ref",
}
_ADMISSION_FIELDS = {
    "schema_version", "admission_preview_id", "created_at", "execution_mode",
    "evidence_status", "decision", "eligible_for_real_admission",
    "coverage_id", "task_id", "candidate_id", "oracle_preview_ids",
    "provenance_id", "oracle_specification_id", "maximum_claim",
    "candidate_payload_hash", "candidate_patch_hash",
    "maximum_retrieval_policy", "authorization_ref",
}
_SIDECAR_FIELDS = {
    "schema_version", "sidecar_preview_id", "created_at", "execution_mode",
    "evidence_status", "publication_state", "retrieval_policy", "coverage_id",
    "task_id", "candidate_id", "oracle_preview_id", "admission_preview_id",
    "verified_claim_preview", "maximum_claim", "maximum_retrieval_policy",
    "provenance_id", "oracle_specification_id", "authorization_ref",
    "candidate_payload_hash", "candidate_patch_hash",
}


class GemRuntimeIntegrationValidationError(ValueError):
    """Raised when a preview graph breaks the frozen runtime contract."""


def load_t3_coverage_package(
    package_dir: str | Path,
    public_subset_config: str | Path,
) -> dict[str, Any]:
    """Load and validate the frozen package using local public statements only."""

    root = Path(package_dir).resolve()
    subset_path = Path(public_subset_config).resolve()
    if root != FROZEN_PACKAGE_DIR.resolve():
        raise GemRuntimeIntegrationValidationError(
            "coverage package directory must be the configured frozen path"
        )
    if subset_path != FROZEN_PUBLIC_SUBSET_CONFIG.resolve():
        raise GemRuntimeIntegrationValidationError(
            "public subset config must be the configured frozen path"
        )
    application_config = _read_json_object(
        APPLICATION_ROOT / "configs" / "gem_t3_execution_application.json"
    )
    _validate_frozen_loader_inputs(
        application_config,
        (
            root / "coverage_records.json",
            root / "coverage_provenance.json",
            root / "oracle_specifications.json",
            subset_path,
        ),
    )
    coverage_records = _read_json_list(root / "coverage_records.json")
    provenance_manifest = _read_json_object(root / "coverage_provenance.json")
    specification_manifest = _read_json_object(root / "oracle_specifications.json")
    public_problem_statements = _load_public_problem_statements(subset_path)
    validate_t3_oracle_coverage_package(
        coverage_records,
        provenance_manifest,
        specification_manifest,
        public_problem_statements,
    )
    implementation_records: list[dict[str, str]] = []
    project_root = APPLICATION_ROOT
    reviewed_implementations = _unique_by(
        application_config.get("oracle_implementations", []),
        "task_id",
        "reviewed implementation",
    )
    provenance_by_coverage = _unique_by(
        provenance_manifest["records"], "coverage_id", "provenance"
    )
    specification_by_coverage = _unique_by(
        specification_manifest["specifications"], "coverage_id", "specification"
    )
    for coverage in coverage_records:
        coverage_id = coverage["coverage_id"]
        provenance = provenance_by_coverage[coverage_id]
        specification = specification_by_coverage[coverage_id]
        source_path = (project_root / specification["proposed_script"]).resolve()
        if project_root not in source_path.parents or not source_path.is_file():
            raise GemRuntimeIntegrationValidationError(
                "registered public-oracle implementation is missing"
            )
        source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        reviewed = reviewed_implementations.get(coverage["task_id"])
        if (
            reviewed is None
            or reviewed.get("path") != specification["proposed_script"]
            or reviewed.get("sha256") != source_digest
        ):
            raise GemRuntimeIntegrationValidationError(
                "registered public-oracle source does not match reviewed application hash"
            )
        implementation_records.append(
            {
                "coverage_id": coverage_id,
                "provenance_id": stable_record_id("gemprov", provenance),
                "oracle_specification_id": stable_record_id(
                    "gemoraspec", specification
                ),
                "oracle_source_hash": "sha256:"
                + source_digest,
                "proposed_script": specification["proposed_script"],
            }
        )
    return {
        "coverage_records": coverage_records,
        "provenance_manifest": provenance_manifest,
        "specification_manifest": specification_manifest,
        "implementation_records": implementation_records,
    }


def load_t3_candidates(path: str | Path) -> list[dict[str, Any]]:
    """Load exactly the registered T=3 candidates from an existing JSONL file."""

    records = _read_jsonl(Path(path))
    for record in records:
        validate_gem_memory_candidate(record)
    by_task = _unique_by(records, "task_id", "candidate")
    if set(by_task) != set(T3_TASK_IDS) or len(records) != len(T3_TASK_IDS):
        raise GemRuntimeIntegrationValidationError(
            "candidate input must contain exactly the frozen T=3 tasks"
        )
    return [by_task[task_id] for task_id in T3_TASK_IDS]


def build_t3_runtime_preview_graph(
    *,
    package: dict[str, Any],
    candidates: list[dict[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    """Build a deterministic, non-executing fixture graph for integration checks."""

    coverage_records = package["coverage_records"]
    provenance_records = package["provenance_manifest"]["records"]
    specifications = package["specification_manifest"]["specifications"]
    coverage_by_task = _unique_by(coverage_records, "task_id", "coverage")
    provenance_by_coverage = _unique_by(
        provenance_records, "coverage_id", "provenance"
    )
    specification_by_coverage = _unique_by(
        specifications, "coverage_id", "specification"
    )
    implementation_by_coverage = _unique_by(
        package["implementation_records"], "coverage_id", "implementation"
    )
    candidate_by_task = _unique_by(candidates, "task_id", "candidate")

    oracle_previews: list[dict[str, Any]] = []
    admission_previews: list[dict[str, Any]] = []
    sidecar_previews: list[dict[str, Any]] = []
    for task_id in T3_TASK_IDS:
        coverage = coverage_by_task[task_id]
        candidate = candidate_by_task[task_id]
        provenance = provenance_by_coverage[coverage["coverage_id"]]
        specification = specification_by_coverage[coverage["coverage_id"]]
        implementation = implementation_by_coverage[coverage["coverage_id"]]
        _validate_candidate_coverage_link(candidate, coverage, provenance)
        candidate_payload_hash = _candidate_payload_hash(candidate)
        intervention = candidate.get("intervention")
        if not isinstance(intervention, dict) or not intervention.get("patch_hash"):
            raise GemRuntimeIntegrationValidationError("candidate patch hash is missing")
        candidate_patch_hash = str(intervention["patch_hash"])

        oracle = {
            "schema_version": ORACLE_PREVIEW_SCHEMA_VERSION,
            "oracle_preview_id": "",
            "created_at": created_at,
            "execution_mode": FIXTURE_DRY_RUN,
            "evidence_status": SIMULATED_NOT_EXECUTED,
            "coverage_id": coverage["coverage_id"],
            "task_id": task_id,
            "candidate_id": coverage["candidate_id"],
            "repo": coverage["repo"],
            "base_commit": coverage["base_commit"],
            "public_source_content_hash": coverage["public_source_content_hash"],
            "oracle_command_hash": coverage["proposed_oracle_command_hash"],
            "oracle_source_hash": implementation["oracle_source_hash"],
            "provenance_id": implementation["provenance_id"],
            "oracle_specification_id": implementation["oracle_specification_id"],
            "candidate_payload_hash": candidate_payload_hash,
            "candidate_patch_hash": candidate_patch_hash,
            "maximum_claim": coverage["maximum_claim"],
            "maximum_retrieval_policy": coverage["maximum_retrieval_policy"],
            "infrastructure_available": None,
            "oracle_executed": False,
            "reproduction_observed": None,
            "candidate_passed": None,
            "task_solved": False,
            "authorization_ref": "",
        }
        oracle["oracle_preview_id"] = _stable_id("gemoraprev", oracle)
        oracle_previews.append(oracle)

        admission = {
            "schema_version": ADMISSION_PREVIEW_SCHEMA_VERSION,
            "admission_preview_id": "",
            "created_at": created_at,
            "execution_mode": FIXTURE_DRY_RUN,
            "evidence_status": SIMULATED_NOT_EXECUTED,
            "decision": PREVIEW_INELIGIBLE,
            "eligible_for_real_admission": False,
            "coverage_id": coverage["coverage_id"],
            "task_id": task_id,
            "candidate_id": coverage["candidate_id"],
            "provenance_id": implementation["provenance_id"],
            "oracle_specification_id": implementation["oracle_specification_id"],
            "candidate_payload_hash": candidate_payload_hash,
            "candidate_patch_hash": candidate_patch_hash,
            "oracle_preview_ids": [oracle["oracle_preview_id"]],
            "maximum_claim": coverage["maximum_claim"],
            "maximum_retrieval_policy": coverage["maximum_retrieval_policy"],
            "authorization_ref": "",
        }
        admission["admission_preview_id"] = _stable_id("gemadmprev", admission)
        admission_previews.append(admission)

        sidecar = {
            "schema_version": SIDECAR_PREVIEW_SCHEMA_VERSION,
            "sidecar_preview_id": "",
            "created_at": created_at,
            "execution_mode": FIXTURE_DRY_RUN,
            "evidence_status": SIMULATED_NOT_EXECUTED,
            "publication_state": PREVIEW_NOT_RETRIEVABLE,
            "retrieval_policy": "blocked",
            "coverage_id": coverage["coverage_id"],
            "task_id": task_id,
            "candidate_id": coverage["candidate_id"],
            "provenance_id": implementation["provenance_id"],
            "oracle_specification_id": implementation["oracle_specification_id"],
            "candidate_payload_hash": candidate_payload_hash,
            "candidate_patch_hash": candidate_patch_hash,
            "oracle_preview_id": oracle["oracle_preview_id"],
            "admission_preview_id": admission["admission_preview_id"],
            "verified_claim_preview": coverage["maximum_claim"],
            "maximum_claim": coverage["maximum_claim"],
            "maximum_retrieval_policy": coverage["maximum_retrieval_policy"],
            "authorization_ref": "",
        }
        sidecar["sidecar_preview_id"] = _stable_id("gemsideprev", sidecar)
        sidecar_previews.append(sidecar)

    graph = {
        "schema_version": RUNTIME_GRAPH_SCHEMA_VERSION,
        "created_at": created_at,
        "execution_mode": FIXTURE_DRY_RUN,
        "evidence_status": SIMULATED_NOT_EXECUTED,
        "real_execution_authorized": False,
        "task_ids": list(T3_TASK_IDS),
        "package_fingerprint": _package_fingerprint(
            coverage_records, package["implementation_records"]
        ),
        "oracle_previews": oracle_previews,
        "admission_previews": admission_previews,
        "sidecar_previews": sidecar_previews,
    }
    validate_t3_runtime_preview_graph(graph, package=package, candidates=candidates)
    return graph


def validate_t3_runtime_preview_graph(
    graph: dict[str, Any],
    *,
    package: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> None:
    """Validate a complete candidate→coverage→preview linkage graph."""

    if not isinstance(graph, dict):
        raise GemRuntimeIntegrationValidationError("runtime graph must be a mapping")
    if graph.get("schema_version") != RUNTIME_GRAPH_SCHEMA_VERSION:
        raise GemRuntimeIntegrationValidationError("unsupported runtime graph schema")
    if set(graph) != _GRAPH_FIELDS:
        raise GemRuntimeIntegrationValidationError("runtime graph fields are not closed")
    if graph.get("execution_mode") != FIXTURE_DRY_RUN:
        raise GemRuntimeIntegrationValidationError("runtime preview must be fixture_dry_run")
    if graph.get("evidence_status") != SIMULATED_NOT_EXECUTED:
        raise GemRuntimeIntegrationValidationError("runtime preview cannot claim execution")
    if graph.get("real_execution_authorized") is not False:
        raise GemRuntimeIntegrationValidationError("runtime preview cannot authorize execution")
    if graph.get("task_ids") != list(T3_TASK_IDS):
        raise GemRuntimeIntegrationValidationError("runtime preview task order is not frozen T=3")

    coverage_records = package["coverage_records"]
    coverage_by_id = _unique_by(coverage_records, "coverage_id", "coverage")
    implementation_by_coverage = _unique_by(
        package["implementation_records"], "coverage_id", "implementation"
    )
    candidates_by_id = _unique_by(candidates, "candidate_id", "candidate")
    oracles = graph.get("oracle_previews")
    admissions = graph.get("admission_previews")
    sidecars = graph.get("sidecar_previews")
    if not all(isinstance(value, list) and len(value) == 3 for value in (oracles, admissions, sidecars)):
        raise GemRuntimeIntegrationValidationError("runtime preview requires three records per stage")
    oracle_by_id = _unique_by(oracles, "oracle_preview_id", "oracle preview")
    admission_by_id = _unique_by(admissions, "admission_preview_id", "admission preview")
    coverage_ids: list[str] = []

    for oracle in oracles:
        _validate_preview_common(oracle, ORACLE_PREVIEW_SCHEMA_VERSION)
        coverage = coverage_by_id.get(oracle.get("coverage_id"))
        if coverage is None:
            raise GemRuntimeIntegrationValidationError("oracle preview has unknown coverage_id")
        _validate_runtime_ceiling(oracle, coverage)
        _validate_implementation_linkage(
            oracle, implementation_by_coverage[coverage["coverage_id"]]
        )
        if oracle.get("candidate_id") not in candidates_by_id:
            raise GemRuntimeIntegrationValidationError("oracle preview has unknown candidate_id")
        _validate_candidate_evidence_linkage(
            oracle, candidates_by_id[oracle["candidate_id"]]
        )
        if oracle.get("oracle_command_hash") != coverage["proposed_oracle_command_hash"]:
            raise GemRuntimeIntegrationValidationError("oracle command hash mismatch")
        if oracle.get("public_source_content_hash") != coverage["public_source_content_hash"]:
            raise GemRuntimeIntegrationValidationError("public source hash mismatch")
        if (
            oracle.get("infrastructure_available") is not None
            or oracle.get("oracle_executed") is not False
            or oracle.get("reproduction_observed") is not None
            or oracle.get("candidate_passed") is not None
            or oracle.get("task_solved") is not False
        ):
            raise GemRuntimeIntegrationValidationError(
                "preview cannot claim infrastructure, execution, reproduction, or success"
            )
        if oracle.get("oracle_preview_id") != _stable_id("gemoraprev", oracle):
            raise GemRuntimeIntegrationValidationError("oracle_preview_id mismatch")
        coverage_ids.append(coverage["coverage_id"])

    if set(coverage_ids) != set(coverage_by_id) or len(set(coverage_ids)) != 3:
        raise GemRuntimeIntegrationValidationError("coverage linkage must be one-to-one")

    for admission in admissions:
        _validate_preview_common(admission, ADMISSION_PREVIEW_SCHEMA_VERSION)
        coverage = coverage_by_id.get(admission.get("coverage_id"))
        if coverage is None:
            raise GemRuntimeIntegrationValidationError("admission preview has unknown coverage_id")
        _validate_runtime_ceiling(admission, coverage)
        _validate_implementation_linkage(
            admission, implementation_by_coverage[coverage["coverage_id"]]
        )
        candidate = candidates_by_id.get(admission.get("candidate_id"))
        if candidate is None:
            raise GemRuntimeIntegrationValidationError("admission preview has unknown candidate_id")
        _validate_candidate_evidence_linkage(admission, candidate)
        if admission.get("decision") != PREVIEW_INELIGIBLE or admission.get("eligible_for_real_admission") is not False:
            raise GemRuntimeIntegrationValidationError("preview cannot become real admission evidence")
        linked = admission.get("oracle_preview_ids")
        if not isinstance(linked, list) or len(linked) != 1 or linked[0] not in oracle_by_id:
            raise GemRuntimeIntegrationValidationError("admission-to-oracle linkage is incomplete")
        if oracle_by_id[linked[0]]["coverage_id"] != coverage["coverage_id"]:
            raise GemRuntimeIntegrationValidationError("admission crosses coverage records")
        if admission.get("admission_preview_id") != _stable_id("gemadmprev", admission):
            raise GemRuntimeIntegrationValidationError("admission_preview_id mismatch")

    for sidecar in sidecars:
        _validate_preview_common(sidecar, SIDECAR_PREVIEW_SCHEMA_VERSION)
        coverage = coverage_by_id.get(sidecar.get("coverage_id"))
        if coverage is None:
            raise GemRuntimeIntegrationValidationError("sidecar preview has unknown coverage_id")
        _validate_runtime_ceiling(sidecar, coverage)
        _validate_implementation_linkage(
            sidecar, implementation_by_coverage[coverage["coverage_id"]]
        )
        candidate = candidates_by_id.get(sidecar.get("candidate_id"))
        if candidate is None:
            raise GemRuntimeIntegrationValidationError("sidecar preview has unknown candidate_id")
        _validate_candidate_evidence_linkage(sidecar, candidate)
        if sidecar.get("publication_state") != PREVIEW_NOT_RETRIEVABLE or sidecar.get("retrieval_policy") != "blocked":
            raise GemRuntimeIntegrationValidationError("preview sidecar must remain blocked")
        if sidecar.get("verified_claim_preview") != coverage["maximum_claim"]:
            raise GemRuntimeIntegrationValidationError("preview claim exceeds frozen ceiling")
        oracle = oracle_by_id.get(sidecar.get("oracle_preview_id"))
        admission = admission_by_id.get(sidecar.get("admission_preview_id"))
        if oracle is None or admission is None:
            raise GemRuntimeIntegrationValidationError("sidecar preview linkage is incomplete")
        if {oracle["coverage_id"], admission["coverage_id"]} != {coverage["coverage_id"]}:
            raise GemRuntimeIntegrationValidationError("sidecar crosses coverage records")
        if sidecar.get("sidecar_preview_id") != _stable_id("gemsideprev", sidecar):
            raise GemRuntimeIntegrationValidationError("sidecar_preview_id mismatch")

    if graph.get("package_fingerprint") != _package_fingerprint(
        coverage_records, package["implementation_records"]
    ):
        raise GemRuntimeIntegrationValidationError("package fingerprint mismatch")


def _validate_preview_common(record: dict[str, Any], schema_version: str) -> None:
    if not isinstance(record, dict) or record.get("schema_version") != schema_version:
        raise GemRuntimeIntegrationValidationError("invalid preview schema")
    expected_fields = {
        ORACLE_PREVIEW_SCHEMA_VERSION: _ORACLE_FIELDS,
        ADMISSION_PREVIEW_SCHEMA_VERSION: _ADMISSION_FIELDS,
        SIDECAR_PREVIEW_SCHEMA_VERSION: _SIDECAR_FIELDS,
    }[schema_version]
    if set(record) != expected_fields:
        raise GemRuntimeIntegrationValidationError("preview fields are not closed")
    if record.get("execution_mode") != FIXTURE_DRY_RUN or record.get("evidence_status") != SIMULATED_NOT_EXECUTED:
        raise GemRuntimeIntegrationValidationError("preview record claims execution")
    if record.get("authorization_ref") != "":
        raise GemRuntimeIntegrationValidationError("dry-run preview cannot carry authorization")


def _validate_runtime_ceiling(record: dict[str, Any], coverage: dict[str, Any]) -> None:
    for key in ("coverage_id", "task_id", "candidate_id", "maximum_claim", "maximum_retrieval_policy"):
        if record.get(key) != coverage.get(key):
            raise GemRuntimeIntegrationValidationError(f"runtime {key} does not match coverage")


def _validate_implementation_linkage(
    record: dict[str, Any], implementation: dict[str, Any]
) -> None:
    for key in ("provenance_id", "oracle_specification_id"):
        if record.get(key) != implementation.get(key):
            raise GemRuntimeIntegrationValidationError(
                f"runtime {key} does not match deterministic package linkage"
            )
    if "oracle_source_hash" in record and record.get("oracle_source_hash") != implementation.get(
        "oracle_source_hash"
    ):
        raise GemRuntimeIntegrationValidationError("oracle source hash mismatch")


def _validate_candidate_evidence_linkage(
    record: dict[str, Any], candidate: dict[str, Any]
) -> None:
    intervention = candidate.get("intervention")
    expected_patch_hash = intervention.get("patch_hash") if isinstance(intervention, dict) else None
    if record.get("candidate_payload_hash") != _candidate_payload_hash(candidate):
        raise GemRuntimeIntegrationValidationError("candidate payload hash mismatch")
    if record.get("candidate_patch_hash") != expected_patch_hash:
        raise GemRuntimeIntegrationValidationError("candidate patch hash mismatch")


def _validate_candidate_coverage_link(
    candidate: dict[str, Any],
    coverage: dict[str, Any],
    provenance: dict[str, Any],
) -> None:
    validate_gem_memory_candidate(candidate)
    linkage = provenance["candidate_linkage"]
    for key in ("task_id", "candidate_id"):
        if candidate.get(key) != coverage.get(key) or linkage.get(key) != coverage.get(key):
            raise GemRuntimeIntegrationValidationError(f"candidate {key} linkage mismatch")
    if candidate.get("repo") != coverage.get("repo"):
        raise GemRuntimeIntegrationValidationError("candidate repo linkage mismatch")
    if linkage.get("created_from_public_artifacts_only") is not True:
        raise GemRuntimeIntegrationValidationError("candidate provenance is not public-only")


def _load_public_problem_statements(path: str | Path) -> dict[str, str]:
    payload = _read_json_object(Path(path))
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise GemRuntimeIntegrationValidationError("public subset tasks must be a list")
    statements: dict[str, str] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("instance_id") or "")
        if task_id in T3_TASK_IDS:
            statements[task_id] = str(task.get("problem_statement") or "")
    return statements


def _package_fingerprint(
    records: list[dict[str, Any]], implementations: list[dict[str, Any]]
) -> str:
    implementation_by_coverage = {
        item["coverage_id"]: item for item in implementations
    }
    payload = [
        {
            "coverage_id": record["coverage_id"],
            "provenance_id": implementation_by_coverage[record["coverage_id"]][
                "provenance_id"
            ],
            "oracle_specification_id": implementation_by_coverage[
                record["coverage_id"]
            ]["oracle_specification_id"],
            "oracle_source_hash": implementation_by_coverage[record["coverage_id"]][
                "oracle_source_hash"
            ],
        }
        for record in records
    ]
    return "sha256:" + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _candidate_payload_hash(candidate: dict[str, Any]) -> str:
    encoded = json.dumps(
        candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_frozen_loader_inputs(
    application_config: dict[str, Any], paths: tuple[Path, ...]
) -> None:
    expected = {
        str(record.get("path")): str(record.get("sha256"))
        for record in application_config.get("frozen_artifacts", [])
    }
    for path in paths:
        try:
            relative = path.resolve().relative_to(APPLICATION_ROOT.resolve()).as_posix()
        except ValueError as exc:
            raise GemRuntimeIntegrationValidationError(
                "frozen loader input escapes application root"
            ) from exc
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected.get(relative) != digest:
            raise GemRuntimeIntegrationValidationError(
                f"frozen artifact hash mismatch: {relative}"
            )


def _stable_id(prefix: str, record: dict[str, Any]) -> str:
    payload = deepcopy(record)
    own_id_field = {
        "gemoraprev": "oracle_preview_id",
        "gemadmprev": "admission_preview_id",
        "gemsideprev": "sidecar_preview_id",
    }[prefix]
    payload[own_id_field] = ""
    payload.pop("created_at", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:20]}"


def _unique_by(records: list[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise GemRuntimeIntegrationValidationError(f"{label} records must be a list")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not str(record.get(key) or ""):
            raise GemRuntimeIntegrationValidationError(f"{label} missing {key}")
        value = str(record[key])
        if value in result:
            raise GemRuntimeIntegrationValidationError(f"duplicate {label} {key}: {value}")
        result[value] = record
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GemRuntimeIntegrationValidationError(f"expected JSON object: {path}")
    return payload


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise GemRuntimeIntegrationValidationError(f"expected JSON record list: {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise GemRuntimeIntegrationValidationError(f"expected JSONL object: {path}")
        records.append(record)
    return records
