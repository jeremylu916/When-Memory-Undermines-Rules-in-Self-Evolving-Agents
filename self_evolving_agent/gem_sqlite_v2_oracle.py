"""SQLite-v2 public-oracle contract for ``django__django-10554``.

This module is intentionally Python 3.6 compatible.  It is independent of the
legacy PostgreSQL package and never rewrites or falls back to that package.
"""

import hashlib
import json
from pathlib import Path

from self_evolving_agent.gem_public_oracle import (
    PublicOracleContractError,
    require_v2_process_authorization,
    stable_record_id,
)


PACKAGE_DIRECTORY = "gem_t3_public_oracle_coverage_sqlite_v2"
PACKAGE_SCHEMA_VERSION = "gem.sqlite_v2_public_oracle_package.v2"
OBSERVATION_SCHEMA_VERSION = "gem.sqlite_v2_semantic_observation.v2"
PAIR_RESULT_SCHEMA_VERSION = "gem.sqlite_v2_discrimination_result.v2"
TASK_ID = "django__django-10554"
APPROVAL_ID = "gem-t3-sqlite-20260712"


class SQLiteV2ContractError(ValueError):
    """Raised when SQLite-v2 linkage or evidence is not closed and exact."""


def load_sqlite_v2_contract(application_root, script_path):
    root = Path(application_root).resolve()
    package = root / "runs" / PACKAGE_DIRECTORY
    manifest = _read_json(package / "package_manifest.json")
    if set(manifest) != {
        "schema_version", "package_id", "package_role", "legacy_package_policy",
        "files", "candidate_source",
    }:
        raise SQLiteV2ContractError("SQLite-v2 package manifest fields are not closed")
    if manifest["schema_version"] != PACKAGE_SCHEMA_VERSION:
        raise SQLiteV2ContractError("unsupported SQLite-v2 package schema")
    if manifest["package_role"] != "sqlite_primary_semantic_oracle":
        raise SQLiteV2ContractError("SQLite-v2 package role mismatch")
    if manifest["legacy_package_policy"] != "preserve_byte_for_byte":
        raise SQLiteV2ContractError("legacy PostgreSQL package preservation is not asserted")

    expected_files = {
        "coverage_records.json", "coverage_provenance.json", "oracle_specifications.json"
    }
    file_hashes = manifest["files"]
    if not isinstance(file_hashes, dict) or set(file_hashes) != expected_files:
        raise SQLiteV2ContractError("SQLite-v2 package file manifest mismatch")
    for name, digest in file_hashes.items():
        if digest != _sha256_bytes((package / name).read_bytes()):
            raise SQLiteV2ContractError("SQLite-v2 package file hash mismatch")

    candidate_path = root / manifest["candidate_source"]["path"]
    if _sha256_bytes(candidate_path.read_bytes()) != manifest["candidate_source"]["sha256"]:
        raise SQLiteV2ContractError("frozen candidate source hash mismatch")
    coverage_records = _read_json(package / "coverage_records.json")
    provenance_records = _read_json(package / "coverage_provenance.json")["records"]
    specifications = _read_json(package / "oracle_specifications.json")["specifications"]
    if len(coverage_records) != 3 or len(provenance_records) != 3 or len(specifications) != 3:
        raise SQLiteV2ContractError("SQLite-v2 package must contain exactly T=3")
    _validate_package_records(root, coverage_records, provenance_records, specifications)
    coverage = _one(coverage_records, "task_id", TASK_ID)
    provenance = _one(provenance_records, "coverage_id", coverage["coverage_id"])
    specification = _one(specifications, "coverage_id", coverage["coverage_id"])
    candidate = _one(_read_jsonl(candidate_path), "task_id", TASK_ID)
    if candidate.get("candidate_id") != coverage["candidate_id"]:
        raise SQLiteV2ContractError("task/candidate linkage mismatch")
    if candidate.get("repo") != coverage["repo"]:
        raise SQLiteV2ContractError("task/repo linkage mismatch")
    relative_script = Path(script_path).resolve().relative_to(root).as_posix()
    if specification["proposed_script"] != relative_script:
        raise SQLiteV2ContractError("SQLite-v2 entrypoint mismatch")
    if _sha256_text(specification["proposed_oracle_command"]) != coverage[
        "proposed_oracle_command_hash"
    ]:
        raise SQLiteV2ContractError("SQLite-v2 command hash mismatch")
    if specification["proposed_oracle_command_hash"] != coverage[
        "proposed_oracle_command_hash"
    ]:
        raise SQLiteV2ContractError("SQLite-v2 coverage/specification mismatch")
    intervention = candidate.get("intervention") or {}
    contract = dict(coverage)
    contract.update({
        "package_id": manifest["package_id"],
        "provenance_id": stable_record_id("gemprov", provenance),
        "oracle_specification_id": stable_record_id("gemoraspec", specification),
        "oracle_source_hash": _sha256_bytes(Path(script_path).read_bytes()),
        "candidate_payload_hash": _sha256_text(json.dumps(
            candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )),
        "candidate_patch_hash": str(intervention.get("patch_hash") or ""),
    })
    if not contract["candidate_patch_hash"].startswith("sha256:"):
        raise SQLiteV2ContractError("candidate patch hash is missing")
    return contract


def require_sqlite_v2_authorization(application_root=None):
    """Use the same digest-bound launcher authorization as every real oracle."""

    root = Path(application_root) if application_root is not None else Path(__file__).resolve().parents[1]
    try:
        return require_v2_process_authorization(root, TASK_ID, "Exec")
    except PublicOracleContractError as exc:
        raise SQLiteV2ContractError(str(exc))


def build_semantic_observation(contract, subject, authorization_ref, fingerprint):
    record = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "package_id": contract["package_id"],
        "task_id": contract["task_id"],
        "coverage_id": contract["coverage_id"],
        "candidate_id": contract["candidate_id"],
        "repo": contract["repo"],
        "base_commit": contract["base_commit"],
        "provenance_id": contract["provenance_id"],
        "oracle_specification_id": contract["oracle_specification_id"],
        "oracle_command_hash": contract["proposed_oracle_command_hash"],
        "oracle_source_hash": contract["oracle_source_hash"],
        "authorization_ref": str(authorization_ref or ""),
        "subject_role": subject["subject_role"],
        "candidate_payload_hash": subject["candidate_payload_hash"],
        "candidate_patch_hash": subject["candidate_patch_hash"],
        "subject_worktree_hash": subject["subject_worktree_hash"],
        "observed_patch_hash": subject["observed_patch_hash"],
        "backend_kind": "sqlite_in_memory",
        "oracle_executed": True,
        "semantic_fingerprint": fingerprint,
        "candidate_passed": None,
        "task_solved": False,
        "admission_eligible": False,
    }
    validate_semantic_observation(record)
    return record


def validate_semantic_observation(record):
    required = {
        "schema_version", "package_id", "task_id", "coverage_id", "candidate_id",
        "repo", "base_commit", "provenance_id", "oracle_specification_id",
        "oracle_command_hash", "oracle_source_hash", "authorization_ref",
        "subject_role", "candidate_payload_hash", "candidate_patch_hash",
        "subject_worktree_hash", "observed_patch_hash", "backend_kind",
        "oracle_executed", "semantic_fingerprint", "candidate_passed",
        "task_solved", "admission_eligible",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise SQLiteV2ContractError("SQLite-v2 observation fields are not closed")
    if record["schema_version"] != OBSERVATION_SCHEMA_VERSION:
        raise SQLiteV2ContractError("unsupported SQLite-v2 observation schema")
    if record["task_id"] != TASK_ID or record["backend_kind"] != "sqlite_in_memory":
        raise SQLiteV2ContractError("SQLite-v2 observation scope mismatch")
    if record["subject_role"] not in ("frozen_base", "candidate"):
        raise SQLiteV2ContractError("invalid observation subject role")
    if not record["authorization_ref"] or record["oracle_executed"] is not True:
        raise SQLiteV2ContractError("observation is not authorized and executed")
    if record["candidate_passed"] is not None or record["task_solved"] is not False:
        raise SQLiteV2ContractError("single observation cannot claim success")
    if record["admission_eligible"] is not False:
        raise SQLiteV2ContractError("single observation cannot authorize admission")
    fingerprint = record["semantic_fingerprint"]
    if not isinstance(fingerprint, dict) or set(fingerprint) != {
        "derived_status", "derived_primary_keys", "original_status",
        "original_primary_keys", "error_stage", "error_type",
    }:
        raise SQLiteV2ContractError("semantic fingerprint fields are not closed")
    if fingerprint["derived_status"] not in ("completed", "error"):
        raise SQLiteV2ContractError("invalid derived status")
    if fingerprint["original_status"] not in ("completed", "error"):
        raise SQLiteV2ContractError("invalid original status")
    for key in ("derived_primary_keys", "original_primary_keys"):
        if not isinstance(fingerprint[key], list) or not all(
            isinstance(value, int) for value in fingerprint[key]
        ):
            raise SQLiteV2ContractError("semantic primary keys must be integers")


def compare_base_candidate(base, candidate):
    validate_semantic_observation(base)
    validate_semantic_observation(candidate)
    linkage = (
        "package_id", "task_id", "coverage_id", "candidate_id", "repo", "base_commit",
        "provenance_id", "oracle_specification_id", "oracle_command_hash",
        "oracle_source_hash", "candidate_payload_hash", "candidate_patch_hash",
    )
    if any(base[key] != candidate[key] for key in linkage):
        raise SQLiteV2ContractError("base/candidate SQLite-v2 linkage mismatch")
    if base["subject_role"] != "frozen_base" or candidate["subject_role"] != "candidate":
        raise SQLiteV2ContractError("SQLite-v2 comparison requires base then candidate")
    distinguishable = base["semantic_fingerprint"] != candidate["semantic_fingerprint"]
    candidate_fixed = (
        distinguishable
        and base["semantic_fingerprint"]["original_status"] == "error"
        and candidate["semantic_fingerprint"]["original_status"] == "completed"
    )
    status = "discriminating_pass" if candidate_fixed else "insufficient_for_admission"
    result = {
        "schema_version": PAIR_RESULT_SCHEMA_VERSION,
        "package_id": base["package_id"],
        "task_id": TASK_ID,
        "coverage_id": base["coverage_id"],
        "base_observation_id": stable_record_id("gemsqliteobs", base),
        "candidate_observation_id": stable_record_id("gemsqliteobs", candidate),
        "sqlite_discriminates": candidate_fixed,
        "status": status,
        "oracle_pass": candidate_fixed,
        "candidate_quarantined": not candidate_fixed,
        "admission_eligible": candidate_fixed,
        "task_solved": False,
        "postgresql_confirmation": "optional_not_required",
    }
    return result


def _validate_package_records(root, coverage, provenance, specifications):
    task_ids = ("astropy__astropy-13033", TASK_ID, "matplotlib__matplotlib-14623")
    if [item.get("task_id") for item in coverage] != list(task_ids):
        raise SQLiteV2ContractError("SQLite-v2 coverage task order/scope mismatch")
    coverage_ids = [item.get("coverage_id") for item in coverage]
    if len(set(coverage_ids)) != 3:
        raise SQLiteV2ContractError("SQLite-v2 coverage ids must be unique")
    if {item.get("coverage_id") for item in provenance} != set(coverage_ids):
        raise SQLiteV2ContractError("SQLite-v2 provenance linkage mismatch")
    if {item.get("coverage_id") for item in specifications} != set(coverage_ids):
        raise SQLiteV2ContractError("SQLite-v2 specification linkage mismatch")
    coverage_fields = {
        "schema_version", "coverage_id", "task_id", "candidate_id", "repo",
        "base_commit", "public_source_ref", "public_source_content_hash",
        "coverage_class", "maximum_claim", "maximum_retrieval_policy",
        "proposed_oracle_command_hash", "execution_state",
    }
    for item in coverage:
        if set(item) != coverage_fields or item["execution_state"] != "prepared_not_executed":
            raise SQLiteV2ContractError("SQLite-v2 coverage record is not closed/prepared")
    legacy = root / "runs" / "gem_t3_public_oracle_coverage_preparation"
    legacy_coverage = _read_json(legacy / "coverage_records.json")
    legacy_provenance = _read_json(legacy / "coverage_provenance.json")["records"]
    legacy_specs = _read_json(legacy / "oracle_specifications.json")["specifications"]
    for task_id in ("astropy__astropy-13033", "matplotlib__matplotlib-14623"):
        if _one(coverage, "task_id", task_id) != _one(legacy_coverage, "task_id", task_id):
            raise SQLiteV2ContractError("unrelated coverage record drift")
        coverage_id = _one(coverage, "task_id", task_id)["coverage_id"]
        if _one(provenance, "coverage_id", coverage_id) != _one(legacy_provenance, "coverage_id", coverage_id):
            raise SQLiteV2ContractError("unrelated provenance record drift")
        if _one(specifications, "coverage_id", coverage_id) != _one(legacy_specs, "coverage_id", coverage_id):
            raise SQLiteV2ContractError("unrelated specification drift")


def _one(records, key, value):
    matches = [record for record in records if record.get(key) == value]
    if len(matches) != 1:
        raise SQLiteV2ContractError("SQLite-v2 package linkage is missing or duplicated")
    return matches[0]


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_jsonl(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _sha256_bytes(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_text(value):
    return _sha256_bytes(value.encode("utf-8"))
