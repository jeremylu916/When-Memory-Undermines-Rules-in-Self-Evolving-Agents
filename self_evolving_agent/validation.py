"""Bounded target-test validation inside a SWE-bench instance image."""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from self_evolving_agent.benchmarks import BenchmarkTask

VALIDATION_PASSED = "passed"
VALIDATION_PATCH_FAILURE = "patch_failure"
VALIDATION_ENVIRONMENT_FAILURE = "validation_environment_failure"
VALIDATION_TOOL_FAILURE = "tool_failure"

_MODEL_PATCH_APPLY_EXIT = 91
_TEST_PATCH_APPLY_EXIT = 92
_DOCKER_TOOL_EXIT_CODES = {125, 126, 127}
_PYTEST_TOOL_EXIT_CODES = {4, 5}
_PATCH_ERROR_MARKERS = (
    "syntaxerror:",
    "indentationerror:",
    "taberror:",
)
_ENVIRONMENT_FAILURE_MARKERS = (
    ("conda_env_missing", "environmentlocationnotfound"),
    ("conda_env_missing", "could not find conda environment"),
    ("conda_env_missing", "condaenvironmentnotfounderror"),
    ("conda_env_missing", "environment location not found"),
    ("conda_env_missing", "not a conda environment"),
    ("conda_env_missing", "no such environment"),
    ("missing_pytest", "no module named pytest"),
    ("missing_pytest", "no module named 'pytest'"),
    ("validation_import_environment", "importerror while loading conftest"),
    ("network_unavailable", "temporary failure in name resolution"),
    ("network_unavailable", "name or service not known"),
    ("network_unavailable", "network is unreachable"),
    ("image_unavailable", "unable to find image"),
    ("docker_daemon_unavailable", "cannot connect to the docker daemon"),
    ("docker_daemon_unavailable", "error response from daemon"),
)


@dataclass(frozen=True)
class ValidationCommandResult:
    command: st
    phase: st
    cwd: st
    image: st
    attempt: st
    stdout: st
    stderr: str
    exit_code: int | None
    timed_out: bool
    timeout_s: int
    duration_ms: int
    validation_status: st
    failure_type: str | None
    failure_summary: st
    model_patch_sha256: st
    model_patch_chars: int
    test_patch_applied: bool
    selected_pass_to_pass_guards: tuple[str, ...] = ()
    guard_selection_reason: str = ""
    guard_coverage_count: int = 0
    failure_reason: str = ""
    not_patch_failure: bool = False

    def to_trace(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationRun:
    passed: bool
    failure_type: str | None
    failure_summary: st
    command_results: tuple[ValidationCommandResult, ...]

    @property
    def feedback_eligible(self) -> bool:
        return self.failure_type == VALIDATION_PATCH_FAILURE


@dataclass(frozen=True)
class _TestTarget:
    raw: st
    path: st
    class_name: str | None
    test_name: st
    tokens: frozenset[str]


def run_image_validation(
    *,
    task: BenchmarkTask,
    image: str,
    patch_text: str,
    timeout_s: int,
    attempt: str,
    max_commands: int = 10,
    log_chars: int = 12000,
) -> ValidationRun:
    """Apply model/test patches in an ephemeral image and run target tests."""
    pass_to_pass_targets = _metadata_test_targets(task.metadata, "pass_to_pass")
    commands = build_validation_commands(
        task.test_commands,
        max_commands=max_commands,
        test_patch=task.test_patch,
        pass_to_pass_targets=pass_to_pass_targets,
    )
    selected_guards = _selected_pass_to_pass_guards(commands, pass_to_pass_targets)
    guard_selection_reason = (
        "nearby_pass_to_pass"
        if selected_guards
        else "no_pass_to_pass_guard_selected"
    )
    if not commands:
        summary = "No target test commands were available for validation."
        return ValidationRun(
            passed=False,
            failure_type=VALIDATION_TOOL_FAILURE,
            failure_summary=summary,
            command_results=(),
        )
    if not patch_text.strip():
        summary = "No model patch was available for image validation."
        return ValidationRun(
            passed=False,
            failure_type=VALIDATION_TOOL_FAILURE,
            failure_summary=summary,
            command_results=(),
        )

    records: list[ValidationCommandResult] = []
    with tempfile.TemporaryDirectory(prefix="sea-validation-") as tmpdir:
        patch_dir = Path(tmpdir)
        _write_patch(patch_dir / "model.patch", patch_text)
        _write_patch(patch_dir / "test.patch", task.test_patch)

        for command in commands:
            record = _run_validation_command(
                command=command,
                image=image,
                patch_dir=patch_dir,
                patch_text=patch_text,
                test_patch=task.test_patch,
                timeout_s=timeout_s,
                attempt=attempt,
                log_chars=log_chars,
                selected_pass_to_pass_guards=selected_guards,
                guard_selection_reason=guard_selection_reason,
            )
            records.append(record)
            if record.validation_status != VALIDATION_PASSED:
                break

    failure = next(
        (
            record
            for record in records
            if record.validation_status != VALIDATION_PASSED
        ),
        None,
    )
    if failure is None:
        return ValidationRun(
            passed=True,
            failure_type=None,
            failure_summary="",
            command_results=tuple(records),
        )
    return ValidationRun(
        passed=False,
        failure_type=failure.failure_type,
        failure_summary=failure.failure_summary,
        command_results=tuple(records),
    )


def build_validation_commands(
    commands: tuple[str, ...] | list[str],
    *,
    max_commands: int,
    test_patch: str = "",
    pass_to_pass_targets: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Select a bounded command set and merge compatible focused pytest calls."""
    cleaned = [str(command).strip() for command in commands if str(command).strip()]
    budget = max(0, max_commands)
    added_tests = _added_test_identifiers(test_patch)
    focused = [
        command
        for command in cleaned
        if any(identifier in command for identifier in added_tests)
    ]
    if focused:
        supplemental = _nearby_pass_to_pass_commands(
            focused,
            pass_to_pass_targets,
            limit=max(0, budget - len(focused)),
        )
        bounded = (focused + supplemental)[:budget]
    else:
        bounded = cleaned[:budget]
    if len(bounded) <= 1:
        return bounded

    parsed: list[list[str]] = []
    try:
        parsed = [shlex.split(command) for command in bounded]
    except ValueError:
        return bounded
    if any(len(parts) < 2 for parts in parsed):
        return bounded

    prefix = parsed[0][:-1]
    if not prefix or not all(parts[:-1] == prefix for parts in parsed):
        return bounded
    if "pytest" not in " ".join(prefix).lower():
        return bounded
    return [shlex.join(prefix + [parts[-1] for parts in parsed])]


def _nearby_pass_to_pass_commands(
    focused_commands: list[str],
    pass_to_pass_targets: tuple[str, ...] | list[str],
    *,
    limit: int,
) -> list[str]:
    if limit <= 0 or not pass_to_pass_targets:
        return []

    focused = [_pytest_command_target(command) for command in focused_commands]
    if any(item is None for item in focused):
        return []
    focused_pairs = [item for item in focused if item is not None]
    prefixes = [pair[0] for pair in focused_pairs]
    if not prefixes or any(prefix != prefixes[0] for prefix in prefixes):
        return []

    prefix = prefixes[0]
    focused_infos = [pair[1] for pair in focused_pairs]
    used_targets = {info.raw for info in focused_infos}
    candidates = [
        (index, target_info)
        for index, target in enumerate(pass_to_pass_targets)
        if (target_info := _parse_test_target(str(target).strip())) is not None
        and target_info.raw not in used_targets
    ]

    selected: list[_TestTarget] = []
    selected_targets = set(used_targets)

    # First give each focused target one nearby regression guard. This avoids a
    # large same-file bucket starving another changed test file under a tight
    # validation budget, which is exactly what the Xarray regression exposed.
    for focused_info in focused_infos:
        best = _best_candidate_for_focus(candidates, focused_info, selected_targets)
        if best is None:
            continue
        selected.append(best)
        selected_targets.add(best.raw)
        if len(selected) >= limit:
            return [shlex.join(prefix + [target.raw]) for target in selected]

    remaining = [
        (score, index, target)
        for index, target in candidates
        if target.raw not in selected_targets
        if (score := _nearby_score(target, focused_infos)) is not None
    ]
    remaining.sort(key=lambda item: (item[0], item[1]))
    for _, _, target in remaining:
        selected.append(target)
        if len(selected) >= limit:
            break

    return [shlex.join(prefix + [target.raw]) for target in selected]


def summarize_validation_failure(
    stdout: str,
    stderr: str,
    *,
    max_chars: int = 6000,
) -> str:
    """Keep high-signal failure lines plus the output tail within a hard bound."""
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not combined:
        return "Validation command failed without stdout or stderr."

    lines = combined.splitlines()
    markers = (
        "failed",
        "error",
        "traceback",
        "assert",
        "exception",
        "no module named",
        "importerror",
        "modulenotfounderror",
        "command not found",
        "timed out",
    )
    selected = [
        line
        for line in lines
        if any(marker in line.lower() for marker in markers)
    ]
    tail = lines[-80:]
    compact = "\n".join(dict.fromkeys(selected[-80:] + tail)).strip()
    return _bounded_text(compact or combined, max_chars)


def _run_validation_command(
    *,
    command: str,
    image: str,
    patch_dir: Path,
    patch_text: str,
    test_patch: str,
    timeout_s: int,
    attempt: str,
    log_chars: int,
    selected_pass_to_pass_guards: tuple[str, ...] = (),
    guard_selection_reason: str = "",
) -> ValidationCommandResult:
    shell_script = _validation_shell_script(command, has_test_patch=bool(test_patch.strip()))
    docker_command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/bin/bash",
        "-v",
        f"{patch_dir.resolve()}:/validation:ro",
        "-w",
        "/testbed",
        image,
        "-lc",
        shell_script,
    ]
    started = time.perf_counter()
    stdout = ""
    stderr = ""
    exit_code: int | None = None
    timed_out = False
    forced_failure_type: str | None = None
    try:
        completed = subprocess.run(
            docker_command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_stream(exc.stdout)
        stderr = _decode_stream(exc.stderr)
        timed_out = True
        forced_failure_type = VALIDATION_ENVIRONMENT_FAILURE
    except FileNotFoundError as exc:
        stderr = str(exc)
        forced_failure_type = VALIDATION_TOOL_FAILURE
    except OSError as exc:
        stderr = str(exc)
        forced_failure_type = VALIDATION_TOOL_FAILURE

    duration_ms = max(1, int((time.perf_counter() - started) * 1000))
    if forced_failure_type is not None:
        failure_type = forced_failure_type
        failure_reason = _forced_failure_reason(
            forced_failure_type,
            timed_out=timed_out,
        )
    else:
        failure_type, failure_reason = _classify_validation_failure(
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
        )
    status = VALIDATION_PASSED if failure_type is None else failure_type
    summary = (
        ""
        if failure_type is None
        else summarize_validation_failure(stdout, stderr, max_chars=min(log_chars, 6000))
    )
    return ValidationCommandResult(
        command=command,
        phase="validation_test",
        cwd="/testbed",
        image=image,
        attempt=attempt,
        stdout=_bounded_text(stdout, log_chars),
        stderr=_bounded_text(stderr, log_chars),
        exit_code=exit_code,
        timed_out=timed_out,
        timeout_s=timeout_s,
        duration_ms=duration_ms,
        validation_status=status,
        failure_type=failure_type,
        failure_summary=summary,
        model_patch_sha256=hashlib.sha256(patch_text.encode("utf-8")).hexdigest(),
        model_patch_chars=len(patch_text),
        test_patch_applied=bool(test_patch.strip()),
        selected_pass_to_pass_guards=selected_pass_to_pass_guards,
        guard_selection_reason=guard_selection_reason,
        guard_coverage_count=len(selected_pass_to_pass_guards),
        failure_reason=failure_reason,
        not_patch_failure=failure_type is not None
        and failure_type != VALIDATION_PATCH_FAILURE,
    )


def _validation_shell_script(command: str, *, has_test_patch: bool) -> str:
    lines = [
        "set +e",
        "git apply --whitespace=nowarn /validation/model.patch",
        'model_patch_rc="$?"',
        (
            f'if [ "$model_patch_rc" -ne 0 ]; then '
            'echo "__SEA_VALIDATION_STAGE=model_patch" >&2; '
            f"exit {_MODEL_PATCH_APPLY_EXIT}; fi"
        ),
    ]
    if has_test_patch:
        lines.extend(
            [
                "git apply --whitespace=nowarn /validation/test.patch",
                'test_patch_rc="$?"',
                (
                    f'if [ "$test_patch_rc" -ne 0 ]; then '
                    'echo "__SEA_VALIDATION_STAGE=test_patch" >&2; '
                    f"exit {_TEST_PATCH_APPLY_EXIT}; fi"
                ),
            ]
        )
    lines.extend(
        [
            'echo "__SEA_VALIDATION_STAGE=test" >&2',
            f"exec /bin/bash -lc {shlex.quote(command)}",
        ]
    )
    return "\n".join(lines)


def _classify_validation_failure(
    *,
    exit_code: int | None,
    timed_out: bool,
    stdout: str,
    stderr: str,
) -> tuple[str | None, str]:
    if not timed_out and exit_code == 0:
        return None, ""
    if timed_out:
        return VALIDATION_ENVIRONMENT_FAILURE, "timeout"
    if exit_code in {
        _MODEL_PATCH_APPLY_EXIT,
        _TEST_PATCH_APPLY_EXIT,
        *_DOCKER_TOOL_EXIT_CODES,
    }:
        return VALIDATION_TOOL_FAILURE, "validation_tool_failure"

    combined = f"{stdout}\n{stderr}".lower()
    if any(marker in combined for marker in _PATCH_ERROR_MARKERS):
        return VALIDATION_PATCH_FAILURE, "patch_syntax_error"

    environment_reason = _validation_environment_reason(combined)
    if environment_reason:
        return VALIDATION_ENVIRONMENT_FAILURE, environment_reason
    if exit_code in _PYTEST_TOOL_EXIT_CODES:
        return VALIDATION_TOOL_FAILURE, "pytest_usage_or_collection_error"
    return VALIDATION_PATCH_FAILURE, "test_failure"


def _validation_environment_reason(combined_output: str) -> str:
    for reason, marker in _ENVIRONMENT_FAILURE_MARKERS:
        if marker in combined_output:
            return reason
    return ""


def _forced_failure_reason(
    failure_type: str,
    *,
    timed_out: bool,
) -> str:
    if timed_out:
        return "timeout"
    if failure_type == VALIDATION_TOOL_FAILURE:
        return "validator_invocation_error"
    if failure_type == VALIDATION_ENVIRONMENT_FAILURE:
        return "validation_environment_failure"
    return ""


def _metadata_test_targets(metadata: dict[str, Any] | None, key: str) -> tuple[str, ...]:
    if not isinstance(metadata, dict):
        return ()
    value = metadata.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _selected_pass_to_pass_guards(
    commands: list[str],
    pass_to_pass_targets: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    targets = [str(target).strip() for target in pass_to_pass_targets if str(target).strip()]
    selected = [
        target
        for target in targets
        if any(target in command for command in commands)
    ]
    return tuple(dict.fromkeys(selected))


def _pytest_command_target(command: str) -> tuple[list[str], _TestTarget] | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    prefix = parts[:-1]
    if "pytest" not in " ".join(prefix).lower():
        return None
    target = _parse_test_target(parts[-1])
    if target is None:
        return None
    return prefix, target


def _parse_test_target(target: str) -> _TestTarget | None:
    raw = target.strip().strip("'\"")
    if not raw:
        return None
    parts = raw.split("::")
    path = parts[0]
    if ".py" not in path:
        return None

    class_name: str | None = None
    for part in parts[1:-1]:
        base = _strip_param_suffix(part)
        if base and not base.startswith("test_"):
            class_name = base
            break

    test_name = _strip_param_suffix(parts[-1]) if len(parts) > 1 else Path(path).stem
    tokens = _target_tokens(test_name) - _path_tokens(path)
    return _TestTarget(
        raw=raw,
        path=path,
        class_name=class_name,
        test_name=test_name,
        tokens=frozenset(tokens),
    )


def _best_candidate_for_focus(
    candidates: list[tuple[int, _TestTarget]],
    focused: _TestTarget,
    used_targets: set[str],
) -> _TestTarget | None:
    matches = [
        (score, index, target)
        for index, target in candidates
        if target.raw not in used_targets
        if (score := _nearby_score(target, [focused])) is not None
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]))
    return matches[0][2]


def _nearby_score(
    candidate: _TestTarget,
    focused_targets: list[_TestTarget],
) -> int | None:
    scores: list[int] = []
    for focused in focused_targets:
        if candidate.path != focused.path:
            continue
        same_class = (
            candidate.class_name
            and focused.class_name
            and candidate.class_name == focused.class_name
        )
        shared_tokens = bool(candidate.tokens & focused.tokens)
        if same_class and shared_tokens:
            scores.append(0)
        elif shared_tokens:
            scores.append(1)
        else:
            scores.append(2)
    return min(scores) if scores else None


def _strip_param_suffix(name: str) -> str:
    return name.split("[", 1)[0]


def _target_tokens(name: str) -> set[str]:
    stopwords = {"test", "tests", "with", "without", "some", "all", "new", "old"}
    return {
        token
        for token in re.split(r"[^A-Za-z0-9]+", name.lower())
        if token and token not in stopwords
    }


def _path_tokens(path: str) -> set[str]:
    return _target_tokens(Path(path).stem)


def _added_test_identifiers(test_patch: str) -> list[str]:
    identifiers: list[str] = []
    for line in test_patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        function_match = re.match(r"\+\s*def\s+(test_[A-Za-z0-9_]+)", line)
        if function_match:
            identifiers.append(function_match.group(1))
            continue
        class_match = re.match(
            r"\+\s*class\s+([A-Za-z0-9_]*(?:Test|Tests)[A-Za-z0-9_]*)",
            line,
        )
        if class_match:
            identifiers.append(class_match.group(1))
    return list(dict.fromkeys(identifiers))


def _write_patch(path: Path, text: str) -> None:
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _bounded_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head
    return f"{text[:head]}\n...<truncated>...\n{text[-tail:]}"


def _decode_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
