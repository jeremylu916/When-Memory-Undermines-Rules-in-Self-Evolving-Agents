"""Fail-closed guards for a future frozen T=3 execution application.

Nothing in this module launches an oracle, Judge, Exec, LLM, harness, network
request, or database.  The default checked-in configuration is intentionally
unauthorized.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from self_evolving_agent.gem_oracle_coverage import T3_TASK_IDS


APPLICATION_SCHEMA_VERSION = "gem.t3_execution_application.v1"
PREFLIGHT_SCHEMA_VERSION = "gem.t3_execution_preflight.v1"

REAL_ARTIFACT_NAMES = (
    "oracle_runs.jsonl",
    "admission_events.jsonl",
    "verified_memory_items.jsonl",
    "admitted_candidates.jsonl",
)

_APPLICATION_FIELDS = {
    "schema_version",
    "task_ids",
    "conditions",
    "t10_enabled",
    "authorization",
    "approved_execution_commands",
    "credential_gate",
    "budgets",
    "timeouts_seconds",
    "max_retries",
    "output_directories",
    "network_policy",
    "failure_policy",
    "oracle_implementations",
    "supporting_sources",
    "frozen_artifacts",
}
_AUTHORIZATION_FIELDS = {
    "approved",
    "approval_id",
    "approval_id_env",
    "authorization_token_env",
    "requires_exact_command_approval",
}
_CREDENTIAL_FIELDS = {
    "acceptable_env_names",
    "minimum_present",
    "persist_values",
}
_BUDGET_FIELDS = {"currency", "per_task_per_condition", "total", "workers"}
_TIMEOUT_FIELDS = {
    "oracle",
    "judge",
    "exec",
    "llm",
    "database_preflight",
    "total",
}
_OUTPUT_FIELDS = {"Judge", "Exec", "must_be_absent_or_empty"}
_NETWORK_FIELDS = {
    "api_calls_allowed",
    "paid_calls_allowed",
    "unapproved_network_allowed",
}
_FAILURE_FIELDS = {
    "fail_closed",
    "fail_fast",
    "partial_results_admissible",
    "interrupted_results_admissible",
    "automatic_retry",
}
_FROZEN_FIELDS = {"path", "sha256"}
_IMPLEMENTATION_FIELDS = {"task_id", "path", "sha256"}
_SUPPORTING_SOURCE_FIELDS = {"path", "sha256"}
_COMMAND_FIELDS = {"condition", "argv", "sha256"}


class T3ExecutionApplicationError(ValueError):
    """Raised when a real-run prerequisite is absent or unsafe."""


def load_execution_application(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_execution_application(payload)
    return payload


def validate_execution_application(config: dict[str, Any]) -> None:
    """Validate the closed safety contract without treating it as authorization."""

    _closed(config, _APPLICATION_FIELDS, "application")
    if config["schema_version"] != APPLICATION_SCHEMA_VERSION:
        raise T3ExecutionApplicationError("unsupported application schema")
    if config["task_ids"] != list(T3_TASK_IDS):
        raise T3ExecutionApplicationError("task set/order must remain exact frozen T=3")
    if config["conditions"] != ["Judge", "Exec"]:
        raise T3ExecutionApplicationError("conditions must be exactly Judge and Exec")
    if config["t10_enabled"] is not False:
        raise T3ExecutionApplicationError("T=10 is forbidden")

    authorization = config["authorization"]
    _closed(authorization, _AUTHORIZATION_FIELDS, "authorization")
    if not all(
        isinstance(authorization[key], str) and authorization[key]
        for key in ("approval_id_env", "authorization_token_env")
    ):
        raise T3ExecutionApplicationError("authorization env names must be explicit")
    if not isinstance(authorization["approval_id"], str):
        raise T3ExecutionApplicationError("approval_id must be a string")
    if authorization["requires_exact_command_approval"] is not True:
        raise T3ExecutionApplicationError("exact command approval is mandatory")
    if authorization["approved"] is True and not authorization["approval_id"]:
        raise T3ExecutionApplicationError("approved application requires approval_id")

    commands = config["approved_execution_commands"]
    if not isinstance(commands, list):
        raise T3ExecutionApplicationError("approved execution commands must be a list")
    if authorization["approved"] is False and commands:
        raise T3ExecutionApplicationError("unapproved application cannot carry commands")
    command_conditions: list[str] = []
    for record in commands:
        _closed(record, _COMMAND_FIELDS, "approved execution command")
        command_conditions.append(record["condition"])
        argv = record["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
            or record["sha256"] != execution_command_hash(argv)
        ):
            raise T3ExecutionApplicationError("approved execution command hash mismatch")
    if authorization["approved"] is True and command_conditions != config["conditions"]:
        raise T3ExecutionApplicationError(
            "approved application requires exact Judge and Exec commands"
        )

    credentials = config["credential_gate"]
    _closed(credentials, _CREDENTIAL_FIELDS, "credential_gate")
    names = credentials["acceptable_env_names"]
    if not isinstance(names, list) or not names or not all(
        isinstance(name, str) and name for name in names
    ):
        raise T3ExecutionApplicationError("credential env alternatives are invalid")
    if credentials["minimum_present"] < 1 or credentials["minimum_present"] > len(names):
        raise T3ExecutionApplicationError("credential minimum_present is invalid")
    if credentials["persist_values"] is not False:
        raise T3ExecutionApplicationError("credential values must never be persisted")

    budgets = config["budgets"]
    _closed(budgets, _BUDGET_FIELDS, "budgets")
    if budgets["currency"] != "USD" or budgets["workers"] != 1:
        raise T3ExecutionApplicationError("budget currency/workers are not fixed")
    if float(budgets["per_task_per_condition"]) <= 0 or float(budgets["total"]) <= 0:
        raise T3ExecutionApplicationError("budgets must be positive")
    calculated_cap = (
        float(budgets["per_task_per_condition"])
        * len(T3_TASK_IDS)
        * len(config["conditions"])
    )
    if float(budgets["total"]) != calculated_cap:
        raise T3ExecutionApplicationError("total budget must equal the hard per-task cap")

    timeouts = config["timeouts_seconds"]
    _closed(timeouts, _TIMEOUT_FIELDS, "timeouts")
    if not all(isinstance(value, int) and value > 0 for value in timeouts.values()):
        raise T3ExecutionApplicationError("every runtime class requires a positive timeout")
    if not isinstance(config["max_retries"], int) or config["max_retries"] != 0:
        raise T3ExecutionApplicationError("automatic retries must remain zero")

    outputs = config["output_directories"]
    _closed(outputs, _OUTPUT_FIELDS, "output_directories")
    if outputs["must_be_absent_or_empty"] is not True:
        raise T3ExecutionApplicationError("output collision guard must be enabled")
    all_output_paths: list[str] = []
    for condition in ("Judge", "Exec"):
        per_task = outputs[condition]
        if not isinstance(per_task, dict) or list(per_task) != list(T3_TASK_IDS):
            raise T3ExecutionApplicationError(
                "each condition requires exact ordered per-task output roots"
            )
        if not all(isinstance(path, str) and path for path in per_task.values()):
            raise T3ExecutionApplicationError("per-task output roots must be non-empty")
        all_output_paths.extend(per_task.values())
    if len(set(all_output_paths)) != 6:
        raise T3ExecutionApplicationError("all six task/condition outputs must be independent")

    network = config["network_policy"]
    _closed(network, _NETWORK_FIELDS, "network_policy")
    if any(network.values()):
        raise T3ExecutionApplicationError("unapproved network/API/paid calls must be disabled")

    failure = config["failure_policy"]
    _closed(failure, _FAILURE_FIELDS, "failure_policy")
    if failure != {
        "fail_closed": True,
        "fail_fast": True,
        "partial_results_admissible": False,
        "interrupted_results_admissible": False,
        "automatic_retry": False,
    }:
        raise T3ExecutionApplicationError("failure policy must be fail-fast/fail-closed")

    implementations = config["oracle_implementations"]
    if not isinstance(implementations, list) or len(implementations) != 3:
        raise T3ExecutionApplicationError("exactly three oracle implementations are required")
    implementation_tasks: list[str] = []
    for record in implementations:
        _closed(record, _IMPLEMENTATION_FIELDS, "oracle implementation")
        implementation_tasks.append(record["task_id"])
        if not record["path"] or not _is_hex_sha256(record["sha256"]):
            raise T3ExecutionApplicationError("invalid oracle implementation manifest")
    if implementation_tasks != list(T3_TASK_IDS):
        raise T3ExecutionApplicationError("oracle implementation task order drift")

    supporting_sources = config["supporting_sources"]
    if not isinstance(supporting_sources, list) or not supporting_sources:
        raise T3ExecutionApplicationError("supporting source manifest is missing")
    supporting_paths: set[str] = set()
    for record in supporting_sources:
        _closed(record, _SUPPORTING_SOURCE_FIELDS, "supporting source")
        if (
            not record["path"]
            or record["path"] in supporting_paths
            or not _is_hex_sha256(record["sha256"])
        ):
            raise T3ExecutionApplicationError("invalid supporting source manifest")
        supporting_paths.add(record["path"])
    required_support = {
        "self_evolving_agent/gem_public_oracle.py",
        "self_evolving_agent/gem_runtime_integration.py",
        "self_evolving_agent/gem_t3_execution_application.py",
        "self_evolving_agent/gem_t3_runtime_evidence.py",
        "scripts/preflight_gem_t3_execution_application.py",
        "scripts/run_gem_stream.py",
        "scripts/analyze_gem_stream_smoke.py",
    }
    if supporting_paths != required_support:
        raise T3ExecutionApplicationError("supporting source set is incomplete or expanded")

    frozen = config["frozen_artifacts"]
    if not isinstance(frozen, list) or len(frozen) < 5:
        raise T3ExecutionApplicationError("frozen package/baseline manifest is incomplete")
    seen: set[str] = set()
    for record in frozen:
        _closed(record, _FROZEN_FIELDS, "frozen artifact")
        if record["path"] in seen:
            raise T3ExecutionApplicationError("duplicate frozen artifact path")
        seen.add(record["path"])
        if not _is_hex_sha256(record["sha256"]):
            raise T3ExecutionApplicationError("invalid frozen artifact hash")


def credential_presence(config: dict[str, Any], env: Mapping[str, str] | None = None) -> dict[str, bool]:
    """Return booleans only; secret values never leave the environment."""

    validate_execution_application(config)
    source = os.environ if env is None else env
    return {
        name: bool(source.get(name))
        for name in config["credential_gate"]["acceptable_env_names"]
    }


def check_frozen_artifacts(config: dict[str, Any], project_root: str | Path) -> dict[str, str]:
    """Fail on the first frozen-package or NoMem-baseline hash drift."""

    validate_execution_application(config)
    root = Path(project_root).resolve()
    observed: dict[str, str] = {}
    for record in config["frozen_artifacts"]:
        path = (root / record["path"]).resolve()
        if root not in path.parents:
            raise T3ExecutionApplicationError("frozen artifact escapes project root")
        if not path.is_file():
            raise T3ExecutionApplicationError(f"missing frozen artifact: {record['path']}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record["sha256"]:
            raise T3ExecutionApplicationError(f"frozen artifact hash mismatch: {record['path']}")
        observed[record["path"]] = digest
    return observed


def check_oracle_implementations(
    config: dict[str, Any], project_root: str | Path
) -> dict[str, str]:
    """Pin each reviewed public-oracle source file to its application hash."""

    validate_execution_application(config)
    root = Path(project_root).resolve()
    observed: dict[str, str] = {}
    for record in config["oracle_implementations"]:
        path = (root / record["path"]).resolve()
        if root not in path.parents or not path.is_file():
            raise T3ExecutionApplicationError("oracle implementation path is invalid")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record["sha256"]:
            raise T3ExecutionApplicationError(
                f"oracle implementation hash mismatch: {record['task_id']}"
            )
        observed[record["task_id"]] = digest
    return observed


def check_supporting_sources(
    config: dict[str, Any], project_root: str | Path
) -> dict[str, str]:
    """Pin every reviewed module used by preview, preflight, and real guards."""

    validate_execution_application(config)
    root = Path(project_root).resolve()
    observed: dict[str, str] = {}
    for record in config["supporting_sources"]:
        path = (root / record["path"]).resolve()
        if root not in path.parents or not path.is_file():
            raise T3ExecutionApplicationError("supporting source path is invalid")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record["sha256"]:
            raise T3ExecutionApplicationError(
                f"supporting source hash mismatch: {record['path']}"
            )
        observed[record["path"]] = digest
    return observed


def check_output_directories(config: dict[str, Any], project_root: str | Path) -> dict[str, str]:
    """Require independent, absent-or-empty roots outside all frozen artifacts."""

    validate_execution_application(config)
    root = Path(project_root).resolve()
    frozen_paths = [(root / item["path"]).resolve() for item in config["frozen_artifacts"]]
    statuses: dict[str, str] = {}
    for condition in ("Judge", "Exec"):
        for task_id in T3_TASK_IDS:
            relative = Path(config["output_directories"][condition][task_id])
            path = (root / relative).resolve()
            if root not in path.parents:
                raise T3ExecutionApplicationError("output directory escapes project root")
            _assert_no_frozen_overlap(path, frozen_paths)
            key = f"{condition}:{task_id}"
            statuses[key] = _absent_or_empty_status(path, relative)
    return statuses


def check_preview_output_directory(
    config: dict[str, Any], project_root: str | Path, output_dir: str | Path
) -> str:
    """Reject occupied preview roots and any frozen input overlap before writes."""

    validate_execution_application(config)
    root = Path(project_root).resolve()
    path = Path(output_dir).resolve()
    frozen_paths = [(root / item["path"]).resolve() for item in config["frozen_artifacts"]]
    _assert_no_frozen_overlap(path, frozen_paths)
    return _absent_or_empty_status(path, Path(output_dir))


def assert_real_execution_allowed(
    config: dict[str, Any],
    project_root: str | Path,
    *,
    task_ids: list[str],
    condition: str,
    command: list[str],
    env: Mapping[str, str] | None = None,
) -> None:
    """The only real-run gate; checked-in state always rejects execution."""

    validate_execution_application(config)
    if task_ids != list(T3_TASK_IDS):
        raise T3ExecutionApplicationError("real task set/order drift")
    if condition not in config["conditions"]:
        raise T3ExecutionApplicationError("real condition is not approved")
    source = os.environ if env is None else env
    authorization = config["authorization"]
    if authorization["approved"] is not True:
        raise T3ExecutionApplicationError("real execution is disabled by default")
    approved_commands = {
        record["condition"]: record for record in config["approved_execution_commands"]
    }
    approved_command = approved_commands.get(condition)
    if (
        approved_command is None
        or command != approved_command["argv"]
        or execution_command_hash(command) != approved_command["sha256"]
    ):
        raise T3ExecutionApplicationError("exact execution command is not approved")
    approval_id = source.get(authorization["approval_id_env"], "")
    token = source.get(authorization["authorization_token_env"], "")
    if not approval_id or approval_id != authorization["approval_id"] or not token:
        raise T3ExecutionApplicationError("explicit human authorization gate failed")
    presence = credential_presence(config, source)
    if sum(presence.values()) < config["credential_gate"]["minimum_present"]:
        raise T3ExecutionApplicationError("required credential is absent")
    check_frozen_artifacts(config, project_root)
    check_oracle_implementations(config, project_root)
    check_supporting_sources(config, project_root)
    check_output_directories(config, project_root)


def execution_command_hash(command: list[str]) -> str:
    """Hash an argv vector without shell normalization ambiguity."""

    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise T3ExecutionApplicationError("execution command must be non-empty argv")
    encoded = json.dumps(
        command, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_no_frozen_overlap(path: Path, frozen_paths: list[Path]) -> None:
    if any(
        path == frozen or path in frozen.parents or frozen in path.parents
        for frozen in frozen_paths
    ):
        raise T3ExecutionApplicationError("output directory overlaps frozen artifacts")


def _absent_or_empty_status(path: Path, label: Path) -> str:
    if not path.exists():
        return "absent"
    if path.is_dir() and not any(path.iterdir()):
        return "empty"
    raise T3ExecutionApplicationError(f"output directory is occupied: {label}")


def assert_no_real_artifacts(path: str | Path) -> None:
    """Reject preview/application roots containing non-empty real evidence files."""

    root = Path(path)
    for name in REAL_ARTIFACT_NAMES:
        artifact = root / name
        if artifact.exists() and artifact.read_text(encoding="utf-8").strip():
            raise T3ExecutionApplicationError(f"unexpected real artifact: {name}")


def validate_environment_evidence(evidence: dict[str, Any]) -> None:
    """Prevent SQLite/mock/static configuration from satisfying Django proof."""

    required_tasks = set(T3_TASK_IDS)
    if not isinstance(evidence, dict) or set(evidence) != required_tasks:
        raise T3ExecutionApplicationError("environment evidence must cover exact T=3")
    for task_id, record in evidence.items():
        if set(record) != {
            "status",
            "exact_base_commit",
            "dependencies_available",
            "backend_kind",
            "connection_verified",
            "connection_transport",
            "external_network_used",
            "authorized_local_network_used",
            "oracle_executed",
            "evidence_codes",
        }:
            raise T3ExecutionApplicationError("environment evidence fields are not closed")
        if record["external_network_used"] is not False or record["oracle_executed"] is not False:
            raise T3ExecutionApplicationError(
                "preflight cannot use external network or execute oracle"
            )
        if record["authorized_local_network_used"] is True:
            if task_id != "django__django-10554" or record["connection_transport"] != "docker_host_network":
                raise T3ExecutionApplicationError(
                    "authorized local network is limited to Django PostgreSQL preflight"
                )
        if task_id == "django__django-10554" and record["status"] == "pass":
            if (
                record["backend_kind"] != "postgresql"
                or record["connection_verified"] is not True
                or record["connection_transport"] not in {
                    "docker_host_network",
                    "local_unix_socket",
                }
            ):
                raise T3ExecutionApplicationError("Django PASS requires real PostgreSQL connection")
        if task_id != "django__django-10554" and record["status"] == "pass":
            if record["exact_base_commit"] is not True or record["dependencies_available"] is not True:
                raise T3ExecutionApplicationError("local dependency PASS lacks exact-base evidence")


def _closed(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise T3ExecutionApplicationError(f"{label} fields are not closed")


def _is_hex_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)
