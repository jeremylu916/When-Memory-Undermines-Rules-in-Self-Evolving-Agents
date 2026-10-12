"""Public-only oracle entrypoint support for the frozen GEM T=3 slice.

This module is intentionally Python 3.6 compatible because the frozen Django
image uses Python 3.6.  It loads only the frozen public coverage package and
never reads predictions, patches, hidden tests, official outputs, or historical
oracle results.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess


PREFLIGHT_SCHEMA_VERSION = "gem.public_oracle_preflight.v1"
OBSERVATION_SCHEMA_VERSION = "gem.public_oracle_observation.v1"

T3_TASK_IDS = (
    "astropy__astropy-13033",
    "django__django-10554",
    "matplotlib__matplotlib-14623",
)

_FORBIDDEN_MARKERS = (
    "hidden_test",
    "hidden test",
    "gold_patch",
    "gold patch",
    "reference_solution",
    "reference solution",
    "reference_patch",
    "reference patch",
    "official_output",
    "official output",
    "official_stdout",
    "official_stderr",
    "model_patch",
    "prediction_path",
    "patch-derived",
    "patch derived",
)

_PREFLIGHT_FIELDS = {
    "schema_version",
    "task_id",
    "coverage_id",
    "provenance_id",
    "oracle_specification_id",
    "repo",
    "expected_base_commit",
    "observed_base_commit",
    "public_source_content_hash",
    "oracle_command_hash",
    "oracle_source_hash",
    "infrastructure_available",
    "oracle_executed",
    "reproduction_observed",
    "candidate_passed",
    "task_solved",
    "backend_kind",
    "dependency_versions",
    "check_codes",
}

_OBSERVATION_FIELDS = {
    "schema_version",
    "task_id",
    "coverage_id",
    "provenance_id",
    "oracle_specification_id",
    "repo",
    "expected_base_commit",
    "observed_base_commit",
    "public_source_content_hash",
    "oracle_command_hash",
    "oracle_source_hash",
    "authorization_ref",
    "subject_role",
    "candidate_payload_hash",
    "candidate_patch_hash",
    "subject_worktree_hash",
    "observed_patch_hash",
    "infrastructure_available",
    "oracle_executed",
    "reproduction_observed",
    "public_observable_satisfied",
    "candidate_passed",
    "task_solved",
    "outcome",
    "detail_codes",
}

_EMPTY_WORKTREE_PAYLOAD = b"\0STATUS\0"
CLEAN_WORKTREE_HASH = "sha256:" + hashlib.sha256(_EMPTY_WORKTREE_PAYLOAD).hexdigest()
EMPTY_PATCH_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()


class PublicOracleContractError(ValueError):
    """Raised when a public-oracle entrypoint is outside the frozen contract."""


def load_public_oracle_contract(application_root, task_id, script_path):
    """Load one task contract from the immutable public package."""

    root = Path(application_root).resolve()
    if task_id not in T3_TASK_IDS:
        raise PublicOracleContractError("task is outside the frozen T=3 set")
    package = root / "runs" / "gem_t3_public_oracle_coverage_preparation"
    coverage_path = package / "coverage_records.json"
    provenance_path = package / "coverage_provenance.json"
    specification_path = package / "oracle_specifications.json"
    candidate_path = (
        root
        / "runs"
        / "gem_t3_nomem_trajectory_collection"
        / "offline_gem"
        / "memory_candidates.jsonl"
    )
    _validate_frozen_public_package_hashes(
        root, (coverage_path, provenance_path, specification_path, candidate_path)
    )
    coverage_records = _read_json(coverage_path)
    provenance_records = _read_json(provenance_path)["records"]
    specifications = _read_json(specification_path)["specifications"]
    _assert_public_only(coverage_records)
    _assert_public_only(provenance_records)
    _assert_public_only(specifications)
    coverage = _one(coverage_records, "task_id", task_id, "coverage")
    provenance = _one(
        provenance_records, "coverage_id", coverage["coverage_id"], "provenance"
    )
    specification = _one(
        specifications,
        "coverage_id",
        coverage["coverage_id"],
        "oracle specification",
    )
    candidate = _one(_read_jsonl(candidate_path), "task_id", task_id, "candidate")
    if (
        candidate.get("candidate_id") != coverage["candidate_id"]
        or candidate.get("repo") != coverage["repo"]
    ):
        raise PublicOracleContractError("candidate payload linkage mismatch")
    intervention = candidate.get("intervention")
    if not isinstance(intervention, dict) or not str(intervention.get("patch_hash") or "").startswith(
        "sha256:"
    ):
        raise PublicOracleContractError("candidate patch hash is missing")
    relative_script = Path(script_path).resolve().relative_to(root).as_posix()
    if specification["proposed_script"] != relative_script:
        raise PublicOracleContractError("entrypoint path does not match frozen specification")
    application_config = _read_json(
        root / "configs" / "gem_t3_execution_application.json"
    )
    supporting = {
        str(record.get("path")): str(record.get("sha256"))
        for record in application_config.get("supporting_sources", [])
    }
    if not supporting:
        raise PublicOracleContractError("public-oracle support source manifest is missing")
    for relative, expected_digest in supporting.items():
        support_path = (root / relative).resolve()
        if root not in support_path.parents or not support_path.is_file():
            raise PublicOracleContractError("public-oracle support source path is invalid")
        observed_digest = hashlib.sha256(support_path.read_bytes()).hexdigest()
        if expected_digest != observed_digest:
            raise PublicOracleContractError("public-oracle support source hash mismatch")
    implementation_records = [
        record
        for record in application_config.get("oracle_implementations", [])
        if record.get("task_id") == task_id
    ]
    if len(implementation_records) != 1:
        raise PublicOracleContractError("reviewed oracle implementation record is missing")
    implementation = implementation_records[0]
    if implementation.get("path") != relative_script or implementation.get(
        "sha256"
    ) != hashlib.sha256(Path(script_path).read_bytes()).hexdigest():
        raise PublicOracleContractError("reviewed oracle implementation hash mismatch")
    if _sha256_text(specification["proposed_oracle_command"]) != coverage[
        "proposed_oracle_command_hash"
    ]:
        raise PublicOracleContractError("oracle command hash mismatch")
    if specification["proposed_oracle_command_hash"] != coverage[
        "proposed_oracle_command_hash"
    ]:
        raise PublicOracleContractError("coverage/specification command hash mismatch")
    subset = _read_json(root / "configs" / "swe_verified_subset_40.json")
    public_tasks = [
        task
        for task in subset.get("tasks", [])
        if task.get("instance_id") == task_id
    ]
    if len(public_tasks) != 1:
        raise PublicOracleContractError("public problem statement is missing or duplicated")
    problem_statement = str(public_tasks[0].get("problem_statement") or "").strip()
    if _sha256_text(problem_statement) != coverage["public_source_content_hash"]:
        raise PublicOracleContractError("public problem-statement hash mismatch")
    contract = dict(coverage)
    contract.update(
        {
            "provenance_id": stable_record_id("gemprov", provenance),
            "oracle_specification_id": stable_record_id("gemoraspec", specification),
            "oracle_source_hash": _sha256_bytes(Path(script_path).read_bytes()),
            "proposed_script": relative_script,
            "candidate_payload_hash": _sha256_text(
                json.dumps(
                    candidate,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            ),
            "candidate_patch_hash": str(intervention["patch_hash"]),
        }
    )
    return contract


def observed_git_head(worktree="."):
    """Return HEAD without changing the target repository."""

    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PublicOracleContractError("target worktree has no readable git HEAD")
    return completed.stdout.strip()


def observed_worktree_hash(worktree="."):
    """Hash tracked diff plus untracked-path status without emitting patch text."""

    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if diff.returncode != 0 or status.returncode != 0:
        raise PublicOracleContractError("target worktree state is not hashable")
    return _sha256_bytes(diff.stdout + b"\0STATUS\0" + status.stdout)


def observed_patch_hash(worktree="."):
    """Hash the canonical tracked binary diff that will be executed."""

    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if diff.returncode != 0:
        raise PublicOracleContractError("target patch state is not hashable")
    return _sha256_bytes(diff.stdout)


def require_subject_provenance(contract, worktree="."):
    """Bind an authorized observation to base-clean or candidate-dirty state."""

    role = str(os.environ.get("GEM_T3_ORACLE_SUBJECT_ROLE", ""))
    if role not in ("frozen_base", "candidate"):
        raise PublicOracleContractError("oracle subject role is absent or invalid")
    worktree_hash = observed_worktree_hash(worktree)
    patch_hash = observed_patch_hash(worktree)
    if role == "frozen_base" and worktree_hash != CLEAN_WORKTREE_HASH:
        raise PublicOracleContractError("frozen-base oracle worktree is not clean")
    if role == "frozen_base" and patch_hash != EMPTY_PATCH_HASH:
        raise PublicOracleContractError("frozen-base oracle patch state is not empty")
    if role == "candidate" and worktree_hash == CLEAN_WORKTREE_HASH:
        raise PublicOracleContractError("candidate oracle worktree has no patch state")
    if role == "candidate" and patch_hash != contract["candidate_patch_hash"]:
        raise PublicOracleContractError(
            "candidate worktree diff does not match frozen candidate patch hash"
        )
    return {
        "subject_role": role,
        "candidate_payload_hash": contract["candidate_payload_hash"],
        "candidate_patch_hash": contract["candidate_patch_hash"],
        "subject_worktree_hash": worktree_hash,
        "observed_patch_hash": patch_hash,
    }


def require_exact_base(contract, requested_base_commit, observed_base_commit):
    """Fail closed unless command, package, and worktree all name the same base."""

    expected = contract["base_commit"]
    if requested_base_commit != expected:
        raise PublicOracleContractError("requested base commit does not match coverage")
    if observed_base_commit != expected:
        raise PublicOracleContractError("worktree HEAD is not the frozen base commit")


def require_real_authorization(application_root):
    """Require the disabled-by-default human gate without returning any secret."""

    if os.environ.get("GEM_T3_V2_APPROVAL_ID"):
        task_id = str(os.environ.get("GEM_T3_V2_TASK_ID", ""))
        return require_v2_process_authorization(
            application_root, task_id, "Exec"
        )

    config = _read_json(
        Path(application_root) / "configs" / "gem_t3_execution_application.json"
    )
    authorization = config.get("authorization", {})
    approval_id = str(os.environ.get("GEM_T3_EXECUTION_APPROVAL_ID", ""))
    token = str(os.environ.get("GEM_T3_EXECUTION_AUTHORIZATION_TOKEN", ""))
    if authorization.get("approved") is not True:
        raise PublicOracleContractError("real execution is disabled in application config")
    if not approval_id or approval_id != authorization.get("approval_id"):
        raise PublicOracleContractError("approval identifier is absent or mismatched")
    if not token:
        raise PublicOracleContractError("explicit authorization token is absent")
    return approval_id


def require_v2_process_authorization(application_root, task_id, condition):
    """Verify a launcher-derived, process-local v2 child authorization.

    This is Python 3.6 compatible because task images import it directly.  It
    independently re-hashes the checked-in manifest and binds the child token
    to approval, digest, nonce, condition, and task.
    """

    root = Path(application_root)
    manifest = _read_json(
        root / "configs" / "gem_t3_execution_application_v2.json"
    )
    manifest_digest = hashlib.sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    approval_id = str(os.environ.get("GEM_T3_V2_APPROVAL_ID", ""))
    observed_digest = str(os.environ.get("GEM_T3_V2_MANIFEST_DIGEST", ""))
    observed_condition = str(os.environ.get("GEM_T3_V2_CONDITION", ""))
    observed_task = str(os.environ.get("GEM_T3_V2_TASK_ID", ""))
    token = str(os.environ.get("GEM_T3_V2_CHILD_AUTHORIZATION_TOKEN", ""))
    envelope_path = _canonical_v2_envelope_path(root)
    if manifest.get("approval_id") != "gem-t3-sqlite-20260712":
        raise PublicOracleContractError("v2 application approval id drift")
    if approval_id != manifest.get("approval_id"):
        raise PublicOracleContractError("v2 process approval id mismatch")
    if observed_digest != manifest_digest:
        raise PublicOracleContractError("v2 process manifest digest mismatch")
    if observed_condition != condition or observed_task != task_id:
        raise PublicOracleContractError("v2 process condition/task mismatch")
    if not token:
        raise PublicOracleContractError("v2 process authorization is absent")
    envelope = _read_json(envelope_path)
    if (
        envelope.get("schema_version") != "gem.t3_authorization_envelope.v2.1"
        or envelope.get("state") != "verified_authorized"
        or envelope.get("approval_id") != approval_id
        or envelope.get("manifest_digest") != manifest_digest
        or envelope.get("independent_verification") != "GO"
        or envelope.get("execution_authorized") is not True
    ):
        raise PublicOracleContractError("v2 authorization envelope is not verified GO")
    token_hashes = envelope.get("child_token_hashes")
    key = "%s:%s" % (condition, task_id)
    if not isinstance(token_hashes, dict) or not hmac.compare_digest(
        str(token_hashes.get(key, "")),
        hashlib.sha256(token.encode("utf-8")).hexdigest(),
    ):
        raise PublicOracleContractError("v2 process authorization token mismatch")
    _validate_v2_verification_report(root, envelope)
    return approval_id


def _validate_v2_verification_report(root, envelope):
    report_path = Path(root) / str(envelope.get("verification_report", ""))
    if (
        not report_path.is_file()
        or hashlib.sha256(report_path.read_bytes()).hexdigest()
        != envelope.get("verification_report_sha256")
        or "Verdict: **GO**" not in report_path.read_text(encoding="utf-8")
    ):
        raise PublicOracleContractError("v2 verification report is not bound GO")


def _canonical_v2_envelope_path(root):
    return Path(root) / "configs" / "gem_t3_authorization_envelope_v2.json"


def build_preflight_record(
    contract,
    observed_base_commit,
    infrastructure_available,
    backend_kind,
    dependency_versions,
    check_codes,
):
    """Build a closed preflight record that cannot claim execution or success."""

    record = _linkage(contract)
    record.update(
        {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "observed_base_commit": str(observed_base_commit or ""),
            "infrastructure_available": bool(infrastructure_available),
            "oracle_executed": False,
            "reproduction_observed": None,
            "candidate_passed": None,
            "task_solved": False,
            "backend_kind": str(backend_kind or "none"),
            "dependency_versions": dict(dependency_versions or {}),
            "check_codes": list(check_codes or []),
        }
    )
    validate_preflight_record(record)
    return record


def build_observation_record(
    contract,
    observed_base_commit,
    authorization_ref,
    reproduction_observed,
    public_observable_satisfied,
    outcome,
    detail_codes,
    subject_role,
    candidate_payload_hash,
    candidate_patch_hash,
    subject_worktree_hash,
    observed_patch_hash,
):
    """Build one authorized single-worktree observation.

    Candidate pass is deliberately ``None`` here.  It can only be decided by a
    separate real runtime adapter after it validates both base and candidate
    observations against the complete frozen linkage.
    """

    record = _linkage(contract)
    record.update(
        {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "observed_base_commit": str(observed_base_commit or ""),
            "authorization_ref": str(authorization_ref or ""),
            "subject_role": str(subject_role or ""),
            "candidate_payload_hash": str(candidate_payload_hash or ""),
            "candidate_patch_hash": str(candidate_patch_hash or ""),
            "subject_worktree_hash": str(subject_worktree_hash or ""),
            "observed_patch_hash": str(observed_patch_hash or ""),
            "infrastructure_available": True,
            "oracle_executed": True,
            "reproduction_observed": bool(reproduction_observed),
            "public_observable_satisfied": bool(public_observable_satisfied),
            "candidate_passed": None,
            "task_solved": False,
            "outcome": str(outcome or ""),
            "detail_codes": list(detail_codes or []),
        }
    )
    validate_observation_record(record)
    return record


def validate_preflight_record(record):
    if not isinstance(record, dict) or set(record) != _PREFLIGHT_FIELDS:
        raise PublicOracleContractError("preflight schema fields are not closed")
    if record["schema_version"] != PREFLIGHT_SCHEMA_VERSION:
        raise PublicOracleContractError("unsupported preflight schema")
    _validate_linkage_fields(record)
    if record["oracle_executed"] is not False:
        raise PublicOracleContractError("preflight cannot claim oracle execution")
    if record["reproduction_observed"] is not None:
        raise PublicOracleContractError("preflight cannot claim reproduction")
    if record["candidate_passed"] is not None or record["task_solved"] is not False:
        raise PublicOracleContractError("preflight cannot claim candidate/task success")
    _validate_string_mapping(record["dependency_versions"], "dependency_versions")
    _validate_string_list(record["check_codes"], "check_codes")


def validate_observation_record(record):
    if not isinstance(record, dict) or set(record) != _OBSERVATION_FIELDS:
        raise PublicOracleContractError("observation schema fields are not closed")
    if record["schema_version"] != OBSERVATION_SCHEMA_VERSION:
        raise PublicOracleContractError("unsupported observation schema")
    _validate_linkage_fields(record)
    if not record["authorization_ref"]:
        raise PublicOracleContractError("executed observation requires authorization_ref")
    if record["subject_role"] not in ("frozen_base", "candidate"):
        raise PublicOracleContractError("executed observation requires subject role")
    for key in (
        "candidate_payload_hash",
        "candidate_patch_hash",
        "subject_worktree_hash",
        "observed_patch_hash",
    ):
        if not isinstance(record[key], str) or not record[key].startswith("sha256:"):
            raise PublicOracleContractError("%s must be a SHA-256 digest" % key)
    if record["subject_role"] == "frozen_base" and record["subject_worktree_hash"] != CLEAN_WORKTREE_HASH:
        raise PublicOracleContractError("base observation worktree is not clean")
    if record["subject_role"] == "frozen_base" and record["observed_patch_hash"] != EMPTY_PATCH_HASH:
        raise PublicOracleContractError("base observation patch is not empty")
    if record["subject_role"] == "candidate" and record["subject_worktree_hash"] == CLEAN_WORKTREE_HASH:
        raise PublicOracleContractError("candidate observation lacks patch state")
    if record["subject_role"] == "candidate" and record["observed_patch_hash"] != record[
        "candidate_patch_hash"
    ]:
        raise PublicOracleContractError("candidate observation patch hash mismatch")
    if record["infrastructure_available"] is not True or record["oracle_executed"] is not True:
        raise PublicOracleContractError("observation must come from available executed oracle")
    if not isinstance(record["reproduction_observed"], bool):
        raise PublicOracleContractError("reproduction_observed must be boolean")
    if not isinstance(record["public_observable_satisfied"], bool):
        raise PublicOracleContractError("public_observable_satisfied must be boolean")
    if record["candidate_passed"] is not None:
        raise PublicOracleContractError("single observation cannot claim candidate_passed")
    if record["task_solved"] is not False:
        raise PublicOracleContractError("public observation cannot claim task_solved")
    if record["outcome"] not in ("pass", "fail"):
        raise PublicOracleContractError("observation outcome must be pass or fail")
    _validate_string_list(record["detail_codes"], "detail_codes")


def stable_record_id(prefix, record):
    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "%s_%s" % (prefix, hashlib.sha256(encoded).hexdigest()[:20])


def emit_json(record):
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))


def _linkage(contract):
    return {
        "task_id": contract["task_id"],
        "coverage_id": contract["coverage_id"],
        "provenance_id": contract["provenance_id"],
        "oracle_specification_id": contract["oracle_specification_id"],
        "repo": contract["repo"],
        "expected_base_commit": contract["base_commit"],
        "public_source_content_hash": contract["public_source_content_hash"],
        "oracle_command_hash": contract["proposed_oracle_command_hash"],
        "oracle_source_hash": contract["oracle_source_hash"],
    }


def _validate_linkage_fields(record):
    for key in (
        "task_id",
        "coverage_id",
        "provenance_id",
        "oracle_specification_id",
        "repo",
        "expected_base_commit",
        "observed_base_commit",
        "public_source_content_hash",
        "oracle_command_hash",
        "oracle_source_hash",
    ):
        if not isinstance(record.get(key), str) or not record[key]:
            raise PublicOracleContractError("%s must be a non-empty string" % key)
    if record["task_id"] not in T3_TASK_IDS:
        raise PublicOracleContractError("record task is outside frozen T=3")
    for key in (
        "public_source_content_hash",
        "oracle_command_hash",
        "oracle_source_hash",
    ):
        value = record[key]
        if not value.startswith("sha256:") or len(value) != 71:
            raise PublicOracleContractError("%s must be a SHA-256 digest" % key)
    if not isinstance(record.get("infrastructure_available"), bool):
        raise PublicOracleContractError("infrastructure_available must be boolean")


def _validate_string_mapping(value, label):
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise PublicOracleContractError("%s must be a string mapping" % label)


def _validate_string_list(value, label):
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise PublicOracleContractError("%s must be a non-empty string list" % label)


def _read_json(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, (dict, list)):
        raise PublicOracleContractError("invalid JSON payload: %s" % path)
    return payload


def _read_jsonl(path):
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise PublicOracleContractError("invalid JSONL payload: %s" % path)
            records.append(value)
    return records


def _validate_frozen_public_package_hashes(root, paths):
    config = _read_json(root / "configs" / "gem_t3_execution_application.json")
    expected = {
        str(record.get("path")): str(record.get("sha256"))
        for record in config.get("frozen_artifacts", [])
    }
    for path in paths:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected.get(relative) != digest:
            raise PublicOracleContractError("frozen public package hash mismatch")


def _assert_public_only(value):
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_public_only(str(key))
            _assert_public_only(item)
        return
    if isinstance(value, list):
        for item in value:
            _assert_public_only(item)
        return
    if isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
            raise PublicOracleContractError("forbidden non-public source marker")


def _one(records, key, value, label):
    matches = [record for record in records if record.get(key) == value]
    if len(matches) != 1:
        raise PublicOracleContractError("expected one %s record" % label)
    return matches[0]


def _sha256_text(value):
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_bytes(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()
