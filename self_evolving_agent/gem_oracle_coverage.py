"""Static public-oracle coverage registration for the fixed GEM T=3 slice.

This module registers what a later oracle *may* test. It never runs an oracle,
creates an admission event, or makes a candidate retrievable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


ORACLE_COVERAGE_SCHEMA_VERSION = "gem.oracle_coverage.v1"
PREPARED_NOT_EXECUTED = "prepared_not_executed"

NARROW_PUBLIC_REPRODUCTION = "narrow_public_reproduction"
BROAD_ISSUE_REPRODUCTION = "broad_issue_reproduction"
COVERAGE_CLASSES = (
    NARROW_PUBLIC_REPRODUCTION,
    BROAD_ISSUE_REPRODUCTION,
)

NARROW_ONLY = "narrow_only"
NOT_RETRIEVABLE_UNTIL_SCOPE_REVIEW = "not_retrievable_until_scope_review"
ORACLE_COVERAGE_PROVENANCE_SCHEMA_VERSION = "gem.oracle_coverage_provenance.v1"
PUBLIC_ORACLE_SPECIFICATION_SCHEMA_VERSION = "gem.public_oracle_specification.v1"


T3_COVERAGE_CONTRACT = {
    "astropy__astropy-13033": {
        "candidate_id": "gemcand_43259cf942f351264b2e",
        "repo": "astropy/astropy",
        "coverage_class": NARROW_PUBLIC_REPRODUCTION,
        "maximum_claim": "The later-required-column-missing diagnostic only.",
        "maximum_retrieval_policy": NARROW_ONLY,
    },
    "django__django-10554": {
        "candidate_id": "gemcand_1e69fb5eb9a208944922",
        "repo": "django/django",
        "coverage_class": NARROW_PUBLIC_REPRODUCTION,
        "maximum_claim": (
            "Re-evaluation safety for this derived-union/order-by sequence on "
            "the specified public database setup."
        ),
        "maximum_retrieval_policy": NARROW_ONLY,
    },
    "matplotlib__matplotlib-14623": {
        "candidate_id": "gemcand_750e4191afe4d1c88c6b",
        "repo": "matplotlib/matplotlib",
        "coverage_class": BROAD_ISSUE_REPRODUCTION,
        "maximum_claim": (
            "The public issue's stated linear-and-log descending-limit behavior, "
            "at the frozen public version/context."
        ),
        "maximum_retrieval_policy": NOT_RETRIEVABLE_UNTIL_SCOPE_REVIEW,
    },
}
T3_TASK_IDS = tuple(T3_COVERAGE_CONTRACT)

_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}\Z")
_FORBIDDEN_MARKERS = (
    "hidden_test",
    "hidden test",
    "hidden_assertion",
    "gold_patch",
    "gold patch",
    "reference_patch",
    "reference patch",
    "official_stdout",
    "official stdout",
    "official_stderr",
    "official stderr",
    "patch-derived",
    "patch derived",
    "model_patch",
    "prediction_path",
    "prediction:",
)
_EXECUTION_RESULT_FIELDS = {
    "oracle_run_id",
    "oracle_success",
    "pre_patch_result",
    "post_patch_result",
    "fail_before",
    "pass_after",
    "admission_event_id",
    "verified_memory_id",
    "result",
    "status",
}
_REQUIRED_FIELDS = {
    "schema_version",
    "coverage_id",
    "task_id",
    "candidate_id",
    "repo",
    "base_commit",
    "public_source_ref",
    "public_source_content_hash",
    "coverage_class",
    "maximum_claim",
    "maximum_retrieval_policy",
    "proposed_oracle_command_hash",
    "execution_state",
}
_PROVENANCE_MANIFEST_FIELDS = {"schema_version", "records"}
_PROVENANCE_RECORD_FIELDS = {
    "candidate_linkage",
    "classification_basis",
    "coverage_id",
    "public_repository_ref",
    "public_source_content_retention",
    "public_source_snapshot",
    "public_source_summary",
    "scope_guard",
}
_CANDIDATE_LINKAGE_FIELDS = {
    "candidate_id",
    "candidate_source",
    "created_from_public_artifacts_only",
    "offline_run_id",
    "task_id",
}
_SPECIFICATION_MANIFEST_FIELDS = {"schema_version", "specifications"}
_SPECIFICATION_FIELDS = {
    "coverage_id",
    "environment_constraints",
    "execution_state",
    "fail_predicate",
    "maximum_claim",
    "maximum_retrieval_policy",
    "pass_predicate",
    "proposed_oracle_command",
    "proposed_oracle_command_hash",
    "proposed_script",
    "public_observable",
    "setup",
    "stop_or_quarantine",
    "task_id",
}


class GemOracleCoverageValidationError(ValueError):
    """Raised when a coverage registration is malformed or exceeds its scope."""


def build_t3_oracle_coverage_records(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    public_registrations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the fixed T=3 registration records without executing an oracle.

    Each registration supplies only public preparation inputs: ``base_commit``,
    ``public_source_ref``, public source text, and a proposed command/script.
    The text is hashed and deliberately omitted from the emitted record.
    """

    candidates_by_task = _candidates_by_task(candidates)
    records: list[dict[str, Any]] = []
    for task_id in T3_TASK_IDS:
        candidate = candidates_by_task.get(task_id)
        if candidate is None:
            raise GemOracleCoverageValidationError(
                f"missing T=3 candidate for task: {task_id}"
            )
        registration = public_registrations.get(task_id)
        if not isinstance(registration, dict):
            raise GemOracleCoverageValidationError(
                f"missing public registration for task: {task_id}"
            )
        contract = T3_COVERAGE_CONTRACT[task_id]
        _validate_candidate_link(candidate, task_id, contract)

        public_source_content = _required_string(
            registration.get("public_source_content"),
            "public_source_content",
        )
        proposed_oracle_command = _required_string(
            registration.get("proposed_oracle_command"),
            "proposed_oracle_command",
        )
        record = {
            "schema_version": ORACLE_COVERAGE_SCHEMA_VERSION,
            "coverage_id": "",
            "task_id": task_id,
            "candidate_id": contract["candidate_id"],
            "repo": contract["repo"],
            "base_commit": _required_string(registration.get("base_commit"), "base_commit"),
            "public_source_ref": _required_string(
                registration.get("public_source_ref"),
                "public_source_ref",
            ),
            "public_source_content_hash": _sha256(public_source_content),
            "coverage_class": contract["coverage_class"],
            "maximum_claim": contract["maximum_claim"],
            "maximum_retrieval_policy": contract["maximum_retrieval_policy"],
            "proposed_oracle_command_hash": _sha256(proposed_oracle_command),
            "execution_state": PREPARED_NOT_EXECUTED,
        }
        record["coverage_id"] = gem_oracle_coverage_id(record)
        validate_gem_oracle_coverage_record(record)
        records.append(record)

    validate_t3_oracle_coverage_records(records)
    return records


def validate_t3_oracle_coverage_records(records: list[dict[str, Any]]) -> None:
    """Validate the immutable three-task coverage slice and no extra tasks."""

    if not isinstance(records, list) or len(records) != len(T3_TASK_IDS):
        raise GemOracleCoverageValidationError("coverage must contain exactly three records")
    task_ids = [str(record.get("task_id") or "") for record in records]
    if set(task_ids) != set(T3_TASK_IDS) or len(set(task_ids)) != len(T3_TASK_IDS):
        raise GemOracleCoverageValidationError(
            "coverage task ids must be exactly the fixed T=3 tasks"
        )
    for record in records:
        validate_gem_oracle_coverage_record(record)


def validate_gem_oracle_coverage_record(record: dict[str, Any]) -> None:
    """Validate one static registration; execution evidence is not allowed here."""

    if not isinstance(record, dict):
        raise GemOracleCoverageValidationError("coverage record must be a mapping")
    for key in _REQUIRED_FIELDS:
        if key not in record:
            raise GemOracleCoverageValidationError(f"missing coverage field: {key}")
    unexpected_fields = set(record).difference(_REQUIRED_FIELDS)
    if unexpected_fields:
        raise GemOracleCoverageValidationError(
            f"unexpected coverage fields: {sorted(unexpected_fields)}"
        )
    if record["schema_version"] != ORACLE_COVERAGE_SCHEMA_VERSION:
        raise GemOracleCoverageValidationError("unsupported coverage schema_version")
    task_id = str(record["task_id"])
    contract = T3_COVERAGE_CONTRACT.get(task_id)
    if contract is None:
        raise GemOracleCoverageValidationError("coverage task is outside the fixed T=3 set")
    for key in (
        "coverage_id",
        "candidate_id",
        "repo",
        "base_commit",
        "public_source_ref",
        "public_source_content_hash",
        "coverage_class",
        "maximum_claim",
        "maximum_retrieval_policy",
        "proposed_oracle_command_hash",
        "execution_state",
    ):
        if not isinstance(record[key], str) or not record[key]:
            raise GemOracleCoverageValidationError(f"{key} must be a non-empty string")
    if record["candidate_id"] != contract["candidate_id"]:
        raise GemOracleCoverageValidationError("task/candidate linkage does not match T=3")
    if record["repo"] != contract["repo"]:
        raise GemOracleCoverageValidationError("task/repo linkage does not match T=3")
    if record["coverage_class"] not in COVERAGE_CLASSES:
        raise GemOracleCoverageValidationError("invalid coverage_class")
    if record["coverage_class"] != contract["coverage_class"]:
        raise GemOracleCoverageValidationError("coverage_class does not match fixed T=3 plan")
    if record["maximum_claim"] != contract["maximum_claim"]:
        raise GemOracleCoverageValidationError("maximum_claim exceeds or differs from plan")
    if record["maximum_retrieval_policy"] != contract["maximum_retrieval_policy"]:
        raise GemOracleCoverageValidationError("maximum retrieval policy does not match plan")
    if record["execution_state"] != PREPARED_NOT_EXECUTED:
        raise GemOracleCoverageValidationError(
            "coverage execution_state must remain prepared_not_executed"
        )
    if not _COMMIT_RE.fullmatch(record["base_commit"]):
        raise GemOracleCoverageValidationError("base_commit must be a commit hash")
    if not record["public_source_ref"].startswith(("https://", "http://")):
        raise GemOracleCoverageValidationError("public_source_ref must be a public URL")
    for key in ("public_source_content_hash", "proposed_oracle_command_hash"):
        if not _HASH_RE.fullmatch(record[key]):
            raise GemOracleCoverageValidationError(f"{key} must be a sha256 hash")
    if record["coverage_id"] != gem_oracle_coverage_id(record):
        raise GemOracleCoverageValidationError("coverage_id does not match payload")
    if set(record).intersection(_EXECUTION_RESULT_FIELDS):
        raise GemOracleCoverageValidationError(
            "prepared coverage must not contain oracle, admission, or sidecar results"
        )
    _assert_no_forbidden_content(record)
    _assert_scope_boundary(record, contract)


def validate_gem_oracle_coverage_provenance(
    manifest: dict[str, Any],
) -> None:
    """Validate the closed provenance companion manifest without execution."""

    _validate_closed_mapping(
        manifest,
        _PROVENANCE_MANIFEST_FIELDS,
        "coverage provenance manifest",
    )
    if manifest["schema_version"] != ORACLE_COVERAGE_PROVENANCE_SCHEMA_VERSION:
        raise GemOracleCoverageValidationError("unsupported provenance schema_version")
    records = manifest["records"]
    if not isinstance(records, list):
        raise GemOracleCoverageValidationError("provenance records must be a list")
    for record in records:
        _validate_closed_mapping(record, _PROVENANCE_RECORD_FIELDS, "provenance record")
        for key in (
            "coverage_id",
            "public_repository_ref",
            "public_source_content_retention",
            "public_source_snapshot",
            "public_source_summary",
            "scope_guard",
        ):
            _require_non_empty_string(record[key], f"provenance {key}")
        if not record["public_repository_ref"].startswith(("https://", "http://")):
            raise GemOracleCoverageValidationError(
                "provenance public_repository_ref must be a public URL"
            )
        if record["public_source_content_retention"] != "hash_only":
            raise GemOracleCoverageValidationError(
                "provenance must retain public source content as hash_only"
            )
        if not isinstance(record["classification_basis"], list) or not all(
            isinstance(item, str) and item for item in record["classification_basis"]
        ):
            raise GemOracleCoverageValidationError(
                "provenance classification_basis must be a non-empty string list"
            )
        linkage = record["candidate_linkage"]
        _validate_closed_mapping(linkage, _CANDIDATE_LINKAGE_FIELDS, "candidate linkage")
        for key in ("candidate_id", "candidate_source", "offline_run_id", "task_id"):
            _require_non_empty_string(linkage[key], f"candidate linkage {key}")
        if linkage["created_from_public_artifacts_only"] is not True:
            raise GemOracleCoverageValidationError(
                "candidate linkage must be limited to public artifacts"
            )
        _assert_no_forbidden_content(record)


def validate_public_oracle_specifications(manifest: dict[str, Any]) -> None:
    """Validate the closed, non-executing public-oracle specification manifest."""

    _validate_closed_mapping(
        manifest,
        _SPECIFICATION_MANIFEST_FIELDS,
        "oracle specification manifest",
    )
    if manifest["schema_version"] != PUBLIC_ORACLE_SPECIFICATION_SCHEMA_VERSION:
        raise GemOracleCoverageValidationError("unsupported specification schema_version")
    specifications = manifest["specifications"]
    if not isinstance(specifications, list):
        raise GemOracleCoverageValidationError("specifications must be a list")
    for specification in specifications:
        _validate_closed_mapping(
            specification,
            _SPECIFICATION_FIELDS,
            "oracle specification",
        )
        for key in _SPECIFICATION_FIELDS.difference({"environment_constraints"}):
            _require_non_empty_string(specification[key], f"specification {key}")
        if not isinstance(specification["environment_constraints"], list) or not all(
            isinstance(item, str) and item
            for item in specification["environment_constraints"]
        ):
            raise GemOracleCoverageValidationError(
                "specification environment_constraints must be a non-empty string list"
            )
        if specification["execution_state"] != PREPARED_NOT_EXECUTED:
            raise GemOracleCoverageValidationError(
                "oracle specification must remain prepared_not_executed"
            )
        expected_hash = _sha256(specification["proposed_oracle_command"])
        if specification["proposed_oracle_command_hash"] != expected_hash:
            raise GemOracleCoverageValidationError(
                "proposed_oracle_command_hash does not match proposed command"
            )
        _assert_no_forbidden_content(specification)


def validate_t3_oracle_coverage_package(
    coverage_records: list[dict[str, Any]],
    provenance_manifest: dict[str, Any],
    specification_manifest: dict[str, Any],
    public_problem_statements: dict[str, str],
) -> None:
    """Validate the full static T=3 package using only local public snapshots.

    ``public_problem_statements`` is an in-memory mapping from task id to the
    frozen local public problem statement. This function performs no network or
    command execution.
    """

    validate_t3_oracle_coverage_records(coverage_records)
    validate_gem_oracle_coverage_provenance(provenance_manifest)
    validate_public_oracle_specifications(specification_manifest)
    if not isinstance(public_problem_statements, dict):
        raise GemOracleCoverageValidationError(
            "public_problem_statements must be a task-to-text mapping"
        )
    if set(public_problem_statements) != set(T3_TASK_IDS):
        raise GemOracleCoverageValidationError(
            "public_problem_statements must contain exactly the fixed T=3 tasks"
        )

    coverage_by_id = _unique_records_by_id(coverage_records, "coverage_id", "coverage")
    provenance_by_id = _unique_records_by_id(
        provenance_manifest["records"],
        "coverage_id",
        "provenance",
    )
    specifications_by_id = _unique_records_by_id(
        specification_manifest["specifications"],
        "coverage_id",
        "specification",
    )
    if set(provenance_by_id) != set(coverage_by_id) or set(specifications_by_id) != set(
        coverage_by_id
    ):
        raise GemOracleCoverageValidationError(
            "coverage, provenance, and specification coverage ids must match one-to-one"
        )

    for coverage_id, coverage in coverage_by_id.items():
        provenance = provenance_by_id[coverage_id]
        specification = specifications_by_id[coverage_id]
        linkage = provenance["candidate_linkage"]
        task_id = coverage["task_id"]
        if linkage["task_id"] != task_id or specification["task_id"] != task_id:
            raise GemOracleCoverageValidationError("task ids do not match across package")
        if linkage["candidate_id"] != coverage["candidate_id"]:
            raise GemOracleCoverageValidationError(
                "candidate ids do not match coverage provenance"
            )
        expected_repository_ref = (
            f"https://github.com/{coverage['repo']}/tree/{coverage['base_commit']}"
        )
        if provenance["public_repository_ref"] != expected_repository_ref:
            raise GemOracleCoverageValidationError(
                "provenance repository/base commit does not match coverage"
            )
        if specification["maximum_claim"] != coverage["maximum_claim"]:
            raise GemOracleCoverageValidationError(
                "maximum_claim does not match coverage specification"
            )
        if (
            specification["maximum_retrieval_policy"]
            != coverage["maximum_retrieval_policy"]
        ):
            raise GemOracleCoverageValidationError(
                "maximum retrieval policy does not match coverage specification"
            )
        if (
            specification["proposed_oracle_command_hash"]
            != coverage["proposed_oracle_command_hash"]
        ):
            raise GemOracleCoverageValidationError(
                "proposed oracle command hash does not match coverage"
            )
        if f"--base-commit {coverage['base_commit']}" not in specification[
            "proposed_oracle_command"
        ]:
            raise GemOracleCoverageValidationError(
                "proposed oracle command does not target the registered base commit"
            )
        source_text = public_problem_statements[task_id]
        _require_non_empty_string(source_text, "public problem statement")
        if coverage["public_source_content_hash"] != public_source_content_hash(
            source_text
        ):
            raise GemOracleCoverageValidationError(
                "public source content hash does not match local snapshot"
            )
        _assert_no_forbidden_content({
            "coverage": coverage,
            "provenance": provenance,
            "specification": specification,
        })


def public_source_content_hash(problem_statement: str) -> str:
    """Hash a frozen local public problem statement; no network is involved."""

    return _sha256(_required_string(problem_statement, "public problem statement"))


def gem_oracle_coverage_id(record: dict[str, Any]) -> str:
    """Return a deterministic id for a complete static coverage registration."""

    payload = {
        key: record.get(key, "")
        for key in (
            "schema_version",
            "task_id",
            "candidate_id",
            "repo",
            "base_commit",
            "public_source_ref",
            "public_source_content_hash",
            "coverage_class",
            "maximum_claim",
            "maximum_retrieval_policy",
            "proposed_oracle_command_hash",
            "execution_state",
        )
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"gemcov_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}"


def _candidates_by_task(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    if not isinstance(candidates, (list, tuple)):
        raise GemOracleCoverageValidationError("candidates must be a list or tuple")
    candidates_by_task: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise GemOracleCoverageValidationError("candidate must be a mapping")
        task_id = str(candidate.get("task_id") or "")
        if task_id in candidates_by_task:
            raise GemOracleCoverageValidationError(f"duplicate candidate task: {task_id}")
        candidates_by_task[task_id] = candidate
    if set(candidates_by_task) != set(T3_TASK_IDS):
        raise GemOracleCoverageValidationError(
            "candidates must be exactly the fixed T=3 tasks"
        )
    return candidates_by_task


def _validate_candidate_link(
    candidate: dict[str, Any],
    task_id: str,
    contract: dict[str, str],
) -> None:
    if str(candidate.get("candidate_id") or "") != contract["candidate_id"]:
        raise GemOracleCoverageValidationError("task/candidate linkage does not match T=3")
    if str(candidate.get("repo") or "") != contract["repo"]:
        raise GemOracleCoverageValidationError("task/repo linkage does not match T=3")


def _assert_scope_boundary(record: dict[str, Any], contract: dict[str, str]) -> None:
    claim = record["maximum_claim"].lower()
    if contract["coverage_class"] == NARROW_PUBLIC_REPRODUCTION:
        if any(marker in claim for marker in ("broad", "global", "repo-wide", "all ")):
            raise GemOracleCoverageValidationError(
                "narrow coverage cannot declare a broad, repo, or global claim"
            )
        if record["maximum_retrieval_policy"] != NARROW_ONLY:
            raise GemOracleCoverageValidationError("narrow coverage must remain narrow_only")
    if contract["coverage_class"] == BROAD_ISSUE_REPRODUCTION:
        if record["maximum_retrieval_policy"] == NARROW_ONLY:
            raise GemOracleCoverageValidationError(
                "broad issue coverage is not directly retrievable"
            )


def _assert_no_forbidden_content(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            _assert_no_forbidden_text(str(key), key_path)
            _assert_no_forbidden_content(item, key_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_forbidden_content(item, f"{path}[{index}]")
    elif isinstance(value, str):
        _assert_no_forbidden_text(value, path or "payload")


def _assert_no_forbidden_text(value: str, path: str) -> None:
    lowered = value.lower().replace("-", "_")
    for marker in _FORBIDDEN_MARKERS:
        if marker in lowered or marker.replace("-", "_") in lowered:
            raise GemOracleCoverageValidationError(
                f"forbidden coverage content at {path}: {marker}"
            )


def _validate_closed_mapping(
    value: Any,
    required_fields: set[str],
    label: str,
) -> None:
    if not isinstance(value, dict):
        raise GemOracleCoverageValidationError(f"{label} must be a mapping")
    missing_fields = required_fields.difference(value)
    if missing_fields:
        raise GemOracleCoverageValidationError(
            f"{label} missing fields: {sorted(missing_fields)}"
        )
    unexpected_fields = set(value).difference(required_fields)
    if unexpected_fields:
        raise GemOracleCoverageValidationError(
            f"{label} unexpected fields: {sorted(unexpected_fields)}"
        )


def _unique_records_by_id(
    records: list[dict[str, Any]],
    id_field: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get(id_field) or "")
        if not record_id:
            raise GemOracleCoverageValidationError(f"{label} record missing {id_field}")
        if record_id in by_id:
            raise GemOracleCoverageValidationError(f"duplicate {label} {id_field}: {record_id}")
        by_id[record_id] = record
    return by_id


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise GemOracleCoverageValidationError(f"{field_name} must be a non-empty string")
    return value


def _required_string(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise GemOracleCoverageValidationError(f"{field_name} must be a non-empty string")
    return text


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
