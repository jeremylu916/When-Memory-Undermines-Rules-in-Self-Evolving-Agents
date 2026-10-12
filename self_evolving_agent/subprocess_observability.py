"""Shared subprocess diagnostics for official evaluator phases."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Iterable


SUBPROCESS_OBSERVABILITY_SCHEMA_VERSION = "m6.subprocess_observability.v1"
CommandRunner = Callable[..., Any]

_SENSITIVE_ENV_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)

_SECRET_FLAG_MARKERS = (
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)


def run_checked_subprocess(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    phase: str,
    evaluation_dir: Path,
    command_runner: CommandRunner,
    retries: int = 0,
    log_name: str | None = None,
    additional_log_names: Iterable[str] = (),
    phase_key: str | None = None,
    stream_output: bool = False,
    announce_attempts: bool = False,
    output_limit: int = 20000,
) -> None:
    """Run a command and write redacted per-attempt diagnostics."""

    primary_log_name = log_name or f"subprocess_{subprocess_phase_slug(phase)}.json"
    log_paths = _dedupe_paths(
        [evaluation_dir / primary_log_name]
        + [evaluation_dir / str(name) for name in additional_log_names]
    )
    attempts: list[dict[str, Any]] = []
    run_started_at = _utc_now()
    run_start = time.monotonic()
    total_attempts = max(0, retries) + 1
    last_returncode = 1
    last_retryable = False
    last_stdout_tail = ""
    last_stderr_tail = ""
    phase_key = phase_key or subprocess_phase_slug(phase)

    for attempt_index in range(total_attempts):
        if announce_attempts:
            print(
                f"[{phase}] attempt {attempt_index + 1}/{total_attempts}",
                flush=True,
            )
        attempt_started_at = _utc_now()
        attempt_start = time.monotonic()
        completed = _execute_command(
            command,
            cwd=cwd,
            environment=environment,
            command_runner=command_runner,
            stream_output=stream_output,
            output_limit=output_limit,
        )
        attempt_ended_at = _utc_now()
        duration_s = round(time.monotonic() - attempt_start, 6)
        last_returncode = int(getattr(completed, "returncode", 1))
        last_stdout_tail = redact_subprocess_text(
            str(getattr(completed, "stdout", "") or ""),
            environment,
            limit=output_limit,
        )
        last_stderr_tail = redact_subprocess_text(
            str(getattr(completed, "stderr", "") or ""),
            environment,
            limit=output_limit,
        )
        last_retryable = is_retryable_subprocess_failure(
            last_stdout_tail,
            last_stderr_tail,
        )
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "command": redact_command(command, environment),
                "cwd": str(cwd),
                "returncode": last_returncode,
                "retryable": last_retryable,
                "started_at": attempt_started_at,
                "ended_at": attempt_ended_at,
                "duration_s": duration_s,
                "stdout_tail": last_stdout_tail,
                "stderr_tail": last_stderr_tail,
            }
        )
        _write_subprocess_log(
            log_paths,
            {
                "schema_version": SUBPROCESS_OBSERVABILITY_SCHEMA_VERSION,
                "phase": phase,
                "phase_key": phase_key,
                "command": redact_command(command, environment),
                "cwd": str(cwd),
                "returncode": last_returncode,
                "retryable": last_retryable,
                "started_at": run_started_at,
                "ended_at": attempt_ended_at,
                "duration_s": round(time.monotonic() - run_start, 6),
                "attempt_count": len(attempts),
                "attempts": attempts,
                "stdout_tail": last_stdout_tail,
                "stderr_tail": last_stderr_tail,
                "output_limit_chars": int(output_limit),
            },
        )
        if last_returncode == 0:
            return
        if not last_retryable or attempt_index >= retries:
            raise RuntimeError(
                f"{phase} failed with exit code {last_returncode}; "
                f"details logged at {log_paths[0]}"
            )


def subprocess_phase_slug(phase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(phase).lower()).strip("_")


def redact_command(command: list[str], environment: dict[str, str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for item in command:
        text = str(item)
        lower = text.lower().lstrip("-")
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if any(marker in lower for marker in _SECRET_FLAG_MARKERS):
            if "=" in text:
                key, _value = text.split("=", 1)
                redacted.append(f"{key}=[REDACTED]")
            else:
                redacted.append(text)
                redact_next = True
            continue
        redacted.append(redact_subprocess_text(text, environment))
    return redacted


def redact_subprocess_text(
    value: str,
    environment: dict[str, str],
    *,
    limit: int = 20000,
) -> str:
    text = str(value)
    for name, secret in environment.items():
        if (
            secret
            and len(secret) >= 8
            and any(marker in name.lower() for marker in _SENSITIVE_ENV_MARKERS)
        ):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._\-]+",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(
        r"\bsk-[A-Za-z0-9_\-]{8,}\b",
        "sk-[REDACTED]",
        text,
    )
    return text[-limit:]


def is_retryable_subprocess_failure(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return any(
        marker in combined
        for marker in (
            "unexpected_eof",
            "unexpected eof",
            "sslerror",
            "ssl:",
            "connection reset",
            "max retries exceeded",
            "temporary failure",
            "temporarily unavailable",
            "timed out",
            "timeout",
        )
    )


def _execute_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    command_runner: CommandRunner,
    stream_output: bool,
    output_limit: int,
) -> Any:
    if stream_output and command_runner is subprocess.run:
        return _execute_streamed_command(
            command,
            cwd=cwd,
            environment=environment,
            output_limit=output_limit,
        )
    return command_runner(
        command,
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
    )


def _execute_streamed_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    output_limit: int,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_tail: deque[str] = deque()
    output_chars = 0
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output_tail.append(line)
        output_chars += len(line)
        while output_chars > output_limit and output_tail:
            output_chars -= len(output_tail.popleft())
    return subprocess.CompletedProcess(
        args=command,
        returncode=process.wait(),
        stdout="".join(output_tail),
        stderr="",
    )


def _write_subprocess_log(paths: list[Path], payload: dict[str, Any]) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
