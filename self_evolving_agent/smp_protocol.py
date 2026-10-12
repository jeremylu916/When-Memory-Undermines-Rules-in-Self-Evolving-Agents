"""OpenMemGov v4 protocol identity, exclusions, and fail-closed errors."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Sequence

from self_evolving_agent.smp_artifacts import file_hash
from self_evolving_agent.smp_contracts import canonical_hash


PROTOCOL_VERSION = "openmemgov.protocol.v4"
MAX_DIAGNOSTIC_TASKS = 20
MAX_DIAGNOSTIC_COST_USD = 25.0
MANIFEST_SCHEMA_VERSION = "openmemgov.diagnostic_manifest.v4"
CERTIFICATE_SCHEMA_VERSION = "openmemgov.protocol_certificate.v4"


def resolve_mini_swe_root(repo_root: str | Path, environment: Mapping[str, str] | None = None) -> Path:
    root = Path(repo_root).resolve()
    values = os.environ if environment is None else environment
    raw = values["SMP_MINISWE_ROOT"] if "SMP_MINISWE_ROOT" in values else str(root / "third_party" / "mini-swe-agent")
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def resolve_project_python(repo_root: str | Path, environment: Mapping[str, str] | None = None) -> Path:
    root = Path(repo_root).resolve()
    values = os.environ if environment is None else environment
    raw = values["SMP_PROJECT_PYTHON"] if "SMP_PROJECT_PYTHON" in values else str(root / ".miniconda3" / "envs" / "evo" / "bin" / "python")
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def validate_runtime_environment_bindings(
    bundle: "RuntimeBundle",
    repo_root: str | Path,
    environment: Mapping[str, str] | None = None,
    output_root: str | Path | None = None,
) -> None:
    root = Path(repo_root).resolve()
    expected_mini = bundle.model_budget.get("mini_swe_root")
    expected_python = bundle.model_budget.get("project_python")
    expected_config = bundle.model_budget.get("miniswe_global_config_dir")
    if (
        not isinstance(expected_mini, str)
        or not isinstance(expected_python, str)
        or not isinstance(expected_config, str)
        or not Path(expected_config).is_absolute()
    ):
        raise FrameworkInvariantError("runtime_external_path_binding_missing")
    expected_mini_path = (Path(expected_mini) if Path(expected_mini).is_absolute() else root / expected_mini).resolve()
    expected_python_path = (Path(expected_python) if Path(expected_python).is_absolute() else root / expected_python).resolve()
    expected_config_path = (Path(expected_config) if Path(expected_config).is_absolute() else root / expected_config).resolve()
    if resolve_mini_swe_root(root, environment) != expected_mini_path:
        raise FrameworkInvariantError("runtime_mini_swe_root_mismatch")
    if resolve_project_python(root, environment) != expected_python_path:
        raise FrameworkInvariantError("runtime_project_python_mismatch")
    values = os.environ if environment is None else environment
    actual_raw = values["MSWEA_GLOBAL_CONFIG_DIR"] if "MSWEA_GLOBAL_CONFIG_DIR" in values else ""
    marker_raw = values["SMP_FROZEN_GLOBAL_CONFIG_DIR"] if "SMP_FROZEN_GLOBAL_CONFIG_DIR" in values else ""
    if not actual_raw or not marker_raw:
        raise FrameworkInvariantError("runtime_global_config_binding_missing")
    actual_path = Path(actual_raw).expanduser().resolve()
    marker_path = Path(marker_raw).expanduser().resolve()
    if actual_path != expected_config_path or marker_path != expected_config_path:
        raise FrameworkInvariantError("runtime_global_config_dir_mismatch")
    if actual_path == root or actual_path.is_relative_to(root):
        raise FrameworkInvariantError("runtime_global_config_inside_checkout")
    if values.get("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT") != "1":
        raise FrameworkInvariantError("runtime_solver_retry_environment_mismatch")
    if bundle.model_budget.get("solver_retry_attempts") != 1:
        raise FrameworkInvariantError("runtime_solver_retry_binding_missing")
    if output_root is not None:
        output = Path(output_root).expanduser().resolve()
        if actual_path == output or not actual_path.is_relative_to(output):
            raise FrameworkInvariantError("runtime_global_config_outside_output_root")


def require_external_output_paths(repo_root: str | Path, **paths: str | Path) -> None:
    """Keep every mutable v4 artifact outside the frozen execution checkout."""
    root = Path(repo_root).resolve()
    for label, raw_path in paths.items():
        if not raw_path:
            raise FrameworkInvariantError("v4_external_output_path_missing", output=label)
        candidate = Path(raw_path).expanduser().resolve()
        if candidate == root or candidate.is_relative_to(root):
            raise FrameworkInvariantError(
                "v4_output_inside_frozen_checkout",
                output=label,
                path=str(candidate),
            )


def validate_runtime_cli_bindings(
    bundle: "RuntimeBundle",
    repo_root: str | Path,
    *,
    candidate_prompt: str | Path | None = None,
    signature_prompt: str | Path | None = None,
    solver_prompt: str | None = None,
    solver_cost_limit: float | None = None,
    mini_config_paths: Sequence[str | Path] | None = None,
    model_config_path: str | Path | None = None,
    model_parameters: Mapping[str, Any] | None = None,
    dataset: str | None = None,
    dataset_split: str | None = None,
    task_images: Mapping[str, str] | None = None,
) -> None:
    """Reject every runtime substitution not exactly named and hashed by the bundle."""
    root = Path(repo_root).resolve()
    metadata = bundle.model_budget
    frozen_clients = {
        "solver_client_type": "minisweagent_litellm_chat_completions",
        "solver_deployment": "DeepSeek-V4-Pro",
        "solver_endpoint": "https://yuexing-4772-resource.services.ai.azure.com/openai/v1",
        "extractor_client_type": "openai_chat_completions",
        "extractor_deployment": "DeepSeek-V4-Pro",
        "extractor_endpoint": "https://yuexing-4772-resource.services.ai.azure.com/openai/v1",
    }
    if any(metadata.get(name) != value for name, value in frozen_clients.items()):
        raise FrameworkInvariantError("runtime_deepseek_client_binding_mismatch")
    extractor_pricing = metadata.get("extractor_pricing")
    solver_pricing = metadata.get("solver_pricing")
    if not isinstance(extractor_pricing, Mapping) or not isinstance(solver_pricing, Mapping):
        raise FrameworkInvariantError("runtime_frozen_pricing_missing")
    for pricing, maximum_key in ((extractor_pricing, "maximum_call_cost"), (solver_pricing, "maximum_task_cost")):
        required_prices = (maximum_key, "input_per_million", "cached_input_per_million", "output_per_million")
        artifact = pricing.get("provenance_artifact")
        price_payload = {name: pricing.get(name) for name in required_prices}
        try:
            observed_provenance_hash = file_hash(root / str(artifact)) if artifact else ""
        except OSError:
            observed_provenance_hash = ""
        if (
            any(
                isinstance(pricing.get(name), bool)
                or not isinstance(pricing.get(name), (int, float))
                or not math.isfinite(float(pricing[name]))
                for name in required_prices
            )
            or pricing[maximum_key] <= 0
            or pricing["input_per_million"] <= 0
            or pricing["cached_input_per_million"] < 0
            or pricing["output_per_million"] <= 0
            or not str(pricing.get("provenance_hash", "")).startswith("sha256:")
            or not isinstance(artifact, str) or not artifact
            or bundle.execution_files.get(artifact) != pricing.get("provenance_hash")
            or observed_provenance_hash != pricing.get("provenance_hash")
            or pricing.get("pricing_payload_hash") != canonical_hash(price_payload)
        ):
            raise FrameworkInvariantError("runtime_frozen_pricing_invalid")
    if metadata.get("extractor_client_params") != {"timeout": 60.0, "max_retries": 0}:
        raise FrameworkInvariantError("runtime_extractor_client_params_mismatch")
    if metadata.get("solver_client_params") != {"base_url": "https://yuexing-4772-resource.services.ai.azure.com/openai/v1"}:
        raise FrameworkInvariantError("runtime_solver_client_params_mismatch")
    extractor_max_tokens = metadata.get("extractor_max_completion_tokens")
    solver_max_tokens = metadata.get("solver_max_completion_tokens")
    if (
        isinstance(extractor_max_tokens, bool) or not isinstance(extractor_max_tokens, int) or extractor_max_tokens <= 0
        or isinstance(solver_max_tokens, bool) or not isinstance(solver_max_tokens, int) or solver_max_tokens <= 0
        or extractor_max_tokens * float(extractor_pricing["output_per_million"]) / 1_000_000 > float(extractor_pricing["maximum_call_cost"]) + 1e-12
        or solver_max_tokens * float(solver_pricing["output_per_million"]) / 1_000_000 > float(solver_pricing["maximum_task_cost"]) + 1e-12
    ):
        raise FrameworkInvariantError("runtime_completion_token_bound_invalid")
    if any(not str(metadata.get(name, "")).startswith("sha256:") for name in ("extractor_retry_policy_hash", "solver_retry_policy_hash")):
        raise FrameworkInvariantError("runtime_retry_policy_binding_missing")
    if metadata.get("bundle_stage") == "pre_probe":
        if metadata.get("extractor_response_format_mode") not in {None, ""} or not str(metadata.get("capability_probe_plan_hash", "")).startswith("sha256:"):
            raise FrameworkInvariantError("runtime_preprobe_binding_invalid")
    elif metadata.get("bundle_stage") == "final_execution":
        if metadata.get("extractor_response_format_mode") not in {"json_schema", "json_object"} or not str(metadata.get("capability_registry_hash", "")).startswith("sha256:"):
            raise FrameworkInvariantError("runtime_structured_mode_missing")
    else:
        raise FrameworkInvariantError("runtime_bundle_stage_invalid")

    def exact_file(label: str, actual: str | Path, metadata_key: str, frozen: Mapping[str, str]) -> None:
        expected_raw = metadata.get(metadata_key)
        if not isinstance(expected_raw, str) or not expected_raw:
            raise FrameworkInvariantError("runtime_cli_binding_metadata_missing", binding=label)
        expected = (root / expected_raw).resolve()
        observed = Path(actual).resolve()
        expected_key = expected_raw
        try:
            expected_hash = file_hash(expected)
            observed_hash = file_hash(observed)
        except OSError as exc:
            raise FrameworkInvariantError("runtime_cli_file_binding_mismatch", binding=label) from exc
        if observed != expected or frozen.get(expected_key) != expected_hash or observed_hash != frozen.get(expected_key):
            raise FrameworkInvariantError("runtime_cli_file_binding_mismatch", binding=label)

    if candidate_prompt is not None:
        exact_file("candidate_prompt", candidate_prompt, "candidate_prompt_file", bundle.prompts)
    if signature_prompt is not None:
        exact_file("signature_prompt", signature_prompt, "signature_prompt_file", bundle.prompts)
    # The solver prompt may be embedded by the mini-agent config, but its separately
    # frozen source file must still remain byte-identical when the bundle names one.
    if "solver_prompt_file" in metadata:
        exact_file("solver_prompt", root / str(metadata["solver_prompt_file"]), "solver_prompt_file", bundle.prompts)
    if solver_prompt is not None:
        expected_solver_hash = metadata.get("solver_prompt_hash")
        if not _is_hash(expected_solver_hash) or canonical_hash({"solver_prompt": solver_prompt}) != expected_solver_hash:
            raise FrameworkInvariantError("runtime_solver_prompt_binding_mismatch")
    if solver_cost_limit is not None:
        expected_limit = metadata.get("solver_max_cost_per_task")
        if (
            isinstance(solver_cost_limit, bool)
            or not isinstance(solver_cost_limit, (int, float))
            or isinstance(expected_limit, bool)
            or not isinstance(expected_limit, (int, float))
            or float(solver_cost_limit) != float(expected_limit)
        ):
            raise FrameworkInvariantError("runtime_solver_cost_limit_mismatch")
        if float(solver_pricing["maximum_task_cost"]) != float(expected_limit):
            raise FrameworkInvariantError("runtime_solver_pricing_limit_mismatch")
    if mini_config_paths is not None:
        expected = metadata.get("mini_config_files")
        if not isinstance(expected, (list, tuple)) or [str(Path(path).resolve()) for path in mini_config_paths] != [str((root / str(path)).resolve()) for path in expected]:
            raise FrameworkInvariantError("runtime_mini_config_binding_mismatch")
        for path in expected:
            try:
                observed_hash = file_hash(root / str(path))
            except OSError as exc:
                raise FrameworkInvariantError("runtime_mini_config_binding_mismatch") from exc
            if bundle.execution_files.get(str(path)) != observed_hash:
                raise FrameworkInvariantError("runtime_mini_config_binding_mismatch")
    if model_config_path is not None:
        exact_file("model_config", model_config_path, "model_config_file", bundle.execution_files)
    if model_parameters is not None:
        expected_parameters = metadata.get("model_parameters")
        if not isinstance(expected_parameters, Mapping) or canonical_hash(model_parameters) != canonical_hash(expected_parameters):
            raise FrameworkInvariantError("runtime_model_parameters_mismatch")
        registry_path = model_parameters.get("litellm_model_registry")
        model_kwargs = model_parameters.get("model_kwargs")
        if (
            set(model_parameters) != {"model_class", "model_name", "model_kwargs", "litellm_model_registry"}
            or model_parameters.get("model_class") != "litellm"
            or model_parameters.get("model_name") != "openai/DeepSeek-V4-Pro"
            or not isinstance(model_kwargs, Mapping)
            or model_kwargs != {
                "base_url": "https://yuexing-4772-resource.services.ai.azure.com/openai/v1",
                "max_completion_tokens": solver_max_tokens,
            }
            or not isinstance(registry_path, str)
            or not registry_path
            or metadata.get("litellm_model_registry") != registry_path
        ):
            raise FrameworkInvariantError("runtime_solver_deepseek_config_mismatch")
        resolved_registry_path = (Path(registry_path) if Path(registry_path).is_absolute() else root / registry_path).resolve()
        try:
            registry_hash = file_hash(resolved_registry_path)
            registry_payload = json.loads(resolved_registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FrameworkInvariantError("runtime_litellm_registry_invalid") from exc
        if metadata.get("litellm_model_registry_hash") != registry_hash or bundle.execution_files.get(registry_path) != registry_hash:
            raise FrameworkInvariantError("runtime_litellm_registry_binding_mismatch")
        expected_registry = {
            "DeepSeek-V4-Pro": {
                "max_tokens": solver_max_tokens,
                "input_cost_per_token": float(solver_pricing["input_per_million"]) / 1_000_000,
                "cache_read_input_token_cost": float(solver_pricing["cached_input_per_million"]) / 1_000_000,
                "output_cost_per_token": float(solver_pricing["output_per_million"]) / 1_000_000,
                "litellm_provider": "openai",
                "mode": "chat",
            }
        }
        if registry_payload != expected_registry:
            raise FrameworkInvariantError("runtime_litellm_registry_pricing_mismatch")
    if dataset is not None and dataset != metadata.get("dataset_identifier"):
        raise FrameworkInvariantError("runtime_dataset_identifier_mismatch")
    if dataset_split is not None and dataset_split != metadata.get("dataset_split"):
        raise FrameworkInvariantError("runtime_dataset_split_mismatch")
    if dataset is not None and str(bundle.external_identities.get("dataset_revision", "")) != str(metadata.get("dataset_revision", "")):
        raise FrameworkInvariantError("runtime_dataset_revision_mismatch")
    if task_images is not None:
        expected_tags = metadata.get("docker_tags")
        if not isinstance(expected_tags, Mapping) or dict(task_images) != dict(expected_tags):
            raise FrameworkInvariantError("runtime_docker_tag_binding_mismatch")
        if set(task_images) != set(bundle.external_identities["docker_image_ids"]):
            raise FrameworkInvariantError("runtime_docker_image_binding_mismatch")


class ProtocolError(Exception):
    """Base class carrying a stable, bounded machine-readable report."""

    def __init__(self, error_code: str, **details: Any):
        self.error_code = error_code
        self.details = details
        super().__init__(error_code)

    def report(self) -> dict[str, Any]:
        return {"error_code": self.error_code, **self.details}


class CandidateValidationError(ProtocolError):
    pass


class StructuredExtractionError(ProtocolError):
    pass


class FrameworkInvariantError(ProtocolError):
    pass


@dataclass(frozen=True)
class StudyEligibility:
    protocol_conformant: bool
    eligible_for_statistics: bool = False
    eligible_for_calibration: bool = False
    eligible_for_memory_bank: bool = False
    eligible_for_gate_u: bool = False

    def __post_init__(self) -> None:
        if any(
            (
                self.eligible_for_statistics,
                self.eligible_for_calibration,
                self.eligible_for_memory_bank,
                self.eligible_for_gate_u,
            )
        ):
            raise FrameworkInvariantError("diagnostic_study_eligibility_must_be_false")


@dataclass(frozen=True)
class DiagnosticExclusion:
    run_identity_hash: str = ""
    manifest_hash: str = ""
    task_seal_hash: str = ""
    official_outcome_hash: str = ""
    adaptation_artifact_hash: str = ""
    artifact_hashes: tuple[str, ...] = ()
    protocol_schema_version: str = ""
    exclusion_reason: str = ""

    def hashes(self) -> frozenset[str]:
        direct = {
            value
            for name, value in asdict(self).items()
            if name.endswith("_hash") and isinstance(value, str) and value.startswith("sha256:")
        }
        direct.update(value for value in self.artifact_hashes if _is_hash(value))
        return frozenset(direct)


@dataclass(frozen=True)
class DiagnosticExclusionRegistry:
    entries: tuple[DiagnosticExclusion, ...]
    schema_version: str = "openmemgov.diagnostic_exclusions.v1"
    registry_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "openmemgov.diagnostic_exclusions.v1":
            raise FrameworkInvariantError("unsupported_exclusion_registry_schema")
        if any(not entry.exclusion_reason or not entry.hashes() for entry in self.entries):
            raise FrameworkInvariantError("invalid_exclusion_registry_entry")
        expected = canonical_hash(self.identity_payload())
        if self.registry_hash and self.registry_hash != expected:
            raise FrameworkInvariantError("exclusion_registry_hash_mismatch")
        object.__setattr__(self, "registry_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "entries": [asdict(entry) for entry in self.entries]}

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "registry_hash": self.registry_hash}

    def reject(self, artifact: Mapping[str, Any], *, purpose: str) -> None:
        if purpose not in {"calibration", "production_ledger", "active_bank", "promotion"}:
            raise FrameworkInvariantError("unknown_import_purpose", purpose=purpose)
        artifact_hashes = _collect_hashes(artifact)
        if any(entry.hashes() & artifact_hashes for entry in self.entries):
            raise FrameworkInvariantError("diagnostic_provenance_excluded", purpose=purpose)
        if artifact.get("protocol_version") != PROTOCOL_VERSION:
            raise FrameworkInvariantError("legacy_protocol_artifact_rejected", purpose=purpose)
        certificate = artifact.get("protocol_certificate")
        eligibility = artifact.get("eligibility")
        if not isinstance(certificate, Mapping):
            raise FrameworkInvariantError("missing_protocol_certificate", purpose=purpose)
        if not isinstance(eligibility, Mapping):
            raise FrameworkInvariantError("missing_study_eligibility", purpose=purpose)
        required = {
            "calibration": "eligible_for_calibration",
            "production_ledger": "eligible_for_memory_bank",
            "active_bank": "eligible_for_memory_bank",
            "promotion": "eligible_for_gate_u",
        }[purpose]
        if eligibility.get(required) is not True:
            raise FrameworkInvariantError("artifact_ineligible_for_import", purpose=purpose)


def load_exclusion_registry(path: str | Path) -> DiagnosticExclusionRegistry:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or not isinstance(value.get("entries"), list):
        raise FrameworkInvariantError("malformed_exclusion_registry")
    return DiagnosticExclusionRegistry(
        entries=tuple(DiagnosticExclusion(**{**row, "artifact_hashes": tuple(row.get("artifact_hashes", ()))}) for row in value["entries"]),
        schema_version=str(value.get("schema_version", "")),
        registry_hash=str(value.get("registry_hash", "")),
    )


def _collect_hashes(value: Any) -> frozenset[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for child in value.values():
            found.update(_collect_hashes(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_collect_hashes(child))
    elif isinstance(value, str) and value.startswith("sha256:") and len(value) == 71:
        found.add(value)
    return frozenset(found)


@dataclass(frozen=True)
class RuntimeBundle:
    commit: str
    clean_checkout: bool
    source_tree_hash: str
    exclusion_registry_hash: str
    execution_files: Mapping[str, str]
    prompts: Mapping[str, str]
    schemas: Mapping[str, str]
    model_budget: Mapping[str, Any]
    external_identities: Mapping[str, Any]
    protocol_version: str = PROTOCOL_VERSION
    bundle_hash: str = ""

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION or not self.clean_checkout:
            raise FrameworkInvariantError("runtime_checkout_not_clean")
        if not self.commit or not self.execution_files or not self.prompts or not self.schemas:
            raise FrameworkInvariantError("runtime_bundle_incomplete")
        if not self.source_tree_hash.startswith("git-tree:") or len(self.source_tree_hash) not in {49, 73}:
            raise FrameworkInvariantError("runtime_source_tree_identity_invalid")
        if not _is_hash(self.exclusion_registry_hash):
            raise FrameworkInvariantError("runtime_exclusion_registry_unbound")
        for group in (self.execution_files, self.prompts, self.schemas):
            if any(not _is_hash(value) for value in group.values()):
                raise FrameworkInvariantError("runtime_bundle_entry_hash_invalid")
        required_external = {
            "mini_swe_revision",
            "environment_fingerprint",
            "swebench_revision",
            "evaluator_hash",
            "dataset_revision",
            "selected_record_hashes",
            "base_commits",
            "docker_image_ids",
        }
        if set(self.external_identities) != required_external:
            raise FrameworkInvariantError("runtime_external_identity_incomplete")
        if any(not self.external_identities[name] for name in required_external):
            raise FrameworkInvariantError("runtime_external_identity_unresolved")
        if not _is_hash(self.external_identities["environment_fingerprint"]):
            raise FrameworkInvariantError("runtime_environment_fingerprint_invalid")
        if not _is_hash(self.external_identities["evaluator_hash"]):
            raise FrameworkInvariantError("runtime_evaluator_hash_invalid")
        if str(self.external_identities["dataset_revision"]).lower() in {"main", "master", "latest", "test"}:
            raise FrameworkInvariantError("runtime_dataset_revision_mutable")
        images = self.external_identities["docker_image_ids"]
        if not isinstance(images, Mapping) or not images or any(
            not isinstance(value, str) or not (value.startswith("sha256:") or "@sha256:" in value)
            for value in images.values()
        ):
            raise FrameworkInvariantError("runtime_docker_image_identity_mutable")
        expected = canonical_hash(self.identity_payload())
        if self.bundle_hash and self.bundle_hash != expected:
            raise FrameworkInvariantError("runtime_bundle_hash_mismatch")
        object.__setattr__(self, "bundle_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "commit": self.commit,
            "clean_checkout": self.clean_checkout,
            "source_tree_hash": self.source_tree_hash,
            "exclusion_registry_hash": self.exclusion_registry_hash,
            "execution_files": dict(sorted(self.execution_files.items())),
            "prompts": dict(sorted(self.prompts.items())),
            "schemas": dict(sorted(self.schemas.items())),
            "model_budget": dict(self.model_budget),
            "external_identities": dict(self.external_identities),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "bundle_hash": self.bundle_hash}


def build_runtime_bundle(
    repo_root: str | Path,
    *,
    execution_files: Sequence[str],
    prompt_files: Mapping[str, str],
    schema_payloads: Mapping[str, Any],
    model_budget: Mapping[str, Any],
    external_identities: Mapping[str, Any],
    external_identity_observers: Mapping[str, Callable[[], Any]],
    exclusion_registry_hash: str,
) -> RuntimeBundle:
    root = Path(repo_root).resolve()
    commit = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    file_map = {name: file_hash(root / name) for name in execution_files}
    prompt_map = {path: file_hash(root / path) for path in prompt_files.values()}
    schemas = {name: canonical_hash(payload) for name, payload in schema_payloads.items()}
    if set(external_identity_observers) != set(external_identities):
        raise FrameworkInvariantError("runtime_external_observers_incomplete")
    observed = {name: observer() for name, observer in external_identity_observers.items()}
    if observed != dict(external_identities):
        raise FrameworkInvariantError("runtime_external_identity_mismatch")
    return RuntimeBundle(
        commit=commit,
        clean_checkout=not status,
        source_tree_hash="git-tree:" + tree,
        exclusion_registry_hash=exclusion_registry_hash,
        execution_files=file_map,
        prompts=prompt_map,
        schemas=schemas,
        model_budget=model_budget,
        external_identities=observed,
    )


def recompute_runtime_bundle(
    expected: RuntimeBundle,
    repo_root: str | Path,
    *,
    schema_payloads: Mapping[str, Any],
    exclusion_registry_hash: str,
) -> RuntimeBundle:
    """Reobserve every bundle input from the execution checkout."""

    root = Path(repo_root).resolve()
    observed_external = _observe_external_identities(root, expected.model_budget)
    observed = build_runtime_bundle(
        root,
        execution_files=tuple(expected.execution_files),
        prompt_files={path: path for path in expected.prompts},
        schema_payloads=schema_payloads,
        model_budget=expected.model_budget,
        external_identities=expected.external_identities,
        external_identity_observers={name: (lambda name=name: observed_external[name]) for name in expected.external_identities},
        exclusion_registry_hash=exclusion_registry_hash,
    )
    if observed.to_dict() != expected.to_dict():
        raise FrameworkInvariantError("runtime_bundle_recomputation_mismatch")
    return observed


def build_observed_runtime_bundle(
    repo_root: str | Path,
    *,
    execution_files: Sequence[str],
    prompt_files: Mapping[str, str],
    schema_payloads: Mapping[str, Any],
    model_budget: Mapping[str, Any],
    exclusion_registry_hash: str,
) -> RuntimeBundle:
    root = Path(repo_root).resolve()
    observed = _observe_external_identities(root, model_budget)
    return build_runtime_bundle(
        root,
        execution_files=execution_files,
        prompt_files=prompt_files,
        schema_payloads=schema_payloads,
        model_budget=model_budget,
        external_identities=observed,
        external_identity_observers={name: (lambda name=name: observed[name]) for name in observed},
        exclusion_registry_hash=exclusion_registry_hash,
    )


def _observe_external_identities(root: Path, metadata: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "mini_swe_root",
        "environment_files",
        "swebench_python",
        "evaluator_file",
        "dataset_metadata_file",
        "selected_targets_file",
        "docker_tags",
    }
    if not required.issubset(metadata):
        raise FrameworkInvariantError("runtime_external_observer_metadata_incomplete")
    environment_files = [str(path) for path in metadata["environment_files"]]
    environment_fingerprint = canonical_hash({path: file_hash(root / path) for path in environment_files})
    dataset = json.loads((root / str(metadata["dataset_metadata_file"])).read_text(encoding="utf-8"))
    targets = json.loads((root / str(metadata["selected_targets_file"])).read_text(encoding="utf-8"))
    if not isinstance(dataset, Mapping) or not isinstance(targets, list):
        raise FrameworkInvariantError("runtime_dataset_observer_artifact_invalid")
    tags = metadata["docker_tags"]
    if not isinstance(tags, Mapping) or not tags:
        raise FrameworkInvariantError("runtime_docker_observer_metadata_invalid")
    docker_ids = {}
    for task_id, tag in tags.items():
        try:
            value = subprocess.run(
                ["docker", "image", "inspect", str(tag), "--format", "{{.Id}}"],
                check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FrameworkInvariantError("runtime_docker_image_identity_unavailable", task_id=str(task_id)) from exc
        docker_ids[str(task_id)] = value
    mini_root = root / str(metadata["mini_swe_root"])
    mini_identity = {
        "commit": _git(mini_root, "rev-parse", "HEAD"),
        "tree": _git(mini_root, "rev-parse", "HEAD^{tree}"),
        "clean": not _git(mini_root, "status", "--porcelain"),
    }
    swebench_identity = _python_distribution_identity(str(metadata["swebench_python"]), "swebench")
    return {
        "mini_swe_revision": mini_identity,
        "environment_fingerprint": environment_fingerprint,
        "swebench_revision": swebench_identity,
        "evaluator_hash": file_hash(root / str(metadata["evaluator_file"])),
        "dataset_revision": str(dataset.get("revision", "")),
        "selected_record_hashes": [canonical_hash(row) for row in targets],
        "base_commits": [str(row.get("base_commit", "")) for row in targets],
        "docker_image_ids": docker_ids,
    }


def _python_distribution_identity(python: str, distribution: str) -> dict[str, str]:
    script = (
        "import importlib.metadata as m,json,pathlib,hashlib;"
        f"d=m.distribution({distribution!r});"
        "files=sorted(str(p) for p in (d.files or []) if str(p).endswith(('.py','.json','.txt')));"
        "h=hashlib.sha256();"
        "[(h.update(p.encode()),h.update(pathlib.Path(d.locate_file(p)).read_bytes())) for p in files if pathlib.Path(d.locate_file(p)).is_file()];"
        "print(json.dumps({'version':d.version,'package_hash':'sha256:'+h.hexdigest()},sort_keys=True))"
    )
    try:
        output = subprocess.run(
            [python, "-c", script], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).stdout
        value = json.loads(output)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise FrameworkInvariantError("runtime_swebench_distribution_identity_unavailable") from exc
    if not isinstance(value, Mapping) or not value.get("version") or not _is_hash(value.get("package_hash")):
        raise FrameworkInvariantError("runtime_swebench_distribution_identity_invalid")
    return dict(value)


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FrameworkInvariantError("runtime_git_identity_unavailable") from exc


@dataclass(frozen=True)
class ProtocolCertificate:
    manifest_hash: str
    runtime_bundle_hash: str
    commit: str
    phase: str
    selected_target_manifest_hash: str
    preflight_report_hash: str
    issued_protocol_version: str = PROTOCOL_VERSION
    schema_version: str = CERTIFICATE_SCHEMA_VERSION
    certificate_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != CERTIFICATE_SCHEMA_VERSION or self.issued_protocol_version != PROTOCOL_VERSION:
            raise FrameworkInvariantError("unsupported_protocol_certificate")
        if any(not _is_hash(value) for value in (
            self.manifest_hash,
            self.runtime_bundle_hash,
            self.selected_target_manifest_hash,
            self.preflight_report_hash,
        )):
            raise FrameworkInvariantError("protocol_certificate_binding_invalid")
        expected = canonical_hash(self.identity_payload())
        if self.certificate_hash and self.certificate_hash != expected:
            raise FrameworkInvariantError("protocol_certificate_hash_mismatch")
        object.__setattr__(self, "certificate_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key != "certificate_hash"}

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "certificate_hash": self.certificate_hash}


def revalidate_protocol_certificate(
    certificate: ProtocolCertificate,
    *,
    manifest_hash: str,
    bundle: RuntimeBundle,
    phase: str,
    selected_target_manifest_hash: str,
) -> None:
    if certificate.certificate_hash != canonical_hash(certificate.identity_payload()):
        raise FrameworkInvariantError("protocol_certificate_hash_mismatch")
    if (
        certificate.manifest_hash != manifest_hash
        or certificate.runtime_bundle_hash != bundle.bundle_hash
        or certificate.commit != bundle.commit
        or certificate.phase != phase
        or certificate.selected_target_manifest_hash != selected_target_manifest_hash
    ):
        raise FrameworkInvariantError("protocol_certificate_context_mismatch")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71


@dataclass(frozen=True)
class V4DiagnosticManifest:
    manifest_id: str
    smp_manifest_hash: str
    runtime_bundle_hash: str
    selected_target_manifest_hash: str
    selection_rule: str
    maximum_tasks: int
    aggregate_cost_cap_usd: float
    semantic_attempts_per_task: int
    transport_retry_policy_hash: str
    diagnostic_ledger_schema: str
    eligibility: StudyEligibility
    protocol_version: str = PROTOCOL_VERSION
    schema_version: str = MANIFEST_SCHEMA_VERSION
    manifest_hash: str = ""

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION or self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise FrameworkInvariantError("legacy_manifest_rejected")
        if (
            isinstance(self.maximum_tasks, bool)
            or not isinstance(self.maximum_tasks, int)
            or not 1 <= self.maximum_tasks <= MAX_DIAGNOSTIC_TASKS
            or self.semantic_attempts_per_task != 1
        ):
            raise FrameworkInvariantError("diagnostic_attempt_policy_invalid")
        if (
            isinstance(self.aggregate_cost_cap_usd, bool)
            or not isinstance(self.aggregate_cost_cap_usd, (int, float))
            or not math.isfinite(float(self.aggregate_cost_cap_usd))
            or not 0 < self.aggregate_cost_cap_usd <= MAX_DIAGNOSTIC_COST_USD
        ):
            raise FrameworkInvariantError("diagnostic_cost_cap_invalid")
        if self.diagnostic_ledger_schema != "openmemgov.disposable_diagnostic_ledger.v4":
            raise FrameworkInvariantError("diagnostic_ledger_policy_invalid")
        if not all(_is_hash(value) for value in (
            self.smp_manifest_hash,
            self.runtime_bundle_hash,
            self.selected_target_manifest_hash,
            self.transport_retry_policy_hash,
        )):
            raise FrameworkInvariantError("diagnostic_manifest_binding_invalid")
        expected = canonical_hash(self.identity_payload())
        if self.manifest_hash and self.manifest_hash != expected:
            raise FrameworkInvariantError("diagnostic_manifest_hash_mismatch")
        object.__setattr__(self, "manifest_hash", expected)

    def identity_payload(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("manifest_hash")
        return row

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "manifest_hash": self.manifest_hash}


def load_v4_diagnostic_manifest(path: str | Path) -> V4DiagnosticManifest:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise FrameworkInvariantError("diagnostic_manifest_not_object")
    row = dict(value)
    eligibility = row.get("eligibility")
    if not isinstance(eligibility, Mapping):
        raise FrameworkInvariantError("missing_study_eligibility")
    row["eligibility"] = StudyEligibility(**eligibility)
    return V4DiagnosticManifest(**row)


def runtime_bundle_from_dict(value: Mapping[str, Any]) -> RuntimeBundle:
    return RuntimeBundle(**dict(value))


def load_runtime_bundle(path: str | Path) -> RuntimeBundle:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise FrameworkInvariantError("runtime_bundle_not_object")
    return runtime_bundle_from_dict(value)


def protocol_certificate_from_dict(value: Mapping[str, Any]) -> ProtocolCertificate:
    return ProtocolCertificate(**dict(value))


def load_protocol_certificate(path: str | Path) -> ProtocolCertificate:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise FrameworkInvariantError("protocol_certificate_not_object")
    return protocol_certificate_from_dict(value)
