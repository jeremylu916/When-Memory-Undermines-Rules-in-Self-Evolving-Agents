from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


LEGACY_GENERATION_PYTHON = os.path.expanduser("~/miniconda3/envs/evo/bin/python")
LEGACY_HARNESS_PYTHON = os.path.expanduser("~/miniconda3/envs/swebench/bin/python")


def default_generation_python() -> str:
    return _resolve_default_python(
        env_keys=("SEA_GENERATION_PYTHON", "SELF_EVOLVING_AGENT_GENERATION_PYTHON"),
        legacy_path=LEGACY_GENERATION_PYTHON,
        repo_env_name="evo",
    )


def default_harness_python() -> str:
    return _resolve_default_python(
        env_keys=("SEA_HARNESS_PYTHON", "SELF_EVOLVING_AGENT_HARNESS_PYTHON"),
        legacy_path=LEGACY_HARNESS_PYTHON,
        repo_env_name="swebench",
    )


def validate_python_runtime(
    executable: str,
    *,
    purpose: str,
    required_module: str | None = None,
) -> None:
    if not _is_executable_usable(executable):
        raise RuntimeError(
            f"{purpose} python is not available: {executable}. "
            "Pass an explicit path via the CLI or set the corresponding "
            "SEA_*_PYTHON environment variable."
        )
    if required_module and not _supports_module(executable, required_module):
        raise RuntimeError(
            f"{purpose} python cannot import `{required_module}`: {executable}. "
            "Point `--harness-python` at an environment where that package is installed."
        )


def _resolve_default_python(
    *,
    env_keys: tuple[str, ...],
    legacy_path: str,
    repo_env_name: str,
) -> str:
    for env_key in env_keys:
        candidate = os.environ.get(env_key)
        if candidate:
            return candidate
    if Path(legacy_path).exists():
        return legacy_path
    repo_local = (
        Path(__file__).resolve().parents[1]
        / ".miniconda3"
        / "envs"
        / repo_env_name
        / "bin"
        / "python"
    )
    if repo_local.exists():
        return str(repo_local)
    if sys.executable:
        return sys.executable
    discovered = shutil.which("python3") or shutil.which("python")
    return discovered or legacy_path


def _is_executable_usable(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _supports_module(executable: str, module_name: str) -> bool:
    try:
        completed = subprocess.run(
            [executable, "-c", f"import {module_name}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=20,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0
