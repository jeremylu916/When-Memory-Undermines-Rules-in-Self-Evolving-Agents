"""Versioned, fail-closed contract for the one real GEM T=3 launcher.

Validation is deliberately side-effect free.  The authorization envelope and
process-local token are checked before a credential source is inspected, an
output directory is created, or a subprocess is started.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


TASK_IDS = (
    "astropy__astropy-13033",
    "django__django-10554",
    "matplotlib__matplotlib-14623",
)
CONDITIONS = ("Judge", "Exec")
APPROVAL_ID = "gem-t3-sqlite-20260712"
MANIFEST_SCHEMA = "gem.t3_execution_application.v2.1"
ENVELOPE_SCHEMA = "gem.t3_authorization_envelope.v2.1"
VERIFICATION_REPORT = "reports/gem_t3_sqlite_v2_independent_verification_20260712.md"
RECOVERY_PRIOR_MANIFEST_DIGEST = "0d00c99a541f59104324cb3f2f4734e9e23756565e9c1ba6bf3a785f36d9e10b"


class LauncherContractError(ValueError):
    """A production-launch prerequisite drifted or is not authorized."""


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LauncherContractError("contract document must be a JSON object")
    return value


def validate_manifest(manifest: dict[str, Any], project_root: Path) -> str:
    expected = {
        "schema_version", "state", "approval_id", "task_ids", "conditions",
        "t10_enabled", "execution_phase", "provider", "model", "runtime", "commands",
        "harness_command", "harness_output", "recovery", "frozen_inputs", "cost_policy", "budget",
        "timeouts_seconds", "max_retries", "workers", "outputs", "network",
        "credential_source", "source_hashes", "forbidden_output_roots",
    }
    _closed(manifest, expected, "manifest")
    if manifest["schema_version"] != MANIFEST_SCHEMA or manifest["state"] != "prepared_not_authorized":
        raise LauncherContractError("checked-in manifest must remain prepared_not_authorized")
    if manifest["approval_id"] != APPROVAL_ID:
        raise LauncherContractError("approval id drift")
    if manifest["task_ids"] != list(TASK_IDS) or manifest["conditions"] != list(CONDITIONS):
        raise LauncherContractError("scope/order must be exact T=3 Judge then Exec")
    if manifest["t10_enabled"] is not False:
        raise LauncherContractError("T=10 is forbidden")
    if manifest["execution_phase"] != "fixed_candidate_judge_exec_oracle_harness":
        raise LauncherContractError("launcher phase drift")
    if manifest["provider"] != "openai-compatible-azure" or manifest["model"] != "openai/gpt-5.5":
        raise LauncherContractError("provider/model drift")
    if manifest["runtime"] != {
        "python": "third_party/mini-swe-agent/.venv/bin/python",
        "configs": [
            {
                "argv_value": "swebench.yaml",
                "source_path": "third_party/mini-swe-agent/src/minisweagent/config/benchmarks/swebench.yaml",
                "sha256": file_sha256(project_root / "third_party/mini-swe-agent/src/minisweagent/config/benchmarks/swebench.yaml"),
            },
            {
                "argv_value": "configs/miniswe_gpt55_medium.yaml",
                "source_path": "configs/miniswe_gpt55_medium.yaml",
                "sha256": file_sha256(project_root / "configs/miniswe_gpt55_medium.yaml"),
            },
        ],
    }:
        raise LauncherContractError("runtime/config drift")
    if manifest["cost_policy"] != {
        "billing_model": "azure/gpt-5.5",
        "input_cost_per_token": 0.000005,
        "output_cost_per_token": 0.00003,
        "evidence_kind": "response_tokens_with_frozen_rates",
        "provider_invoice": False,
    }:
        raise LauncherContractError("cost policy drift")
    budget = manifest["budget"]
    if budget != {
        "currency": "USD",
        "per_task_per_condition": 3.0,
        "prior_unknown_cost_reserve_usd": 3.0,
        "total": 18.0,
    }:
        raise LauncherContractError("budget drift")
    if manifest["timeouts_seconds"] != {"task": 7200, "llm": 7200, "oracle": 600, "harness": 7200, "total": 43200}:
        raise LauncherContractError("timeout drift")
    if manifest["max_retries"] != 0 or manifest["workers"] != 1:
        raise LauncherContractError("retry/worker drift")
    if manifest["credential_source"] != "/root/.secrets/openai.env":
        raise LauncherContractError("credential source drift")
    if manifest["network"] != {
        "api_calls_allowed_after_authorization": True,
        "paid_calls_allowed_after_authorization": True,
        "allowed_api_base": "https://yuexing-4772-resource.services.ai.azure.com/openai/v1",
        "unapproved_network_allowed": False,
    }:
        raise LauncherContractError("network policy drift")

    outputs = manifest["outputs"]
    if not isinstance(outputs, dict) or list(outputs) != list(CONDITIONS):
        raise LauncherContractError("output condition order drift")
    expected_records = []
    seen_outputs: set[str] = set()
    for condition in CONDITIONS:
        if not isinstance(outputs[condition], dict) or list(outputs[condition]) != list(TASK_IDS):
            raise LauncherContractError("output task order drift")
        for task_id in TASK_IDS:
            output = outputs[condition][task_id]
            if not isinstance(output, str) or not output or output in seen_outputs:
                raise LauncherContractError("six independent outputs are required")
            seen_outputs.add(output)
            expected_records.append((condition, task_id, output))
    forbidden = tuple(manifest["forbidden_output_roots"])
    for output in seen_outputs:
        if any(_paths_overlap(Path(output), Path(item)) for item in forbidden):
            raise LauncherContractError("output overlaps frozen/offline/dry-run/history")
    harness_output = manifest["harness_output"]
    if not isinstance(harness_output, str) or not harness_output:
        raise LauncherContractError("harness output is missing")
    if harness_output in seen_outputs or any(
        _paths_overlap(Path(harness_output), Path(item)) for item in forbidden
    ):
        raise LauncherContractError("harness output overlaps protected output")

    commands = manifest["commands"]
    if not isinstance(commands, list) or len(commands) != 6:
        raise LauncherContractError("exactly six commands are required")
    for record, (condition, task_id, output) in zip(commands, expected_records):
        _closed(
            record,
            {"condition", "task_id", "argv", "argv_sha256", "launched_argv", "launched_argv_sha256"},
            "command",
        )
        expected_argv = _solver_argv(manifest, task_id, output)
        launched_argv = _launched_argv(manifest, condition, expected_argv)
        if record != {
            "condition": condition,
            "task_id": task_id,
            "argv": expected_argv,
            "argv_sha256": canonical_digest(expected_argv),
            "launched_argv": launched_argv,
            "launched_argv_sha256": canonical_digest(launched_argv),
        }:
            raise LauncherContractError("command argv/provider/config/output drift")

    expected_harness = _harness_argv(manifest)
    harness = manifest["harness_command"]
    _closed(harness, {"argv", "argv_sha256"}, "harness command")
    if harness != {
        "argv": expected_harness,
        "argv_sha256": canonical_digest(expected_harness),
    }:
        raise LauncherContractError("official harness command drift")

    recovery = manifest["recovery"]
    _closed(
        recovery,
        {"mode", "prior_manifest_digest", "completed_judge_results"},
        "recovery",
    )
    if (
        recovery["mode"] != "resume_after_exec_infrastructure_failure"
        or recovery["prior_manifest_digest"] != RECOVERY_PRIOR_MANIFEST_DIGEST
        or list(recovery["completed_judge_results"]) != list(TASK_IDS)
    ):
        raise LauncherContractError("recovery identity/scope drift")
    for task_id, record in recovery["completed_judge_results"].items():
        _closed(record, {"path", "sha256"}, "recovered Judge result")
        expected_path = str(Path(outputs["Judge"][task_id]) / "judge_result.json")
        if record["path"] != expected_path or not _is_sha256(record["sha256"]):
            raise LauncherContractError("recovered Judge path/hash drift")
        path = project_root / record["path"]
        if not path.is_file() or file_sha256(path) != record["sha256"]:
            raise LauncherContractError("recovered Judge result hash mismatch")
        _validate_recovered_judge(load_json(path), manifest, task_id)

    frozen_inputs = manifest["frozen_inputs"]
    required_inputs = {
        "configs/swe_verified_subset_40.json",
        "runs/gem_t3_nomem_trajectory_collection/offline_gem/memory_candidates.jsonl",
        "runs/gem_t3_nomem_trajectory_collection/preds_t3_nomem.json",
        "runs/gem_t3_nomem_trajectory_collection/offline_gem/summary.json",
        "artifacts/official_results/openai__gpt-5.5.gem_t3_nomem.json",
        "runs/gem_t3_public_oracle_coverage_preparation/coverage_records.json",
        "runs/gem_t3_public_oracle_coverage_preparation/coverage_provenance.json",
        "runs/gem_t3_public_oracle_coverage_preparation/oracle_specifications.json",
        "runs/gem_t3_public_oracle_coverage_sqlite_v2/package_manifest.json",
        "runs/gem_t3_public_oracle_coverage_sqlite_v2/coverage_records.json",
        "runs/gem_t3_public_oracle_coverage_sqlite_v2/coverage_provenance.json",
        "runs/gem_t3_public_oracle_coverage_sqlite_v2/oracle_specifications.json",
    }
    if not isinstance(frozen_inputs, dict) or set(frozen_inputs) != required_inputs:
        raise LauncherContractError("frozen input manifest drift")
    for relative, expected_hash in frozen_inputs.items():
        if file_sha256(project_root / relative) != expected_hash:
            raise LauncherContractError("frozen input hash drift")
    from self_evolving_agent.gem_sqlite_v2_oracle import load_sqlite_v2_contract

    load_sqlite_v2_contract(
        project_root,
        project_root / "scripts/gem_public_oracles/django_10554_sqlite_v2.py",
    )

    hashes = manifest["source_hashes"]
    required_sources = {
        "self_evolving_agent/gem_t3_production_launcher.py",
        "self_evolving_agent/gem_t3_condition_runner.py",
        "self_evolving_agent/gem_public_oracle.py",
        "self_evolving_agent/gem_sqlite_v2_oracle.py",
        "scripts/launch_gem_t3_production.py",
        "scripts/run_gem_t3_condition.py",
        "scripts/gem_public_oracles/astropy_13033.py",
        "scripts/gem_public_oracles/django_10554_sqlite_v2.py",
        "scripts/gem_public_oracles/matplotlib_14623.py",
    }
    if not isinstance(hashes, dict) or set(hashes) != required_sources:
        raise LauncherContractError("launcher source manifest drift")
    for relative, expected_hash in hashes.items():
        path = (project_root / relative).resolve()
        if project_root.resolve() not in path.parents or not path.is_file():
            raise LauncherContractError("launcher source missing or outside project")
        if file_sha256(path) != expected_hash:
            raise LauncherContractError("launcher source hash drift")
    return canonical_digest(manifest)


def validate_authorization_envelope(envelope: dict[str, Any], manifest_digest: str) -> None:
    _closed(envelope, {
        "schema_version", "state", "approval_id", "manifest_digest",
        "independent_verification", "execution_authorized", "token_env",
        "token_sha256", "child_token_hashes", "verification_report",
        "verification_report_sha256",
    }, "authorization envelope")
    if envelope["schema_version"] != ENVELOPE_SCHEMA or envelope["approval_id"] != APPROVAL_ID:
        raise LauncherContractError("authorization envelope identity drift")
    if envelope["manifest_digest"] != manifest_digest:
        raise LauncherContractError("authorization envelope manifest digest mismatch")
    if envelope["token_env"] != "GEM_T3_V2_AUTHORIZATION_TOKEN":
        raise LauncherContractError("authorization environment binding drift")
    if envelope["verification_report"] != VERIFICATION_REPORT:
        raise LauncherContractError("verification report path drift")
    if envelope["state"] not in {"prepared_not_authorized", "verified_authorized"}:
        raise LauncherContractError("authorization envelope state is invalid")
    if envelope["state"] == "prepared_not_authorized":
        if (
            envelope["independent_verification"] != "pending"
            or envelope["execution_authorized"] is not False
            or envelope["token_sha256"] != ""
            or envelope["child_token_hashes"] != {}
            or envelope["verification_report_sha256"] != ""
        ):
            raise LauncherContractError("prepared envelope cannot authorize execution")
    else:
        if envelope["independent_verification"] != "GO" or envelope["execution_authorized"] is not True:
            raise LauncherContractError("verified envelope lacks independent GO")
        if not _is_sha256(envelope["token_sha256"]) or not _is_sha256(
            envelope["verification_report_sha256"]
        ):
            raise LauncherContractError("verified envelope hash binding is invalid")
        expected_keys = [f"{condition}:{task}" for condition in CONDITIONS for task in TASK_IDS]
        if list(envelope["child_token_hashes"]) != expected_keys or not all(
            _is_sha256(value) for value in envelope["child_token_hashes"].values()
        ):
            raise LauncherContractError("verified child token hashes are incomplete")


def child_process_token(master_token: str, manifest_digest: str, condition: str, task_id: str) -> str:
    if not master_token:
        raise LauncherContractError("process-local authorization token is absent")
    message = canonical_digest({
        "approval_id": APPROVAL_ID,
        "manifest_digest": manifest_digest,
        "condition": condition,
        "task_id": task_id,
    }).encode("ascii")
    return hmac.new(master_token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def authorize_run(
    manifest: dict[str, Any], envelope: dict[str, Any], project_root: Path,
    env: Mapping[str, str],
) -> str:
    """Complete every non-secret, non-mutating gate and return the bound digest."""
    digest = validate_manifest(manifest, project_root)
    validate_authorization_envelope(envelope, digest)
    if envelope["state"] != "verified_authorized":
        raise LauncherContractError("production execution is disabled by default")
    observed = env.get(envelope["token_env"], "")
    if not observed or not hmac.compare_digest(
        hashlib.sha256(observed.encode("utf-8")).hexdigest(),
        envelope["token_sha256"],
    ):
        raise LauncherContractError("process-local authorization token mismatch")
    envelope_path = _canonical_envelope_path(project_root)
    if not envelope_path.is_file() or load_json(envelope_path) != envelope:
        raise LauncherContractError("runtime authorization envelope file mismatch")
    _validate_verification_report(project_root, envelope)
    check_outputs(manifest, project_root)
    return digest


def check_outputs(manifest: dict[str, Any], project_root: Path) -> None:
    root = project_root.resolve()
    for condition in CONDITIONS:
        for task_id in TASK_IDS:
            path = (root / manifest["outputs"][condition][task_id]).resolve()
            if root not in path.parents:
                raise LauncherContractError("output escapes project root")
            recovery = manifest["recovery"]["completed_judge_results"]
            if condition == "Judge" and task_id in recovery:
                expected = (root / recovery[task_id]["path"]).resolve()
                if (
                    not path.is_dir()
                    or expected.parent != path
                    or {item.name for item in path.iterdir()} != {"judge_result.json"}
                    or file_sha256(expected) != recovery[task_id]["sha256"]
                ):
                    raise LauncherContractError("recovered Judge output is not exact")
                continue
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                raise LauncherContractError("production output is occupied")
    harness = (root / manifest["harness_output"]).resolve()
    if root not in harness.parents:
        raise LauncherContractError("harness output escapes project root")
    if harness.exists() and (not harness.is_dir() or any(harness.iterdir())):
        raise LauncherContractError("harness output is occupied")


def launch(manifest: dict[str, Any], envelope: dict[str, Any], project_root: Path,
           env: Mapping[str, str] | None = None) -> list[int]:
    """Run Judge, Exec-oracle processing, then official harness after the gate."""
    source = dict(os.environ if env is None else env)
    authorize_run(manifest, envelope, project_root, source)

    # This is intentionally after authorization.  Do not open or parse this file.
    credential_source = Path(manifest["credential_source"])
    if not credential_source.is_file():
        raise LauncherContractError("credential unavailable")
    deadline = time.monotonic() + manifest["timeouts_seconds"]["total"]
    exits: list[int] = []
    # A prior Judge API response completed before its usage/cost record could be
    # persisted.  Reserve the entire original per-task cap so a manual recovery
    # cannot silently erase that unknown paid call from the aggregate budget.
    spent = float(manifest["budget"]["prior_unknown_cost_reserve_usd"])
    for record in manifest["commands"]:
        if record["condition"] == "Judge" and record["task_id"] in manifest[
            "recovery"
        ]["completed_judge_results"]:
            judge_path = project_root / manifest["recovery"]["completed_judge_results"][
                record["task_id"]
            ]["path"]
            recovered = load_json(judge_path)
            _validate_recovered_judge(recovered, manifest, record["task_id"])
            spent += float(recovered["usage"]["cost_usd"])
            if spent > manifest["budget"]["total"]:
                raise LauncherContractError("total budget exceeded by recovered evidence")
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LauncherContractError("total wall timeout exhausted")
        child_env = source.copy()
        # Authorization material belongs to the launcher process, never the
        # solver or its logs.  Remove it immediately after the gate succeeds.
        child_env.pop(envelope["token_env"], None)
        master_token = source[envelope["token_env"]]
        child_token = child_process_token(
            master_token, validate_manifest(manifest, project_root),
            record["condition"], record["task_id"]
        )
        expected_child_hash = envelope["child_token_hashes"][
            f"{record['condition']}:{record['task_id']}"
        ]
        if hashlib.sha256(child_token.encode("utf-8")).hexdigest() != expected_child_hash:
            raise LauncherContractError("runtime child token does not match envelope")
        child_env.update({
            "GEM_T3_V2_APPROVAL_ID": APPROVAL_ID,
            "GEM_T3_V2_MANIFEST_DIGEST": validate_manifest(manifest, project_root),
            "GEM_T3_V2_CONDITION": record["condition"],
            "GEM_T3_V2_TASK_ID": record["task_id"],
            "GEM_T3_V2_CHILD_AUTHORIZATION_TOKEN": child_token,
        })
        argv = record["launched_argv"]
        try:
            result = subprocess.run(
                argv, cwd=project_root, env=child_env, check=False,
                timeout=min(manifest["timeouts_seconds"]["task"], remaining),
            )
        except subprocess.TimeoutExpired as exc:
            raise LauncherContractError("task runtime timeout") from exc
        exits.append(result.returncode)
        if result.returncode != 0:
            raise LauncherContractError("task subprocess failed; fail-fast")
        if record["condition"] == "Judge":
            judge_path = project_root / manifest["outputs"]["Judge"][record["task_id"]] / "judge_result.json"
            if not judge_path.is_file():
                raise LauncherContractError("Judge result is missing")
            usage = load_json(judge_path).get("usage", {})
            cost = usage.get("cost_usd")
            if not isinstance(cost, (int, float)) or cost < 0:
                raise LauncherContractError("Judge cost evidence is unavailable")
            evidence = usage.get("cost_evidence")
            if not isinstance(evidence, dict) or evidence.get("billing_model") != manifest[
                "cost_policy"
            ]["billing_model"]:
                raise LauncherContractError("Judge cost evidence is not manifest-bound")
            if cost > manifest["budget"]["per_task_per_condition"]:
                raise LauncherContractError("per-task Judge budget exceeded")
            spent += float(cost)
            if spent > manifest["budget"]["total"]:
                raise LauncherContractError("total budget exceeded")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LauncherContractError("total wall timeout exhausted before harness")
    harness_env = source.copy()
    for name in (envelope["token_env"],):
        harness_env.pop(name, None)
    try:
        result = subprocess.run(
            manifest["harness_command"]["argv"], cwd=project_root,
            env=harness_env, check=False,
            timeout=min(manifest["timeouts_seconds"]["harness"], remaining),
        )
    except subprocess.TimeoutExpired as exc:
        raise LauncherContractError("official harness timeout") from exc
    exits.append(result.returncode)
    if result.returncode != 0:
        raise LauncherContractError("official harness infrastructure failed")
    return exits


def _solver_argv(manifest: dict[str, Any], task_id: str, output: str) -> list[str]:
    return [
        manifest["runtime"]["python"], "scripts/run_gem_t3_condition.py",
        "--manifest", "configs/gem_t3_execution_application_v2.json",
        "--condition", next(
            condition for condition in CONDITIONS
            if manifest["outputs"][condition][task_id] == output
        ),
        "--task-id", task_id, "--output-dir", output,
    ]


def _harness_argv(manifest: dict[str, Any]) -> list[str]:
    return [
        manifest["runtime"]["python"], "-m", "swebench.harness.run_evaluation",
        "--dataset_name", "princeton-nlp/SWE-Bench_Verified",
        "--split", "test",
        "--predictions_path", "runs/gem_t3_nomem_trajectory_collection/preds_t3_nomem.json",
        "--max_workers", "1", "--timeout", "1800",
        "--run_id", "gem_t3_sqlite_v2_20260712",
        "--report_dir", manifest["harness_output"],
        "--instance_ids", *TASK_IDS,
    ]


def _launched_argv(manifest: dict[str, Any], condition: str, argv: list[str]) -> list[str]:
    if condition == "Exec":
        return list(argv)
    wrapper = (
        'set -a; source "$1" >/dev/null 2>&1 || exit 78; '
        '[ -n "${OPENAI_API_KEY:-}${AZURE_OPENAI_API_KEY:-}" ] || exit 79; '
        'shift; exec "$@"'
    )
    return [
        "/bin/bash", "-c", wrapper, "gem-credential-wrapper",
        manifest["credential_source"], *argv,
    ]


def _validate_recovered_judge(record: dict[str, Any], manifest: dict[str, Any], task_id: str) -> None:
    required = {
        "schema_version", "created_at", "authorization_ref", "condition", "task_id",
        "candidate_id", "provider", "model", "decision", "confidence", "reason", "usage",
        "admission_event_created", "sidecar_created", "task_solved", "leakage_flags",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise LauncherContractError("recovered Judge fields are not closed")
    if (
        record["schema_version"] != "gem.t3_judge_result.v2"
        or record["authorization_ref"] != APPROVAL_ID
        or record["condition"] != "Judge"
        or record["task_id"] != task_id
        or record["provider"] != manifest["provider"]
        or record["model"] != manifest["model"]
        or record["decision"] not in {"admitted", "quarantined"}
        or record["admission_event_created"] is not False
        or record["sidecar_created"] is not False
        or record["task_solved"] is not False
        or record["leakage_flags"] != []
    ):
        raise LauncherContractError("recovered Judge semantic linkage drift")
    usage = record["usage"]
    evidence = usage.get("cost_evidence") if isinstance(usage, dict) else None
    cost = usage.get("cost_usd") if isinstance(usage, dict) else None
    if (
        not isinstance(cost, (int, float))
        or cost < 0
        or not isinstance(evidence, dict)
        or evidence.get("billing_model") != manifest["cost_policy"]["billing_model"]
        or evidence.get("provider_invoice") is not False
    ):
        raise LauncherContractError("recovered Judge cost evidence is invalid")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_verification_report(project_root: Path, envelope: dict[str, Any]) -> None:
    report_path = project_root / envelope["verification_report"]
    if (
        not report_path.is_file()
        or file_sha256(report_path) != envelope["verification_report_sha256"]
        or "Verdict: **GO**" not in report_path.read_text(encoding="utf-8")
    ):
        raise LauncherContractError("independent verification report is not bound GO")


def _canonical_envelope_path(project_root: Path) -> Path:
    return (project_root / "configs/gem_t3_authorization_envelope_v2.json").resolve()


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _closed(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise LauncherContractError(f"{label} fields are not closed")
