"""Minimal command oracle runner for GEM admission."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any


ORACLE_RUN_SCHEMA_VERSION = "gem.oracle_run.v1"

ORACLE_RESULTS = ("pass", "fail", "error", "timeout")


class OracleRunValidationError(ValueError):
    """Raised when a GEM oracle run payload is malformed."""


def run_command_oracle(
    pre_worktree: str | Path,
    post_worktree: str | Path,
    command: str | list[str] | tuple[str, ...],
    *,
    candidate_id: str,
    oracle_id: str,
    timeout_s: float = 30.0,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Run a command in pre and post worktrees and return a GEM oracle record."""

    pre = _run_command(command, Path(pre_worktree), timeout_s)
    post = _run_command(command, Path(post_worktree), timeout_s)
    return _build_oracle_run(
        candidate_id=candidate_id,
        oracle_id=oracle_id,
        command=command,
        pre_worktree=str(pre_worktree),
        post_worktree=str(post_worktree),
        pre_patch_result=pre["result"],
        post_patch_result=post["result"],
        logs={"pre": pre["log"], "post": post["log"]},
        cost={
            "pre_wall_time_s": pre["wall_time_s"],
            "post_wall_time_s": post["wall_time_s"],
            "wall_time_s": round(pre["wall_time_s"] + post["wall_time_s"], 6),
        },
        created_at=created_at,
    )


def build_oracle_run_from_results(
    *,
    candidate_id: str,
    oracle_id: str,
    command: str | list[str] | tuple[str, ...],
    pre_patch_result: str,
    post_patch_result: str,
    pre_worktree: str = "",
    post_worktree: str = "",
    logs: dict[str, Any] | None = None,
    wall_time_s: float = 0.0,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated oracle run from mocked pre/post results."""

    normalized_logs = logs or {
        "pre": {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "error": "",
            "wall_time_s": 0.0,
        },
        "post": {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "error": "",
            "wall_time_s": 0.0,
        },
    }
    return _build_oracle_run(
        candidate_id=candidate_id,
        oracle_id=oracle_id,
        command=command,
        pre_worktree=pre_worktree,
        post_worktree=post_worktree,
        pre_patch_result=pre_patch_result,
        post_patch_result=post_patch_result,
        logs=normalized_logs,
        cost={"wall_time_s": float(wall_time_s)},
        created_at=created_at,
    )


def build_mock_oracle_run(**kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for fixture tests."""

    return build_oracle_run_from_results(**kwargs)


def validate_oracle_run(record: dict[str, Any]) -> None:
    """Validate a GEM oracle run record."""

    if not isinstance(record, dict):
        raise OracleRunValidationError("oracle run must be a mapping")
    for key in (
        "schema_version",
        "oracle_run_id",
        "created_at",
        "candidate_id",
        "oracle_id",
        "command",
        "pre_worktree",
        "post_worktree",
        "pre_patch_result",
        "post_patch_result",
        "fail_before",
        "pass_after",
        "logs",
        "cost",
        "wall_time_s",
    ):
        if key not in record:
            raise OracleRunValidationError(f"missing oracle run field: {key}")
    if record["schema_version"] != ORACLE_RUN_SCHEMA_VERSION:
        raise OracleRunValidationError("unsupported oracle run schema_version")
    for key in ("oracle_run_id", "created_at", "candidate_id", "oracle_id"):
        if not isinstance(record[key], str) or not record[key]:
            raise OracleRunValidationError(f"{key} must be a non-empty string")
    for key in ("pre_worktree", "post_worktree"):
        if not isinstance(record[key], str):
            raise OracleRunValidationError(f"{key} must be a string")
    _validate_command(record["command"])
    if record["pre_patch_result"] not in ORACLE_RESULTS:
        raise OracleRunValidationError("invalid pre_patch_result")
    if record["post_patch_result"] not in ORACLE_RESULTS:
        raise OracleRunValidationError("invalid post_patch_result")
    if record["fail_before"] is not (record["pre_patch_result"] == "fail"):
        raise OracleRunValidationError("fail_before does not match pre_patch_result")
    if record["pass_after"] is not (record["post_patch_result"] == "pass"):
        raise OracleRunValidationError("pass_after does not match post_patch_result")
    if not isinstance(record["logs"], dict):
        raise OracleRunValidationError("logs must be a mapping")
    if not isinstance(record["cost"], dict):
        raise OracleRunValidationError("cost must be a mapping")
    try:
        float(record["wall_time_s"])
    except (TypeError, ValueError) as exc:
        raise OracleRunValidationError("wall_time_s must be numeric") from exc
    if record["oracle_run_id"] != oracle_run_id(record):
        raise OracleRunValidationError("oracle_run_id does not match payload")


def oracle_run_id(record: dict[str, Any]) -> str:
    """Return a deterministic oracle run id for replay-stable payload fields."""

    payload = _jsonable(deepcopy(record))
    if isinstance(payload, dict):
        payload.pop("oracle_run_id", None)
        payload.pop("created_at", None)
        payload.pop("logs", None)
        payload.pop("cost", None)
        payload.pop("wall_time_s", None)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"gemorun_{hashlib.sha256(encoded).hexdigest()[:20]}"


def read_oracle_runs_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read validated oracle runs from JSONL."""

    source = Path(path)
    if not source.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OracleRunValidationError(
                f"invalid JSON at {source}:{line_number}"
            ) from exc
        validate_oracle_run(record)
        records.append(record)
    return records


def write_oracle_runs_jsonl(
    path: str | Path,
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> None:
    """Write validated oracle runs to JSONL."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for record in records:
        validate_oracle_run(record)
        lines.append(
            json.dumps(
                _jsonable(record),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _build_oracle_run(
    *,
    candidate_id: str,
    oracle_id: str,
    command: str | list[str] | tuple[str, ...],
    pre_worktree: str,
    post_worktree: str,
    pre_patch_result: str,
    post_patch_result: str,
    logs: dict[str, Any],
    cost: dict[str, Any],
    created_at: str | None,
) -> dict[str, Any]:
    record = {
        "schema_version": ORACLE_RUN_SCHEMA_VERSION,
        "oracle_run_id": "",
        "created_at": created_at or _utc_now(),
        "candidate_id": str(candidate_id or ""),
        "oracle_id": str(oracle_id or ""),
        "command": _normalize_command(command),
        "pre_worktree": str(pre_worktree or ""),
        "post_worktree": str(post_worktree or ""),
        "pre_patch_result": str(pre_patch_result or ""),
        "post_patch_result": str(post_patch_result or ""),
        "fail_before": str(pre_patch_result or "") == "fail",
        "pass_after": str(post_patch_result or "") == "pass",
        "logs": _jsonable(logs),
        "cost": _jsonable(cost),
        "wall_time_s": float(cost.get("wall_time_s", 0.0) or 0.0),
    }
    record["oracle_run_id"] = oracle_run_id(record)
    validate_oracle_run(record)
    return record


def _run_command(
    command: str | list[str] | tuple[str, ...],
    worktree: Path,
    timeout_s: float,
) -> dict[str, Any]:
    _validate_command(_normalize_command(command))
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command if not isinstance(command, tuple) else list(command),
            cwd=str(worktree),
            shell=isinstance(command, str),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        wall_time_s = round(time.monotonic() - start, 6)
        return {
            "result": "timeout",
            "wall_time_s": wall_time_s,
            "log": {
                "stdout": _text(exc.stdout),
                "stderr": _text(exc.stderr),
                "exit_code": None,
                "error": "timeout",
                "wall_time_s": wall_time_s,
            },
        }
    except Exception as exc:  # noqa: BLE001 - exception is the oracle result.
        wall_time_s = round(time.monotonic() - start, 6)
        return {
            "result": "error",
            "wall_time_s": wall_time_s,
            "log": {
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "error": f"{type(exc).__name__}: {exc}",
                "wall_time_s": wall_time_s,
            },
        }

    wall_time_s = round(time.monotonic() - start, 6)
    return {
        "result": "pass" if completed.returncode == 0 else "fail",
        "wall_time_s": wall_time_s,
        "log": {
            "stdout": _text(completed.stdout),
            "stderr": _text(completed.stderr),
            "exit_code": int(completed.returncode),
            "error": "",
            "wall_time_s": wall_time_s,
        },
    }


def _validate_command(command: Any) -> None:
    if isinstance(command, str):
        if not command.strip():
            raise OracleRunValidationError("command must be non-empty")
        return
    if isinstance(command, list):
        if not command or not all(isinstance(item, str) and item for item in command):
            raise OracleRunValidationError("command list must contain non-empty strings")
        return
    raise OracleRunValidationError("command must be a string or list of strings")


def _normalize_command(command: str | list[str] | tuple[str, ...]) -> str | list[str]:
    if isinstance(command, tuple):
        return [str(item) for item in command]
    if isinstance(command, list):
        return [str(item) for item in command]
    return str(command)


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


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
