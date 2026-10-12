"""Real-time, append-only progress reporting for long-running experiments."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, TextIO


TIMELINE_SCHEMA_VERSION = "sea.timeline.v1"


class ProgressReporter:
    """Emit human-readable progress and machine-readable JSONL events.

    Progress reporting is deliberately best-effort: an unavailable log path must
    never change an experiment result. Durations use a monotonic clock while
    timestamps use the local timezone for operator readability.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        log_path: str | Path | None = None,
        jsonl_path: str | Path | None = None,
        stream: TextIO | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.log_path = Path(log_path) if log_path else None
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self.stream = stream or sys.stderr
        self._started_at = perf_counter()

    def emit(self, event: str, *, status: str | None = None, **fields: Any) -> None:
        if not self.enabled:
            return
        now = datetime.now().astimezone()
        elapsed_s = round(max(0.0, perf_counter() - self._started_at), 3)
        record = {
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "timestamp": now.isoformat(timespec="seconds"),
            "event": str(event),
            "run_elapsed_s": elapsed_s,
            **_jsonable_fields(fields),
        }
        if status is not None:
            record["status"] = str(status)
        line = _human_line(record)
        try:
            print(line, file=self.stream, flush=True)
        except Exception:
            pass
        self._append_text(self.log_path, line + "\n")
        self._append_text(
            self.jsonl_path,
            json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n",
        )

    def _append_text(self, path: Path | None, text: str) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass


def _human_line(record: dict[str, Any]) -> str:
    elapsed = _format_duration(float(record["run_elapsed_s"]))
    timestamp = str(record["timestamp"])[11:19]
    event = str(record["event"]).replace("_", " ").upper()
    excluded = {"schema_version", "timestamp", "event", "run_elapsed_s"}
    details = " ".join(
        f"{key}={_display_value(value)}"
        for key, value in record.items()
        if key not in excluded and value is not None and value != ""
    )
    return f"[{timestamp}] [+{elapsed}] {event}" + (f" {details}" if details else "")


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _display_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple)):
        return f"[{','.join(str(item) for item in value)}]"
    return str(value).replace("\n", " ")


def _jsonable_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, Path):
            clean[str(key)] = str(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            clean[str(key)] = value
        elif isinstance(value, (list, tuple)):
            clean[str(key)] = [str(item) for item in value]
        else:
            clean[str(key)] = str(value)
    return clean
