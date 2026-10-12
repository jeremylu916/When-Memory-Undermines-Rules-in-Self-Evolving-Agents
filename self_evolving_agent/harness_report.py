"""Parse official SWE-bench harness reports and map them back to EvaluationScore."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from self_evolving_agent.benchmarks import BenchmarkTask
from self_evolving_agent.scoring import EvaluationScore, score_records


def score_harness_report(
    *,
    tasks: list[BenchmarkTask],
    aggregate_report_path: str | Path,
    detail_report_root: str | Path | None = None,
    aggregate_metric: str = "task_success_rate",
) -> EvaluationScore:
    """Return an EvaluationScore computed from official SWE-bench harness output."""
    aggregate_report = load_aggregate_report(aggregate_report_path)
    detail_reports = (
        load_detail_reports(detail_report_root) if detail_report_root is not None else {}
    )
    records = build_records_from_harness_reports(
        tasks=tasks,
        aggregate_report=aggregate_report,
        detail_reports=detail_reports,
    )
    return score_records(records, aggregate_metric=aggregate_metric)


def load_aggregate_report(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "resolved_ids" not in data or "unresolved_ids" not in data or "error_ids" not in data:
        raise ValueError(f"Not a valid SWE-bench aggregate report: {path}")
    return data


def load_detail_reports(root: str | Path) -> dict[str, dict[str, Any]]:
    """Load per-instance report.json files from a harness log directory."""
    detail_reports: dict[str, dict[str, Any]] = {}
    for path in Path(root).rglob("report.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if _looks_like_aggregate_report(payload):
            continue
        if not isinstance(payload, dict):
            continue
        for instance_id, report in payload.items():
            if isinstance(report, dict) and "resolved" in report:
                detail_reports[instance_id] = report
    return detail_reports


def load_prediction_records(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def recover_aggregate_report_from_detail_reports(
    *,
    predictions_path: str | Path,
    detail_report_root: str | Path,
    task_ids: list[str],
    output_path: str | Path,
    recovery_reason: str,
) -> Path:
    """Strictly recover an aggregate report after a report-stage failure.

    Recovery is allowed only when every non-empty submitted prediction has a
    well-formed per-instance report.  Empty patches need no container report.
    Missing or malformed reports cause a hard failure so partial harness runs
    can never be misreported as completed evaluations.
    """

    predictions = load_prediction_records(predictions_path)
    predictions_by_id = {
        str(record.get("instance_id")): record
        for record in predictions
        if isinstance(record, dict) and record.get("instance_id") is not None
    }
    if not predictions_by_id:
        raise RuntimeError(f"no predictions found at {predictions_path}")
    missing_predictions = sorted(set(task_ids) - set(predictions_by_id))
    if missing_predictions:
        raise RuntimeError(
            "missing predictions for tasks: " + ", ".join(missing_predictions)
        )

    details = load_detail_reports(detail_report_root)
    completed_ids: list[str] = []
    resolved_ids: list[str] = []
    unresolved_ids: list[str] = []
    empty_patch_ids: list[str] = []
    missing_report_ids: list[str] = []
    malformed_report_ids: list[str] = []
    for task_id in task_ids:
        prediction = predictions_by_id[task_id]
        if not str(prediction.get("model_patch") or "").strip():
            empty_patch_ids.append(task_id)
            continue
        detail = details.get(task_id)
        if detail is None:
            missing_report_ids.append(task_id)
            continue
        resolved = detail.get("resolved")
        if resolved is True:
            resolved_ids.append(task_id)
        elif resolved is False:
            unresolved_ids.append(task_id)
        else:
            malformed_report_ids.append(task_id)
            continue
        completed_ids.append(task_id)

    if missing_report_ids or malformed_report_ids:
        problems: list[str] = []
        if missing_report_ids:
            problems.append(
                "missing report.json for non-empty predictions: "
                + ", ".join(sorted(missing_report_ids))
            )
        if malformed_report_ids:
            problems.append(
                "malformed report.json missing resolved status: "
                + ", ".join(sorted(malformed_report_ids))
            )
        raise RuntimeError("; ".join(problems))

    aggregate = {
        "total_instances": len(task_ids),
        "submitted_instances": len(predictions_by_id),
        "completed_instances": len(completed_ids),
        "resolved_instances": len(resolved_ids),
        "unresolved_instances": len(unresolved_ids),
        "empty_patch_instances": len(empty_patch_ids),
        "error_instances": 0,
        "completed_ids": sorted(completed_ids),
        "incomplete_ids": [],
        "empty_patch_ids": sorted(empty_patch_ids),
        "submitted_ids": sorted(predictions_by_id),
        "resolved_ids": sorted(resolved_ids),
        "unresolved_ids": sorted(unresolved_ids),
        "error_ids": [],
        "schema_version": 2,
        "recovered_from_detail_reports": True,
        "recovery_reason": str(recovery_reason),
    }
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(aggregate, indent=4, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return target


def build_records_from_harness_reports(
    *,
    tasks: list[BenchmarkTask],
    aggregate_report: dict[str, Any],
    detail_reports: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    detail_reports = detail_reports or {}

    resolved_ids = set(aggregate_report.get("resolved_ids", []))
    unresolved_ids = set(aggregate_report.get("unresolved_ids", []))
    error_ids = set(aggregate_report.get("error_ids", []))
    incomplete_ids = set(aggregate_report.get("incomplete_ids", []))
    empty_patch_ids = set(aggregate_report.get("empty_patch_ids", []))

    records: list[dict[str, Any]] = []
    for task in tasks:
        task_id = task.task_id
        detail = detail_reports.get(task_id, {})
        success = task_id in resolved_ids
        execution_status = _execution_status_for_task(
            task_id=task_id,
            success=success,
            detail=detail,
            unresolved_ids=unresolved_ids,
            error_ids=error_ids,
            incomplete_ids=incomplete_ids,
            empty_patch_ids=empty_patch_ids,
        )
        test_pass = _test_pass_ratio(detail.get("tests_status"), success=success)

        official_harness = _official_harness_block(
            task_id=task_id,
            detail=detail,
            success=success,
            empty_patch_ids=empty_patch_ids,
        )

        records.append(
            {
                "task": {
                    "id": task.task_id,
                    "category": task.category,
                    "benchmark": task.benchmark,
                },
                "success": success,
                "execution_status": execution_status,
                "metrics": {"test_pass": test_pass},
                "cost": 0.0,
                "source": "official_harness",
                "harness": official_harness,
                "official_harness": official_harness,
            }
        )
    return records


def build_critic_records_from_official_evidence(
    *,
    tasks: list[BenchmarkTask],
    aggregate_report: dict[str, Any],
    detail_reports: dict[str, dict[str, Any]] | None = None,
    original_trace_records: list[dict[str, Any]] | None = None,
    prediction_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fuse original traces, predictions, and official reports for critic use.

    ``build_records_from_harness_reports`` intentionally returns thin synthetic
    records for scoring. The critic needs richer evidence: patch text, file
    edits when available, and official FAIL_TO_PASS / PASS_TO_PASS outcomes.
    This helper keeps official harness success/failure as the source of truth
    while preserving generation-time evidence from the trace/prediction files.
    """
    base_records = build_records_from_harness_reports(
        tasks=tasks,
        aggregate_report=aggregate_report,
        detail_reports=detail_reports,
    )
    traces_by_id = {
        str(record.get("task", {}).get("id")): record
        for record in (original_trace_records or [])
        if isinstance(record, dict) and record.get("task", {}).get("id") is not None
    }
    predictions_by_id = {
        str(record.get("instance_id")): record
        for record in (prediction_records or [])
        if isinstance(record, dict) and record.get("instance_id") is not None
    }

    fused: list[dict[str, Any]] = []
    for base in base_records:
        task_id = str(base["task"]["id"])
        original = traces_by_id.get(task_id)
        prediction = predictions_by_id.get(task_id, {})
        model_patch = str(prediction.get("model_patch") or "")

        record = copy.deepcopy(original) if original is not None else {}
        original_task = record.get("task", {}) if isinstance(record.get("task"), dict) else {}
        record["task"] = {**base["task"], **original_task}
        record["success"] = base["success"]
        record["execution_status"] = base["execution_status"]
        record["metrics"] = base["metrics"]
        record["cost"] = record.get("cost", 0.0)
        record["source"] = "official_harness_fused"
        record["official_harness"] = base["official_harness"]
        record["harness"] = base["official_harness"]
        record["model_patch"] = model_patch
        record["prediction"] = {
            "model_name_or_path": prediction.get("model_name_or_path"),
            "model_patch_chars": len(model_patch),
            "model_patch_present": bool(model_patch.strip()),
        }

        if not record.get("patch_diff") and model_patch.strip():
            record["patch_diff"] = model_patch
        record.setdefault("patch_diff", "")
        record.setdefault("file_edits", [])
        record.setdefault("command_results", [])
        record.setdefault("test_results", [])
        record.setdefault("intermediate_steps", [])
        record["evidence_missing"] = {
            "original_trace": original is None,
            "file_edits": original is None or "file_edits" not in original,
            "patch_diff": (
                original is None
                or not str(original.get("patch_diff") or "").strip()
            )
            and not model_patch.strip(),
        }
        fused.append(record)

    return fused


def _execution_status_for_task(
    *,
    task_id: str,
    success: bool,
    detail: dict[str, Any],
    unresolved_ids: set[str],
    error_ids: set[str],
    incomplete_ids: set[str],
    empty_patch_ids: set[str],
) -> str:
    if success:
        return "success"
    if task_id in error_ids or task_id in incomplete_ids:
        return "environment_failed"
    if task_id in empty_patch_ids:
        return "patch_failed"

    patch_exists = detail.get("patch_exists")
    patch_successfully_applied = detail.get("patch_successfully_applied")
    patch_is_none = detail.get("patch_is_None")

    if patch_is_none is True or patch_exists is False or patch_successfully_applied is False:
        return "patch_failed"
    if task_id in unresolved_ids:
        return "test_failed"
    return "patch_failed"


def _official_harness_block(
    *,
    task_id: str,
    detail: dict[str, Any],
    success: bool,
    empty_patch_ids: set[str],
) -> dict[str, Any]:
    tests_status = detail.get("tests_status", {})
    patch_exists = detail.get("patch_exists")
    patch_is_none = detail.get("patch_is_None")
    empty_patch = task_id in empty_patch_ids
    if empty_patch and patch_exists is None:
        patch_exists = False
    return {
        "resolved": detail.get("resolved", success),
        "patch_exists": patch_exists,
        "patch_is_None": patch_is_none,
        "patch_successfully_applied": detail.get(
            "patch_successfully_applied", None
        ),
        "empty_patch": empty_patch,
        "tests_status": tests_status,
        "tests_status_counts": _tests_status_counts(tests_status),
    }


def _tests_status_counts(tests_status: Any) -> dict[str, dict[str, int]]:
    if not isinstance(tests_status, dict):
        return {}
    counts: dict[str, dict[str, int]] = {}
    for name, bucket in tests_status.items():
        if not isinstance(bucket, dict):
            continue
        counts[str(name)] = {
            "success": len(bucket.get("success", []) or []),
            "failure": len(bucket.get("failure", []) or []),
        }
    return counts


def _test_pass_ratio(tests_status: Any, *, success: bool) -> float:
    if not isinstance(tests_status, dict):
        return 1.0 if success else 0.0

    successes = 0
    failures = 0
    for bucket in tests_status.values():
        if not isinstance(bucket, dict):
            continue
        successes += len(bucket.get("success", []))
        failures += len(bucket.get("failure", []))
    total = successes + failures
    if total == 0:
        return 1.0 if success else 0.0
    return successes / total


def _looks_like_aggregate_report(payload: Any) -> bool:
    return isinstance(payload, dict) and "resolved_ids" in payload and "unresolved_ids" in payload
