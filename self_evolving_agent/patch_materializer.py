"""Structured edit materialization for LLM repair output."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StructuredEdit:
    file_path: str
    find: str = ""
    replace: str = ""
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class StructuredEditResult:
    success: bool
    file_edits: list[dict[str, Any]]
    error: str = ""


def extract_structured_edits(text: str) -> list[StructuredEdit]:
    payload = _extract_json_payload(text)
    if payload is None:
        return []

    raw_edits: Any
    if isinstance(payload, list):
        raw_edits = payload
    elif isinstance(payload, dict):
        raw_edits = (
            payload.get("edits")
            or payload.get("changes")
            or payload.get("replacements")
            or [payload]
        )
    else:
        return []

    edits: list[StructuredEdit] = []
    if not isinstance(raw_edits, list):
        return []
    for item in raw_edits:
        if not isinstance(item, dict):
            continue
        file_path = _first_present(item, ("file", "path", "relative_path"))
        find = _first_present(item, ("find", "old", "old_text"))
        replace = _first_present(item, ("replace", "new", "new_text"))
        start_line, end_line = _line_range_fields(item)
        if file_path is None or replace is None:
            continue
        if find is None and (start_line is None or end_line is None):
            continue
        edits.append(
            StructuredEdit(
                file_path=str(file_path),
                find="" if find is None else str(find),
                replace=str(replace),
                start_line=start_line,
                end_line=end_line,
            )
        )
    return edits


def apply_structured_edits(workspace: Path, edits: list[StructuredEdit]) -> StructuredEditResult:
    if not edits:
        return StructuredEditResult(success=False, file_edits=[], error="no structured edits")

    workspace = workspace.resolve()
    buffers: dict[Path, str] = {}
    file_edits: list[dict[str, Any]] = []

    for index, edit in enumerate(edits, start=1):
        safe_path = _safe_relative_path(edit.file_path)
        if safe_path is None:
            return StructuredEditResult(
                success=False,
                file_edits=[],
                error=f"edit {index}: unsafe file path: {edit.file_path}",
            )
        target = (workspace / safe_path).resolve()
        if workspace not in target.parents and target != workspace:
            return StructuredEditResult(
                success=False,
                file_edits=[],
                error=f"edit {index}: path escapes workspace: {edit.file_path}",
            )
        if not target.exists() or not target.is_file():
            return StructuredEditResult(
                success=False,
                file_edits=[],
                error=f"edit {index}: target file not found: {edit.file_path}",
            )
        if target not in buffers:
            try:
                buffers[target] = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return StructuredEditResult(
                    success=False,
                    file_edits=[],
                    error=f"edit {index}: could not read {edit.file_path}: {exc}",
                )

        content = buffers[target]
        find_text = edit.find
        replace_text = edit.replace
        if edit.start_line is not None or edit.end_line is not None:
            range_result = _apply_line_range_edit(
                content,
                edit,
                relative_path=safe_path.as_posix(),
                edit_index=index,
            )
            if isinstance(range_result, StructuredEditResult):
                return range_result
            buffers[target], line_edit_record = range_result
            file_edits.append(line_edit_record)
            continue

        if find_text == "":
            return StructuredEditResult(
                success=False,
                file_edits=[],
                error=f"edit {index}: find text is empty for {edit.file_path}",
            )
        if find_text not in content and "\r\n" in content:
            find_text = find_text.replace("\n", "\r\n")
            replace_text = replace_text.replace("\n", "\r\n")
        if find_text not in content:
            hint = _nearest_snippet_hint(content, find_text)
            return StructuredEditResult(
                success=False,
                file_edits=[],
                error=(
                    f"edit {index}: target text not found in {edit.file_path}"
                    f"{hint}"
                ),
            )

        occurrences = content.count(find_text)
        buffers[target] = content.replace(find_text, replace_text, 1)
        file_edits.append(
            {
                "path": str(target),
                "relative_path": safe_path.as_posix(),
                "action": "structured_replace_text",
                "find_chars": len(find_text),
                "replace_chars": len(replace_text),
                "occurrences": occurrences,
            }
        )

    for target, content in buffers.items():
        try:
            target.write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            return StructuredEditResult(
                success=False,
                file_edits=[],
                error=f"could not write {target.relative_to(workspace).as_posix()}: {exc}",
            )

    return StructuredEditResult(success=True, file_edits=file_edits)


def _apply_line_range_edit(
    content: str,
    edit: StructuredEdit,
    *,
    relative_path: str,
    edit_index: int,
) -> tuple[str, dict[str, Any]] | StructuredEditResult:
    if edit.start_line is None or edit.end_line is None:
        return StructuredEditResult(
            success=False,
            file_edits=[],
            error=f"edit {edit_index}: both start_line and end_line are required for {edit.file_path}",
        )
    if edit.start_line < 1 or edit.end_line < edit.start_line:
        return StructuredEditResult(
            success=False,
            file_edits=[],
            error=(
                f"edit {edit_index}: invalid line range "
                f"{edit.start_line}-{edit.end_line} for {edit.file_path}"
            ),
        )

    lines = content.splitlines(keepends=True)
    if edit.end_line > len(lines):
        return StructuredEditResult(
            success=False,
            file_edits=[],
            error=(
                f"edit {edit_index}: line range {edit.start_line}-{edit.end_line} "
                f"out of bounds for {edit.file_path} ({len(lines)} lines)"
            ),
        )

    selected_text = "".join(lines[edit.start_line - 1 : edit.end_line])
    guard_text = edit.find
    if guard_text:
        normalized_guard = _convert_newlines_like(guard_text, content)
        if selected_text != normalized_guard:
            return StructuredEditResult(
                success=False,
                file_edits=[],
                error=(
                    f"edit {edit_index}: line range guard did not match "
                    f"{edit.file_path}:{edit.start_line}-{edit.end_line}"
                ),
            )

    replacement = _convert_newlines_like(edit.replace, content)
    if replacement and _ends_with_newline(selected_text) and not _ends_with_newline(replacement):
        replacement += _dominant_newline(content)
    lines[edit.start_line - 1 : edit.end_line] = replacement.splitlines(keepends=True)
    return (
        "".join(lines),
        {
            "path": relative_path,
            "relative_path": relative_path,
            "action": "structured_replace_lines",
            "start_line": edit.start_line,
            "end_line": edit.end_line,
            "find_chars": len(guard_text),
            "replace_chars": len(replacement),
            "replaced_lines": edit.end_line - edit.start_line + 1,
        },
    )


def _extract_json_payload(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None

    if "```" in stripped:
        parts = stripped.split("```")
        for index in range(1, len(parts), 2):
            candidate = parts[index]
            if candidate.lstrip().startswith(("json", "JSON")):
                candidate = candidate.split("\n", 1)[1] if "\n" in candidate else ""
            payload = _decode_first_json(candidate.strip())
            if payload is not None:
                return payload

    return _decode_first_json(stripped)


def _decode_first_json(text: str) -> Any:
    decoder = json.JSONDecoder()
    starts = [pos for pos in (text.find("{"), text.find("[")) if pos != -1]
    for start in sorted(starts):
        try:
            payload, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        return payload
    return None


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _line_range_fields(item: dict[str, Any]) -> tuple[int | None, int | None]:
    range_value = item.get("line_range")
    if isinstance(range_value, list | tuple) and len(range_value) == 2:
        return _optional_int(range_value[0]), _optional_int(range_value[1])

    start = _first_present(item, ("start_line", "line_start", "startLine"))
    end = _first_present(item, ("end_line", "line_end", "endLine"))
    return _optional_int(start), _optional_int(end)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_relative_path(path_text: str) -> Path | None:
    normalized = path_text.replace("\\", "/").strip()
    if not normalized:
        return None
    path = Path(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _convert_newlines_like(text: str, content: str) -> str:
    if "\r\n" in content:
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _dominant_newline(content: str) -> str:
    return "\r\n" if "\r\n" in content else "\n"


def _ends_with_newline(text: str) -> bool:
    return text.endswith(("\n", "\r\n"))


def _nearest_snippet_hint(content: str, find_text: str) -> str:
    find_lines = [line for line in find_text.splitlines() if line.strip()]
    content_lines = content.splitlines()
    if not find_lines or not content_lines:
        return ""

    informative_lines = [
        line
        for line in find_lines
        if len(line.strip()) >= 8 and any(char.isalnum() or char == "_" for char in line)
    ]
    if not informative_lines:
        return ""
    anchors = sorted(informative_lines, key=lambda line: len(line.strip()), reverse=True)[:3]
    best_score = 0.0
    best_index: int | None = None
    for index, line in enumerate(content_lines):
        if not line.strip():
            continue
        score = max(
            difflib.SequenceMatcher(None, anchor.strip(), line.strip()).ratio()
            for anchor in anchors
        )
        if score > best_score:
            best_score = score
            best_index = index

    if best_index is None or best_score < 0.55:
        return ""

    start = max(0, best_index - 2)
    end = min(len(content_lines), best_index + 3)
    snippet = "\n".join(
        f"L{line_number:04d}: {line}"
        for line_number, line in enumerate(content_lines[start:end], start=start + 1)
    )
    if len(snippet) > 1200:
        snippet = snippet[:1200] + "\n...<truncated>..."
    failed_find = find_text.strip()
    if len(failed_find) > 800:
        failed_find = failed_find[:800] + "\n...<truncated>..."
    return (
        "\nFailed find text:\n"
        f"{failed_find}\n"
        f"Closest existing snippet (lines {start + 1}-{end}):\n"
        f"{snippet}"
    )
