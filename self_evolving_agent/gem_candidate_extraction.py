"""Conservative GEM candidate extraction from mini-SWE trajectories.

The extractor intentionally uses no LLM and stores only public, low-granularity
evidence: trajectory/prediction references plus patch fingerprints. Raw patches,
tool logs, official outputs, hidden tests, and reference patches are never copied
into the candidate payload.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from self_evolving_agent.gem_memory_candidate import (
    build_gem_memory_candidate,
    validate_gem_memory_candidate,
)


GEM_CANDIDATE_EXTRACTION_SCHEMA_VERSION = "gem.candidate_extraction.v1"

FORBIDDEN_LEAKAGE_MARKERS = (
    "gold_patch",
    "gold diff",
    "reference_patch",
    "reference patch",
    "hidden_test",
    "hidden test",
    "hidden_assertion",
    "official_stdout",
    "official stderr",
    "official_stderr",
    "official stdout",
    "official_hidden_output",
    "fail_to_pass",
    "pass_to_pass",
)

SECRET_PATTERNS = (
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|bearer|token|password|secret)"
            r"(\s*[:=]\s*)([^\s,'\"]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"), "gh_[REDACTED]"),
)


class GemCandidateExtractionError(ValueError):
    """Raised when trajectory extraction would consume unsafe evidence."""


def extract_gem_candidates_from_trajectory(
    trajectory_path: str | Path,
    *,
    prediction_metadata: dict[str, Any] | str | Path | None = None,
    task_context: dict[str, Any] | None = None,
    protocol_id: str = "",
    run_id: str = "",
    pre_patch_ref: str | None = None,
    post_patch_ref: str | None = None,
    created_at: str | None = None,
) -> list[dict[str, Any]]:
    """Read one mini-SWE trajectory JSON and emit 0..N GEM candidates."""

    source_path = Path(trajectory_path)
    trajectory = _read_json(source_path)
    prediction = _load_prediction_metadata(prediction_metadata)
    return extract_gem_candidates_from_payload(
        trajectory,
        trajectory_ref=str(source_path),
        prediction_metadata=prediction,
        task_context=task_context,
        protocol_id=protocol_id,
        run_id=run_id,
        pre_patch_ref=pre_patch_ref,
        post_patch_ref=post_patch_ref,
        created_at=created_at,
    )


def extract_gem_candidates_from_payload(
    trajectory: dict[str, Any],
    *,
    trajectory_ref: str,
    prediction_metadata: dict[str, Any] | None = None,
    task_context: dict[str, Any] | None = None,
    protocol_id: str = "",
    run_id: str = "",
    pre_patch_ref: str | None = None,
    post_patch_ref: str | None = None,
    created_at: str | None = None,
) -> list[dict[str, Any]]:
    """Extract candidates from an already-loaded trajectory payload."""

    if not isinstance(trajectory, dict):
        raise GemCandidateExtractionError("trajectory must be a mapping")
    prediction = prediction_metadata if isinstance(prediction_metadata, dict) else {}
    context = task_context if isinstance(task_context, dict) else {}
    _assert_no_leakage_markers(
        {
            "trajectory": _public_scan_view(trajectory),
            "prediction_metadata": _public_scan_view(prediction),
            "task_context": _public_scan_view(context),
        }
    )

    patch_text = _submitted_patch_text(trajectory, prediction)
    patch_hash = _patch_hash(trajectory, prediction, patch_text)
    if not patch_text.strip() and not patch_hash:
        return []

    task_id = _task_id(trajectory, prediction, context)
    repo = _repo(trajectory, prediction, context)
    benchmark_id = _benchmark_id(trajectory, prediction, context)
    trajectory_id = _trajectory_id(trajectory, trajectory_ref)
    exit_status = _exit_status(trajectory)
    patch_chars = len(patch_text)
    refs = _candidate_refs(
        trajectory_ref=trajectory_ref,
        prediction_metadata=prediction,
        patch_hash=patch_hash,
    )
    pre_ref = (
        pre_patch_ref
        or _string(prediction.get("pre_patch_ref"))
        or _string(trajectory.get("pre_patch_ref"))
        or f"{trajectory_id}:pre"
    )
    post_ref = (
        post_patch_ref
        or _string(prediction.get("post_patch_ref"))
        or _string(trajectory.get("post_patch_ref"))
        or f"{trajectory_id}:post:{patch_hash}"
    )
    candidate = build_gem_memory_candidate(
        task_id=task_id,
        repo=repo,
        benchmark_id=benchmark_id,
        trajectory_id=trajectory_id,
        pre_patch_ref=pre_ref,
        post_patch_ref=post_ref,
        claim=(
            "A mini-SWE trajectory produced a non-empty patch that may encode a "
            "reusable repair lesson; admit only after executable fail-before/"
            "pass-after evidence."
        ),
        failure_signature={
            "type": "public_trajectory_patch_submission",
            "observable": (
                f"mini-SWE trajectory exited with {exit_status or 'unknown'} and "
                f"submitted patch {patch_hash} ({patch_chars} chars)."
            ),
        },
        intervention={
            "summary": (
                "Submitted a non-empty patch in a mini-SWE trajectory; GEM must "
                "verify the claimed repair with an oracle before reuse."
            ),
            "patch_hash": patch_hash,
            "patch_chars": patch_chars,
        },
        applicability={
            "repo": repo,
            "benchmark_id": benchmark_id,
            "scope": "same task/repo failure family until admitted by oracle evidence",
            "issue_type_tags": _string_list(context.get("issue_type_tags", [])),
        },
        exclusion={
            "rule": (
                "Do not admit, retrieve, or promote this candidate without "
                "fail-before/pass-after oracle evidence."
            ),
            "blocked_inputs": [
                "private benchmark-only checks",
                "solution patch artifacts",
                "external reference fixes",
                "official non-public outputs",
            ],
        },
        evidence_refs=refs,
        oracle_refs=_string_list(prediction.get("oracle_refs", [])),
        provenance={
            "schema_version": GEM_CANDIDATE_EXTRACTION_SCHEMA_VERSION,
            "protocol_id": str(protocol_id or _string(context.get("protocol_id"))),
            "run_id": str(run_id or _string(context.get("run_id"))),
            "task_id": task_id,
            "repo": repo,
            "trajectory_path": trajectory_ref,
            "prediction_path": _string(prediction.get("prediction_path")),
            "candidate_source": "mini_swe_public_trajectory",
            "extractor": "conservative_patch_submission_v1",
            "created_from_public_artifacts_only": True,
        },
        created_at=created_at,
    )
    validate_gem_memory_candidate(candidate)
    return [candidate]


def _submitted_patch_text(
    trajectory: dict[str, Any],
    prediction: dict[str, Any],
) -> str:
    for key in ("model_patch", "patch", "patch_diff", "submission"):
        value = prediction.get(key)
        if isinstance(value, str) and value.strip():
            return _redact_text(value)
    info = trajectory.get("info")
    if isinstance(info, dict):
        value = info.get("submission")
        if isinstance(value, str) and value.strip():
            return _redact_text(value)
    for key in ("submission", "patch_diff", "model_patch"):
        value = trajectory.get(key)
        if isinstance(value, str) and value.strip():
            return _redact_text(value)
    return ""


def _patch_hash(
    trajectory: dict[str, Any],
    prediction: dict[str, Any],
    patch_text: str,
) -> str:
    for payload in (prediction, trajectory):
        value = payload.get("patch_hash") if isinstance(payload, dict) else None
        if isinstance(value, str) and value.strip():
            return value if value.startswith("sha256:") else f"sha256:{value}"
    info = trajectory.get("info")
    if isinstance(info, dict):
        value = info.get("patch_hash")
        if isinstance(value, str) and value.strip():
            return value if value.startswith("sha256:") else f"sha256:{value}"
    if patch_text.strip():
        digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"
    return ""


def _candidate_refs(
    *,
    trajectory_ref: str,
    prediction_metadata: dict[str, Any],
    patch_hash: str,
) -> list[str]:
    refs = [f"trajectory:{trajectory_ref}"]
    prediction_path = _string(prediction_metadata.get("prediction_path"))
    if prediction_path:
        refs.append(f"prediction:{prediction_path}")
    if patch_hash:
        refs.append(f"patch_hash:{patch_hash}")
    return refs


def _task_id(
    trajectory: dict[str, Any],
    prediction: dict[str, Any],
    context: dict[str, Any],
) -> str:
    for payload in (context, prediction, trajectory):
        value = _first_present(payload, "instance_id", "task_id", "id")
        if value:
            return value
    info = trajectory.get("info")
    if isinstance(info, dict):
        value = _first_present(info, "instance_id", "task_id", "id")
        if value:
            return value
    return "unknown_task"


def _repo(
    trajectory: dict[str, Any],
    prediction: dict[str, Any],
    context: dict[str, Any],
) -> str:
    for payload in (context, prediction, trajectory):
        value = _first_present(payload, "repository", "repo", "repo_id")
        if value:
            return value
    return "unknown/unknown"


def _benchmark_id(
    trajectory: dict[str, Any],
    prediction: dict[str, Any],
    context: dict[str, Any],
) -> str:
    for payload in (context, prediction, trajectory):
        value = _first_present(payload, "benchmark_id", "benchmark", "dataset")
        if value:
            return value
    return "SWE-bench Verified"


def _trajectory_id(trajectory: dict[str, Any], trajectory_ref: str) -> str:
    for payload in (trajectory.get("info"), trajectory):
        if isinstance(payload, dict):
            value = _first_present(payload, "trajectory_id", "run_id", "id")
            if value:
                return f"traj_{_stable_slug(value)}"
    digest = hashlib.sha256(
        json.dumps(_jsonable(trajectory), sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    stem = Path(str(trajectory_ref)).stem or "trajectory"
    return f"traj_{_stable_slug(stem)}_{digest[:12]}"


def _exit_status(trajectory: dict[str, Any]) -> str:
    info = trajectory.get("info")
    if isinstance(info, dict) and info.get("exit_status"):
        return _string(info.get("exit_status"))
    value = trajectory.get("exit_status")
    return _string(value) if value else ""


def _load_prediction_metadata(
    value: dict[str, Any] | str | Path | None,
) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return deepcopy(value)
    path = Path(value)
    payload = _read_json_or_jsonl(path)
    if isinstance(payload, list):
        if not payload:
            return {}
        if not isinstance(payload[0], dict):
            raise GemCandidateExtractionError("prediction JSONL entries must be mappings")
        result = dict(payload[0])
    elif isinstance(payload, dict):
        result = _normalize_prediction_mapping(payload)
    else:
        raise GemCandidateExtractionError("prediction metadata must be a mapping")
    result.setdefault("prediction_path", str(path))
    return result


def _normalize_prediction_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    if _looks_like_prediction(payload):
        return dict(payload)
    if len(payload) == 1:
        prediction_key, prediction_value = next(iter(payload.items()))
        if isinstance(prediction_value, dict) and _looks_like_prediction(prediction_value):
            result = dict(prediction_value)
            result.setdefault("instance_id", str(prediction_key))
            result["prediction_key"] = str(prediction_key)
            return result
    return dict(payload)


def _looks_like_prediction(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("model_patch", "patch", "patch_diff", "submission"))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GemCandidateExtractionError(f"expected JSON object: {path}")
    return payload


def _read_json_or_jsonl(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def _assert_no_leakage_markers(value: Any) -> None:
    encoded = json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=True).lower()
    normalized = encoded.replace("-", "_")
    for marker in FORBIDDEN_LEAKAGE_MARKERS:
        if marker.lower() in normalized:
            raise GemCandidateExtractionError(f"forbidden leakage marker: {marker}")


def _public_scan_view(value: Any) -> Any:
    """Return a compact scan payload to catch explicit forbidden markers."""

    return _jsonable(value)


def _first_present(payload: Any, *keys: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value):
            return str(value)
    return ""


def _string(value: Any) -> str:
    if value is None:
        return ""
    return _redact_text(str(value))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        value = [value]
    return [_redact_text(str(item)) for item in value if str(item)]


def _redact_text(text: str) -> str:
    redacted = str(text)
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _stable_slug(value: Any) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")
    return slug or "unknown"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
