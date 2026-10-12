"""Configuration loading and writing for the minimal YAML subset we use."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
from typing import Any


# The six auditable prompt modules that controlled self-evolution is allowed to edit.
# Order is the order in which the agent consumes them while solving a task.
PROMPT_MODULES = (
    "system",
    "planning",
    "localization",
    "patch_generation",
    "verification",
    "security_review",
)

DEFAULT_PROMPTS = {
    "system": "You are a controlled software engineering agent. Solve the task and keep changes minimal.",
    "planning": "Break the task into steps: understand the issue, locate code, edit, verify.",
    "localization": "Identify the files and code locations that must change to resolve the issue.",
    "patch_generation": "Produce a minimal patch that resolves the issue. Task: {task_prompt}",
    "verification": "Re-run the relevant tests and confirm the change does not break passing tests.",
    "security_review": "For security-sensitive tasks, check the patch removes the vulnerability without unsafe behavior.",
}


def get_prompt_modules(config: dict[str, Any]) -> dict[str, str]:
    """Return the six prompt modules, filling defaults for any that are missing.

    Also accepts the legacy single-prompt layout (``system_prompt`` /
    ``task_prompt_template``) so older configs keep working.
    """
    prompts = config.get("prompts")
    modules = dict(DEFAULT_PROMPTS)

    if isinstance(prompts, dict):
        for module in PROMPT_MODULES:
            if prompts.get(module) is not None:
                modules[module] = str(prompts[module])
    else:
        legacy_system = config.get("system_prompt")
        if legacy_system is not None:
            modules["system"] = str(legacy_system)
        legacy_patch = config.get("task_prompt_template")
        if legacy_patch is not None:
            modules["patch_generation"] = str(legacy_patch)

    return modules


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")

    if config_path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = _load_yaml_subset(text)

    if not isinstance(data, dict):
        raise ValueError(f"Config must be a mapping: {config_path}")
    return data


def dump_config(config: dict[str, Any]) -> str:
    return _dump_mapping(config).rstrip() + "\n"


def write_config(config: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_config(config), encoding="utf-8")


def clone_config(config: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(config)


def config_fingerprint(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _load_yaml_subset(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line:
            raise ValueError(f"Tabs are not supported in config YAML line {line_number}")

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if ":" not in stripped:
            raise ValueError(f"Expected 'key: value' on config line {line_number}")

        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Empty key on config line {line_number}")

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"Invalid indentation on config line {line_number}")

        parent = stack[-1][1]
        raw_value = raw_value.strip()
        if raw_value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(raw_value)

    return root


def _parse_scalar(raw_value: str) -> Any:
    lowered = raw_value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None

    if raw_value[0:1] in {'"', "'"}:
        return ast.literal_eval(raw_value)

    try:
        return int(raw_value)
    except ValueError:
        pass

    try:
        return float(raw_value)
    except ValueError:
        return raw_value


def _dump_mapping(mapping: dict[str, Any], indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in mapping.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(_dump_mapping(value, indent + 2))
        else:
            lines.append(f"{prefix}{key}: {_format_scalar(value)}")
    return "\n".join(lines)


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))
