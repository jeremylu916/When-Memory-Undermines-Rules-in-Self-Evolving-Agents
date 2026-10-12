"""Task workspace preparation, command execution, and patch capture."""

from __future__ import annotations

import inspect
import io
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
import stat

from self_evolving_agent.benchmarks import BenchmarkTask

SUCCESS = "success"
ENVIRONMENT_FAILED = "environment_failed"
TEST_FAILED = "test_failed"
PATCH_FAILED = "patch_failed"


@dataclass(frozen=True)
class CommandResult:
    command: str
    phase: str
    cwd: str
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    timeout_s: int
    duration_ms: int

    def to_trace(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceResult:
    path: Path
    setup_commands: list[CommandResult]
    error: str | None = None


def prepare_task_workspace(
    task: BenchmarkTask,
    workspace_root: Path,
    timeout_s: int,
) -> WorkspaceResult:
    workspace = workspace_root / _safe_task_dir(task.task_id)
    if workspace.exists():
        cleanup_workspace(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)

    setup_commands: list[CommandResult] = []
    if task.repo_url:
        clone = run_command(
            f"git clone --filter=blob:none --no-checkout {task.repo_url} {workspace.name}",
            cwd=workspace.parent,
            timeout_s=timeout_s,
            phase="setup",
        )
        setup_commands = [clone]
        if clone.exit_code != 0 or clone.timed_out:
            archive = _download_github_archive(
                repo_url=task.repo_url,
                commit=task.base_commit,
                workspace=workspace,
                timeout_s=timeout_s,
            )
            setup_commands.append(archive)
            if archive.exit_code != 0 or archive.timed_out:
                return WorkspaceResult(
                    path=workspace,
                    setup_commands=setup_commands,
                    error=f"Could not clone or download repository: {task.repo_url}",
                )
            baseline_commands = _initialize_git_baseline(workspace, timeout_s)
            setup_commands.extend(baseline_commands)
            failed = next(
                (
                    command
                    for command in baseline_commands
                    if command.exit_code != 0 or command.timed_out
                ),
                None,
            )
            if failed:
                return WorkspaceResult(
                    path=workspace,
                    setup_commands=setup_commands,
                    error=f"Could not initialize git baseline for workspace: {failed.command}",
                )
    elif task.repo_path:
        source = Path(task.repo_path)
        if not source.exists():
            return WorkspaceResult(
                path=workspace,
                setup_commands=[],
                error=f"Repository path does not exist: {source}",
            )
        shutil.copytree(source, workspace)
    else:
        workspace.mkdir(parents=True)

    if task.repo_path and not (workspace / ".git").exists():
        setup_commands.extend(_initialize_git_baseline(workspace, timeout_s))
        failed = next(
            (
                command
                for command in setup_commands
                if command.exit_code != 0 or command.timed_out
            ),
            None,
        )
        if failed:
            return WorkspaceResult(
                path=workspace,
                setup_commands=setup_commands,
                error=f"Could not initialize git baseline for workspace: {failed.command}",
            )

    if task.base_commit and (workspace / ".git").exists() and not any(
        command.command.startswith("download_github_archive ") for command in setup_commands
    ):
        checkout = run_command(
            f"git checkout --force {task.base_commit}",
            cwd=workspace,
            timeout_s=timeout_s,
            phase="setup",
        )
        setup_commands.append(checkout)
        if checkout.exit_code != 0 or checkout.timed_out:
            archive = _download_github_archive(
                repo_url=task.repo_url,
                commit=task.base_commit,
                workspace=workspace,
                timeout_s=timeout_s,
            )
            setup_commands.append(archive)
            if archive.exit_code != 0 or archive.timed_out:
                return WorkspaceResult(
                    path=workspace,
                    setup_commands=setup_commands,
                    error=f"Could not checkout or download base commit: {task.base_commit}",
                )
            baseline_commands = _initialize_git_baseline(workspace, timeout_s)
            setup_commands.extend(baseline_commands)
            failed = next(
                (
                    command
                    for command in baseline_commands
                    if command.exit_code != 0 or command.timed_out
                ),
                None,
            )
            if failed:
                return WorkspaceResult(
                    path=workspace,
                    setup_commands=setup_commands,
                    error=f"Could not initialize git baseline for workspace: {failed.command}",
                )

    return WorkspaceResult(path=workspace, setup_commands=setup_commands)


def _download_github_archive(
    *,
    repo_url: str,
    commit: str | None,
    workspace: Path,
    timeout_s: int,
) -> CommandResult:
    started = time.perf_counter()
    archive_url = _github_archive_url(repo_url, commit)
    command = f"download_github_archive {archive_url or repo_url}"
    if archive_url is None:
        return CommandResult(
            command=command,
            phase="setup",
            cwd=str(workspace.parent),
            stdout="",
            stderr="Archive fallback only supports GitHub URLs with base_commit.",
            exit_code=1,
            timed_out=False,
            timeout_s=timeout_s,
            duration_ms=max(1, int((time.perf_counter() - started) * 1000)),
        )

    try:
        if workspace.exists():
            cleanup_workspace(workspace)
        with urllib.request.urlopen(archive_url, timeout=timeout_s) as response:
            archive_bytes = response.read()
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = archive.namelist()
            root = names[0].split("/", 1)[0]
            archive.extractall(workspace.parent)
        extracted = workspace.parent / root
        extracted.rename(workspace)
        return CommandResult(
            command=command,
            phase="setup",
            cwd=str(workspace.parent),
            stdout=f"Downloaded and extracted {archive_url}",
            stderr="",
            exit_code=0,
            timed_out=False,
            timeout_s=timeout_s,
            duration_ms=max(1, int((time.perf_counter() - started) * 1000)),
        )
    except TimeoutError:
        timed_out = True
        stderr = "Archive download timed out."
    except Exception as exc:
        timed_out = False
        stderr = str(exc)

    return CommandResult(
        command=command,
        phase="setup",
        cwd=str(workspace.parent),
        stdout="",
        stderr=stderr,
        exit_code=None if timed_out else 1,
        timed_out=timed_out,
        timeout_s=timeout_s,
        duration_ms=max(1, int((time.perf_counter() - started) * 1000)),
    )


def _github_archive_url(repo_url: str, commit: str | None) -> str | None:
    if not commit:
        return None
    prefix = "https://github.com/"
    if not repo_url.startswith(prefix):
        return None
    repo = repo_url.removeprefix(prefix).removesuffix(".git").strip("/")
    if repo.count("/") != 1:
        return None
    return f"https://github.com/{repo}/archive/{commit}.zip"


def _initialize_git_baseline(workspace: Path, timeout_s: int) -> list[CommandResult]:
    commands = [
        "git init",
        "git config core.autocrlf false",
        "git add -A",
        'git -c user.name="Self Evolving Agent" -c user.email="agent@example.local" commit -m baseline',
    ]
    results: list[CommandResult] = []
    for command in commands:
        result = run_command(command, cwd=workspace, timeout_s=timeout_s, phase="setup")
        results.append(result)
        if result.exit_code != 0 or result.timed_out:
            break
    return results


def run_command(
    command: str,
    *,
    cwd: Path,
    timeout_s: int,
    phase: str,
) -> CommandResult:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            shell=True,
            text=True,
            timeout=timeout_s,
        )
        return CommandResult(
            command=command,
            phase=phase,
            cwd=str(cwd),
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            timed_out=False,
            timeout_s=timeout_s,
            duration_ms=max(1, int((time.perf_counter() - started) * 1000)),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_timeout_stream(exc.stdout)
        stderr = _decode_timeout_stream(exc.stderr)
        return CommandResult(
            command=command,
            phase=phase,
            cwd=str(cwd),
            stdout=stdout,
            stderr=stderr,
            exit_code=None,
            timed_out=True,
            timeout_s=timeout_s,
            duration_ms=max(1, int((time.perf_counter() - started) * 1000)),
        )


def apply_patch_text(
    *,
    workspace: Path,
    patch_text: str,
    timeout_s: int,
    phase: str,
) -> CommandResult:
    workspace = workspace.resolve()
    patch_dir = workspace.parent / "_patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = (patch_dir / f"{workspace.name}-{phase}.patch").resolve()
    if patch_text and not patch_text.endswith("\n"):
        patch_text += "\n"
    patch_path.write_text(patch_text, encoding="utf-8", newline="\n")
    return run_command(
        f'git apply --whitespace=nowarn "{patch_path}"',
        cwd=workspace,
        timeout_s=timeout_s,
        phase=phase,
    )


def collect_patch_diff(workspace: Path, timeout_s: int) -> str:
    if not (workspace / ".git").exists():
        return ""
    diff = run_command(
        "git diff --no-ext-diff --binary",
        cwd=workspace,
        timeout_s=timeout_s,
        phase="capture",
    )
    if diff.exit_code != 0 or diff.timed_out:
        return ""
    return diff.stdout


def classify_execution(
    *,
    agent_success: bool,
    environment_error: str | None,
    command_results: list[dict[str, object]],
) -> str:
    if environment_error:
        return ENVIRONMENT_FAILED

    archive_fallback_succeeded = any(
        str(result["command"]).startswith("download_github_archive ")
        and result["exit_code"] == 0
        and not result["timed_out"]
        for result in command_results
    )
    setup_failed = any(
        result["phase"] in {"setup", "test_patch"}
        and not (
            archive_fallback_succeeded
            and (
                str(result["command"]).startswith("git clone ")
                or str(result["command"]).startswith("git checkout ")
            )
        )
        and (result["timed_out"] or result["exit_code"] not in (0, None))
        for result in command_results
    )
    if setup_failed:
        return ENVIRONMENT_FAILED

    test_runtime_missing = any(
        result["phase"] == "test" and _is_missing_test_runtime(result)
        for result in command_results
    )
    if test_runtime_missing:
        return ENVIRONMENT_FAILED

    test_failed = any(
        result["phase"] == "test"
        and (result["timed_out"] or result["exit_code"] != 0)
        for result in command_results
    )
    if test_failed:
        return TEST_FAILED

    if not agent_success:
        return PATCH_FAILED
    return SUCCESS


def cleanup_workspace(path: Path) -> None:
    if path.exists():
        if "onexc" in inspect.signature(shutil.rmtree).parameters:
            shutil.rmtree(path, onexc=_make_writable_and_retry)
        else:
            shutil.rmtree(path, onerror=_make_writable_and_retry)


def _make_writable_and_retry(function: object, path: str, exc_info: object) -> None:
    del exc_info
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _safe_task_dir(task_id: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in task_id)


def _decode_timeout_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _is_missing_test_runtime(result: dict[str, object]) -> bool:
    combined = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
    return (
        "No module named pytest" in combined
        or "No module named 'pytest'" in combined
        or "ImportError while loading conftest" in combined
        or "ModuleNotFoundError: No module named" in combined
    )
