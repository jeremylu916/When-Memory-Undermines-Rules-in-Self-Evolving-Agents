"""Read-only four-phase readiness checks for the real SMP experiment pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any, Callable, Mapping, Sequence

import yaml

from self_evolving_agent.python_runtime import default_harness_python
from self_evolving_agent.smp_artifacts import file_hash
from self_evolving_agent.smp_contracts import canonical_hash
from self_evolving_agent.smp_io import (
    certificate_from_dict,
    load_json,
    load_manifest,
    load_profile,
    load_snapshot,
    memory_from_dict,
)
from self_evolving_agent.smp_manifest import (
    production_certificate_compatible,
    validate_frozen_bank_payload,
    validate_static_entry_schedule,
)
from self_evolving_agent.smp_protocol import (
    FrameworkInvariantError,
    ProtocolCertificate,
    RuntimeBundle,
    V4DiagnosticManifest,
    load_runtime_bundle,
    recompute_runtime_bundle,
    resolve_mini_swe_root,
    resolve_project_python,
    require_external_output_paths,
    validate_runtime_environment_bindings,
)
from self_evolving_agent.smp_capabilities import capability_probe_plan_from_mapping, load_capability_registry


PHASES = ("capability_probe", "source_shadow", "f_r_calibration", "utility_trial", "test_static_paired")
PHASE_ARTIFACTS = {
    "capability_probe": ("probe_plan", "probe_payloads"),
    "source_shadow": (),
    "f_r_calibration": ("train_r", "tune_f", "tune_r", "cert_f", "cert_r"),
    "utility_trial": ("profile", "trial_entries"),
    "test_static_paired": ("profile", "active_entries", "active_bank", "utility_certificates"),
}


def credential_safe_environment(
    environment: Mapping[str, str], credential_name: str
) -> tuple[dict[str, str], bool]:
    """Copy an environment without ever retrieving the configured credential value."""

    present = credential_name in environment
    sanitized = {name: environment[name] for name in environment if name != credential_name}
    return sanitized, present


@dataclass(frozen=True)
class PreflightRequest:
    phase: str
    manifest_path: str
    model_config_path: str
    mini_config_paths: tuple[str, ...]
    output_root: str
    artifacts: Mapping[str, str]
    run_id: str
    harness_python: str = ""
    credential_env_name: str = "OPENAI_API_KEY"
    protocol_manifest_path: str = ""
    runtime_bundle_path: str = ""
    protocol_certificate_path: str = ""
    exclusion_registry_path: str = ""
    cost_ledger_path: str = ""
    capability_registry_path: str = ""

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError("unsupported SMP preflight phase")
        if not self.run_id or not self.mini_config_paths:
            raise ValueError("preflight requires run ID and frozen mini-SWE configs")


def run_preflight(
    request: PreflightRequest,
    *,
    command_runner: Callable[..., Any] = subprocess.run,
    process_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    launcher = root / "scripts" / "smp_python"
    environment = os.environ if process_environment is None else process_environment
    evo_python = resolve_project_python(root, environment)
    mini_swe_root = resolve_mini_swe_root(root, environment)
    mini_swe_python = mini_swe_root / ".venv" / "bin" / "python"
    harness_python = request.harness_python or default_harness_python()
    checks: dict[str, dict[str, Any]] = {}

    checks["project_interpreter"] = _path_check(evo_python, executable=True)
    checks["mini_swe_checkout"] = {"ok": mini_swe_root.is_dir(), "path": str(mini_swe_root)}
    checks["mini_swe_interpreter"] = _path_check(mini_swe_python, executable=True)
    checks["supported_launcher"] = _path_check(launcher, executable=True)
    isolated_env, credential_present = credential_safe_environment(
        environment, request.credential_env_name
    )
    output = Path(request.output_root).resolve()
    global_config_dir = (output / ".smp_preflight_miniswe_config").resolve()
    isolated_env["MSWEA_GLOBAL_CONFIG_DIR"] = str(global_config_dir)
    isolated_env["SMP_FROZEN_GLOBAL_CONFIG_DIR"] = str(global_config_dir)
    isolated_env["MSWEA_SILENT_STARTUP"] = "1"
    isolated_env["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
    isolated_env["SMP_MINISWE_ROOT"] = str(mini_swe_root)
    isolated_env["SMP_PROJECT_PYTHON"] = str(evo_python)
    if request.runtime_bundle_path:
        try:
            runtime_bundle = load_runtime_bundle(request.runtime_bundle_path)
            validate_runtime_environment_bindings(runtime_bundle, root, isolated_env, output_root=output)
            checks["runtime_environment_binding"] = {"ok": True, "mini_swe_root": str(mini_swe_root), "project_python": str(evo_python)}
            if runtime_bundle.model_budget.get("bundle_stage") == "pre_probe":
                checks["gate3_capabilities"] = {"ok": True, "status": "pre_probe_plan_ready"}
            elif runtime_bundle.model_budget.get("bundle_stage") == "final_execution" and request.capability_registry_path:
                capability_registry = load_capability_registry(request.capability_registry_path)
                capability_registry.require_gate3()
                if capability_registry.registry_hash != runtime_bundle.model_budget.get("capability_registry_hash"):
                    raise FrameworkInvariantError("runtime_capability_registry_mismatch")
                checks["gate3_capabilities"] = {"ok": True, "registry_hash": capability_registry.registry_hash}
            else:
                checks["gate3_capabilities"] = {"ok": False, "reason": "final capability registry is unavailable"}
        except Exception as exc:
            checks["runtime_environment_binding"] = {"ok": False, "reason": _safe_error(exc)}
            checks["gate3_capabilities"] = {"ok": False, "reason": _safe_error(exc)}
    else:
        checks["runtime_environment_binding"] = {"ok": True, "reason": "legacy preflight without v4 runtime bundle"}
        checks["gate3_capabilities"] = {"ok": True, "reason": "legacy preflight without v4 runtime bundle"}
    try:
        if not checks["runtime_environment_binding"]["ok"]:
            raise FrameworkInvariantError("preflight_runtime_environment_binding_failed")
        require_external_output_paths(root, miniswe_global_config_dir=global_config_dir)
        if not global_config_dir.is_relative_to(output) or global_config_dir == output:
            raise FrameworkInvariantError("preflight_global_config_outside_output_root")
        global_config_dir.mkdir(parents=True, exist_ok=True)
        if any(global_config_dir.iterdir()):
            raise FrameworkInvariantError("preflight_global_config_not_empty")
        checks["miniswe_global_config"] = {"ok": True, "path": str(global_config_dir), "empty": True}
    except (OSError, FrameworkInvariantError) as exc:
        checks["miniswe_global_config"] = {"ok": False, "reason": _safe_error(exc)}
    checks["mini_swe_import"] = _command_check(
        [str(launcher), "-c", "from minisweagent.agents.default import DefaultAgent; from self_evolving_agent.smp_mini_agent import SMPMiniAgent"],
        command_runner,
        env=isolated_env,
    )
    checks["required_python_packages"] = {
        "ok": all(importlib.util.find_spec(name) is not None for name in ("yaml",)),
        "required": ["yaml"],
    }
    checks["strict_structured_sdk"] = _command_check(
        [
            str(launcher),
            "-c",
            "import inspect; from openai import OpenAI; c=OpenAI(base_url='https://yuexing-4772-resource.services.ai.azure.com/openai/v1',api_key='presence-check-only'); p=inspect.signature(c.chat.completions.create).parameters; assert {'model','messages','response_format'} <= set(p)",
        ],
        command_runner,
        env=isolated_env,
    )
    checks["credential_environment"] = {
        "ok": credential_present,
        "environment_variable": request.credential_env_name,
        "present": credential_present,
    }
    docker = shutil.which("docker")
    checks["docker_cli"] = {"ok": bool(docker), "path": docker}
    checks["docker_daemon"] = (
        _command_check([docker, "info", "--format", "{{json .ServerVersion}}"], command_runner)
        if docker
        else {"ok": False, "reason": "docker CLI unavailable"}
    )
    checks["swebench_harness"] = _command_check(
        [harness_python, "-c", "import swebench.harness.run_evaluation"], command_runner
    )

    model_path = Path(request.model_config_path)
    checks["model_config"] = _file_check(model_path)
    if checks["model_config"]["ok"]:
        checks["model_config"]["hash"] = file_hash(model_path)
        try:
            merged_config = _load_merged_runner_config(request)
            if _contains_inline_secret_key(merged_config):
                raise ValueError("model config contains an inline credential field")
            model_row = merged_config.get("model", merged_config)
            model_name = str(model_row["model_name"])
            tokenizer_env = dict(isolated_env)
            tokenizer_env.pop(request.credential_env_name, None)
            tokenizer_env["SMP_PREFLIGHT_MODEL_NAME"] = model_name
            tokenizer_env["SMP_PREFLIGHT_MERGED_MODEL_JSON"] = json.dumps(model_row, sort_keys=True, ensure_ascii=True)
            checks["token_accounting"] = _command_check(
                [str(launcher), "-c", "import json,os; from litellm import token_counter; from minisweagent.models import get_model; c=json.loads(os.environ['SMP_PREFLIGHT_MERGED_MODEL_JSON']); get_model(config=c); n=token_counter(model=os.environ['SMP_PREFLIGHT_MODEL_NAME'], text='smp readiness'); assert isinstance(n,int) and n>0"],
                command_runner,
                env=tokenizer_env,
            )
        except Exception as exc:
            checks["token_accounting"] = {"ok": False, "reason": _safe_error(exc)}
    else:
        checks["token_accounting"] = {"ok": False, "reason": "model config unavailable"}
    mini_rows = [_file_check(Path(path)) for path in request.mini_config_paths]
    for row, path in zip(mini_rows, request.mini_config_paths):
        if row["ok"]:
            row["hash"] = file_hash(path)
    checks["mini_configs"] = {"ok": all(row["ok"] for row in mini_rows), "files": mini_rows}

    manifest = None
    try:
        manifest = load_manifest(request.manifest_path)
        _validate_frozen_runner_config(request, manifest)
        required_splits = {
            "capability_probe": ("train-source",),
            "source_shadow": ("train-source",),
            "f_r_calibration": ("train-source", "dev-tune", "dev-cert"),
            "utility_trial": ("dev-cert",),
            "test_static_paired": ("test-static",),
        }[request.phase]
        if any(not manifest.splits[name] for name in required_splits):
            raise ValueError("manifest has no targets for required phase splits: " + ", ".join(required_splits))
        if request.phase == "utility_trial" and not manifest.utility_checkpoint_hash:
            raise ValueError("utility trial phase requires a frozen subject/task-seed checkpoint")
        if request.phase == "source_shadow" and any(
            any(name not in row for name in ("task_id", "problem_statement", "task_index", "run_index", "seed"))
            for row in manifest.splits["train-source"]
        ):
            raise ValueError("train-source rows are missing executable task/run fields")
        checks["target_manifest"] = {
            "ok": True,
            "manifest_id": manifest.manifest_id,
            "manifest_hash": manifest.manifest_hash,
            "experiment_type": manifest.experiment_type,
        }
        checks["split_leakage"] = {"ok": True, "splits": {name: len(rows) for name, rows in manifest.splits.items()}}
    except Exception as exc:
        checks["target_manifest"] = {"ok": False, "reason": _safe_error(exc)}
        checks["split_leakage"] = {"ok": False, "reason": "manifest validation did not pass"}

    missing_artifacts = []
    artifact_checks: dict[str, dict[str, Any]] = {}
    for name in PHASE_ARTIFACTS[request.phase]:
        path = request.artifacts.get(name, "")
        row = _file_check(Path(path)) if path else {"ok": False, "reason": f"missing --artifact {name}=PATH"}
        artifact_checks[name] = row
        if not row["ok"]:
            missing_artifacts.append(name)
    if not missing_artifacts and manifest is not None:
        try:
            _validate_phase_artifacts(request, manifest)
        except Exception as exc:
            artifact_checks["compatibility"] = {"ok": False, "reason": _safe_error(exc)}
            missing_artifacts.append("incompatible_phase_artifacts")
    checks["phase_artifacts"] = {"ok": not missing_artifacts, "artifacts": artifact_checks}

    try:
        output.mkdir(parents=True, exist_ok=True)
        probe = output / ".smp_preflight_write_probe"
        probe.write_text("readiness\n", encoding="utf-8")
        probe.unlink()
        checks["output_directory"] = {"ok": True, "path": str(output.resolve())}
    except OSError as exc:
        checks["output_directory"] = {"ok": False, "reason": _safe_error(exc)}

    code_check_names = (
        "project_interpreter",
        "mini_swe_checkout",
        "mini_swe_interpreter",
        "supported_launcher",
        "runtime_environment_binding",
        "miniswe_global_config",
        "gate3_capabilities",
        "mini_swe_import",
        "required_python_packages",
        "strict_structured_sdk",
        "model_config",
        "token_accounting",
        "mini_configs",
        "target_manifest",
        "split_leakage",
        "output_directory",
    )
    code_ready = all(checks[name]["ok"] for name in code_check_names)
    environment_ready = checks["credential_environment"]["ok"] and checks["docker_cli"]["ok"] and checks["docker_daemon"]["ok"] and checks["swebench_harness"]["ok"]
    phase_ready = code_ready and environment_ready and not missing_artifacts
    next_command = _next_command(request, root=root, environment=environment)
    report = {
        "schema_version": "smp.preflight.v1",
        "phase": request.phase,
        "status": "ready" if phase_ready else "blocked",
        "code_ready": code_ready,
        "environment_ready": environment_ready,
        "missing_experimental_artifacts": missing_artifacts,
        "checks": checks,
        "immutable_run_manifest_hash": manifest.manifest_hash if manifest else None,
        "exact_next_command": next_command,
    }
    report["report_hash"] = canonical_hash(report)
    return report


def issue_v4_protocol_certificate(
    *,
    protocol_manifest: V4DiagnosticManifest,
    expected_bundle: RuntimeBundle,
    repo_root: str | Path,
    exclusion_registry_hash: str,
    schema_payloads: Mapping[str, Any],
    preflight_report: Mapping[str, Any],
) -> ProtocolCertificate:
    if preflight_report.get("status") != "ready" or preflight_report.get("code_ready") is not True:
        raise FrameworkInvariantError("protocol_certificate_requires_ready_preflight")
    bundle = recompute_runtime_bundle(
        expected_bundle,
        repo_root,
        schema_payloads=schema_payloads,
        exclusion_registry_hash=exclusion_registry_hash,
    )
    if bundle.bundle_hash != protocol_manifest.runtime_bundle_hash:
        raise FrameworkInvariantError("diagnostic_manifest_bundle_mismatch")
    report_hash = preflight_report.get("report_hash")
    if not isinstance(report_hash, str):
        raise FrameworkInvariantError("preflight_report_hash_missing")
    return ProtocolCertificate(
        manifest_hash=protocol_manifest.manifest_hash,
        runtime_bundle_hash=bundle.bundle_hash,
        commit=bundle.commit,
        phase="capability_probe" if bundle.model_budget.get("bundle_stage") == "pre_probe" else "diagnostic_smoke",
        selected_target_manifest_hash=protocol_manifest.selected_target_manifest_hash,
        preflight_report_hash=report_hash,
    )


def _next_command(request: PreflightRequest, *, root: Path, environment: Mapping[str, str] | None = None) -> str:
    python = str(resolve_project_python(root, environment))
    mini_swe_root = str(resolve_mini_swe_root(root, environment))
    launcher = str(root / "scripts" / "smp_python")
    environment_prefix = [
        "env",
        f"SMP_MINISWE_ROOT={mini_swe_root}",
        f"SMP_PROJECT_PYTHON={python}",
        f"MSWEA_GLOBAL_CONFIG_DIR={Path(request.output_root).resolve() / '.smp_preflight_miniswe_config'}",
        f"SMP_FROZEN_GLOBAL_CONFIG_DIR={Path(request.output_root).resolve() / '.smp_preflight_miniswe_config'}",
        "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=1",
    ]
    module = ["-m", "scripts.run_smp"]
    common = ["--manifest", request.manifest_path, "--run-id", request.run_id, "--output-root", request.output_root]
    protocol = []
    if request.protocol_manifest_path:
        if not all((request.runtime_bundle_path, request.protocol_certificate_path, request.exclusion_registry_path)):
            raise FrameworkInvariantError("preflight_protocol_paths_incomplete")
        protocol = [
            "--protocol-manifest", request.protocol_manifest_path,
            "--runtime-bundle", request.runtime_bundle_path,
            "--protocol-certificate", request.protocol_certificate_path,
            "--exclusion-registry", request.exclusion_registry_path,
        ]
        if not request.cost_ledger_path:
            raise FrameworkInvariantError("preflight_cost_ledger_path_missing")
        protocol.extend(["--cost-ledger", request.cost_ledger_path])
        if request.capability_registry_path:
            protocol.extend(["--capability-registry", request.capability_registry_path])
    if request.phase == "capability_probe":
        command = [
            *environment_prefix, launcher, *module, "probe-capabilities", *protocol,
            "--output-root", request.output_root,
            "--probe-root", str(Path(request.output_root).resolve() / "capability-probes"),
            "--probe-plan", request.artifacts.get("probe_plan", "<capability-probe-plan.json>"),
            "--probe-payloads", request.artifacts.get("probe_payloads", "<capability-probe-payloads.json>"),
            "--capability-registry-output", request.capability_registry_path or "<capability-registry.json>",
            "--model-config", request.model_config_path,
        ]
    elif request.phase == "source_shadow":
        command = [*environment_prefix, launcher, *module, "run-source-shadow", *common, *protocol, "--bank-version", "0", "--extractor-prompt", str(root / "configs" / "smp_signature_extractor_prompt_v1.txt")]
    elif request.phase == "f_r_calibration":
        command = [*environment_prefix, python, *module, "calibrate", *common]
        for name in PHASE_ARTIFACTS[request.phase]:
            command.extend([f"--{name.replace('_', '-')}", request.artifacts.get(name, f"<{name}.json>")])
        command.extend(["--extractor-prompt", str(root / "configs" / "smp_signature_extractor_prompt_v1.txt")])
        command.extend(["--model-config", request.model_config_path, "--certification-registry", "<certification-registry.json>", "--output", "<frozen-calibration-profile.json>", "--issued-at", "<UTC-timestamp>"])
    elif request.phase == "utility_trial":
        command = [*environment_prefix, launcher, *module, "run-paired", *common, "--split", "dev-cert", "--entries", request.artifacts.get("trial_entries", "<authorized-trial-entries.json>"), "--extractor-prompt", str(root / "configs" / "smp_signature_extractor_prompt_v1.txt")]
    else:
        command = [*environment_prefix, launcher, *module, "run-paired", *common, "--split", "test-static", "--entries", request.artifacts.get("active_entries", "<frozen-active-entries.json>"), "--active-bank", request.artifacts.get("active_bank", "<frozen-active-bank.json>"), "--certificate-bundle", request.artifacts.get("utility_certificates", "<utility-certificates.json>"), "--extractor-prompt", str(root / "configs" / "smp_signature_extractor_prompt_v1.txt")]
    if request.phase != "f_r_calibration":
        config_paths = list(request.mini_config_paths)
        if request.model_config_path not in config_paths:
            config_paths.append(request.model_config_path)
        for path in config_paths:
            command.extend(["--mini-config", path])
    return shlex.join(command)


def _file_check(path: Path) -> dict[str, Any]:
    return {"ok": path.is_file(), "path": str(path)} if str(path) else {"ok": False, "reason": "path is empty"}


def _path_check(path: Path, *, executable: bool) -> dict[str, Any]:
    ok = path.is_file() and (not executable or os.access(path, os.X_OK))
    return {"ok": ok, "path": str(path)}


def _command_check(command: Sequence[str], runner: Callable[..., Any], *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    try:
        completed = runner(
            list(command),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            env=dict(env) if env is not None else None,
        )
        return {"ok": completed.returncode == 0, "returncode": completed.returncode}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": _safe_error(exc)}


def _safe_error(exc: BaseException) -> str:
    return type(exc).__name__ + ": " + str(exc)[:300]


def _contains_inline_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in {"api_key", "token", "password", "secret", "authorization"}:
                return True
            if _contains_inline_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_inline_secret_key(child) for child in value)
    return False


def _load_merged_runner_config(request: PreflightRequest) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    paths = list(request.mini_config_paths)
    if request.model_config_path not in paths:
        paths.append(request.model_config_path)
    for path in paths:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(value, Mapping):
            raise ValueError("mini-SWE config must be a YAML object")
        merged = _deep_merge(merged, value)
    return merged


def _validate_frozen_runner_config(request: PreflightRequest, manifest: Any) -> None:
    merged = _load_merged_runner_config(request)
    model = dict(merged.get("model", {}))
    agent = dict(merged.get("agent", {}))
    expected = {
        "model_hash": canonical_hash({"model_class": model.get("model_class"), "model_name": model.get("model_name")}),
        "model_parameters_hash": canonical_hash(model.get("model_kwargs", {})),
        "prompt_hash": canonical_hash({"system": agent.get("system_template"), "instance": agent.get("instance_template")}),
        "solver_budget_hash": canonical_hash({name: agent.get(name) for name in ("step_limit", "cost_limit", "wall_time_limit_seconds")}),
    }
    mismatches = [name for name, value in expected.items() if manifest.frozen_components[name] != value]
    if mismatches:
        raise ValueError("manifest/config freeze mismatch: " + ", ".join(mismatches))


def _deep_merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_phase_artifacts(request: PreflightRequest, manifest: Any) -> None:
    if request.phase == "capability_probe":
        plan_payload = load_json(request.artifacts["probe_plan"])
        payloads = load_json(request.artifacts["probe_payloads"])
        if not isinstance(plan_payload, Mapping) or not isinstance(payloads, Mapping):
            raise ValueError("capability probe plan and payloads must be JSON objects")
        plan = capability_probe_plan_from_mapping(plan_payload)
        if (
            canonical_hash(payloads.get("solver")) != plan.solver_payload_hash
            or canonical_hash(payloads.get("structured")) != plan.structured_payload_hash
        ):
            raise ValueError("capability probe payload binding mismatch")
        bundle = load_runtime_bundle(request.runtime_bundle_path)
        if bundle.model_budget.get("bundle_stage") != "pre_probe" or bundle.model_budget.get("capability_probe_plan_hash") != plan.plan_hash:
            raise ValueError("capability probe runtime bundle binding mismatch")
        return
    if request.phase == "source_shadow":
        return
    if request.phase == "f_r_calibration":
        for name in PHASE_ARTIFACTS[request.phase]:
            value = load_json(request.artifacts[name])
            if not isinstance(value, list) or not value:
                raise ValueError(f"{name} must be a non-empty calibration record array")
        return
    profile = load_profile(request.artifacts["profile"])
    if not profile.compatible(domain="swe", memory_schema_version="smp.memory.v1", test_mode=False):
        raise ValueError("phase calibration profile is not production-compatible")
    entries_name = "trial_entries" if request.phase == "utility_trial" else "active_entries"
    entries = load_json(request.artifacts[entries_name])
    if not isinstance(entries, list) or not entries:
        raise ValueError("paired entries must be a non-empty array")
    bank = None
    frozen_memories = ()
    if request.phase == "test_static_paired":
        static_schedule = validate_static_entry_schedule(manifest, entries)
        bank = load_json(request.artifacts["active_bank"])
        frozen_rows = validate_frozen_bank_payload(manifest, bank)
        frozen_memories = tuple(memory_from_dict(row) for row in frozen_rows)
    for entry_index, entry in enumerate(entries):
        entry_profile = load_profile(entry["profile"])
        if entry_profile.artifact_hash != profile.artifact_hash:
            raise ValueError("paired entry calibration profile differs from phase profile")
        nomem = load_snapshot(entry["nomem_snapshot"])
        treatment = load_snapshot(entry["treatment_snapshot"])
        task = entry.get("task", {})
        if treatment.task_id != str(task.get("task_id")) or nomem.task_id != treatment.task_id:
            raise ValueError("paired entry task/snapshot linkage is invalid")
        if nomem.memories:
            raise ValueError("NoMem preflight snapshot contains memory")
        if request.phase == "utility_trial":
            authorization = treatment.authorization
            if (
                authorization.manifest_hash != manifest.manifest_hash
                or authorization.profile_hash != profile.artifact_hash
                or authorization.checkpoint_hash != manifest.utility_checkpoint_hash
                or authorization.run_id != request.run_id
                or not any(
                    str(row["subject_id"]) == authorization.source_memory_id
                    and str(row["task_id"]) == authorization.target_task_id
                    and int(row["seed"]) == int(entry["seed"])
                    and int(row["run_index"]) == authorization.run_index
                    for row in manifest.checkpoint_task_seeds
                )
            ):
                raise ValueError("utility trial entry authorization linkage is invalid")
        else:
            expected_memories = tuple(
                memory for memory in frozen_memories
                if memory.source_task_id != str(task["task_id"])
                and memory.available_from_task_index <= treatment.task_index
            )
            if (
                any(memory.state.value not in {"active", "narrow"} for memory in treatment.memories)
                or treatment.task_index != int(static_schedule[entry_index]["task_index"])
                or nomem.task_index != treatment.task_index
                or nomem.bank_version != int(bank["bank_version"])
                or treatment.bank_version != int(bank["bank_version"])
                or nomem.bank_item_count != len(frozen_memories)
                or treatment.bank_item_count != len(frozen_memories)
                or tuple(memory.to_dict() for memory in treatment.memories)
                != tuple(memory.to_dict() for memory in expected_memories)
            ):
                raise ValueError("test-static snapshots do not exactly derive from the frozen active bank")
    if request.phase == "test_static_paired":
        bank_ids = {str(row["memory_id"]) for row in bank.get("memories", [])}
        raw_certificates = load_json(request.artifacts["utility_certificates"])
        values = raw_certificates.get("certificates", raw_certificates) if isinstance(raw_certificates, Mapping) else raw_certificates
        rows = values.values() if isinstance(values, Mapping) else values
        certificates = [certificate_from_dict(row) for row in rows]
        active_ids = {memory.memory_id for entry in entries for memory in load_snapshot(entry["treatment_snapshot"]).memories}
        if not active_ids.issubset(bank_ids):
            raise ValueError("active paired snapshots do not derive from the frozen bank")
        certified_ids = {
            certificate.subject_id
            for certificate in certificates
            if production_certificate_compatible(
                certificate, manifest=manifest, subject_id=certificate.subject_id
            )
        }
        if not active_ids.issubset(certified_ids):
            raise ValueError("test-static active bank lacks compatible utility certificates")
        if any(
            certificate.subject_id in active_ids
            and (
                not production_certificate_compatible(
                    certificate, manifest=manifest, subject_id=certificate.subject_id
                )
            )
            for certificate in certificates
        ):
            raise ValueError("test-static certificate bundle is incompatible with manifest freeze")
