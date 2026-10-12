"""Artifact helpers for M6 root-cause events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from self_evolving_agent.failure_cause import (
    FAILURE_CAUSE_EVENT_SCHEMA_VERSION,
    build_failure_cause_events,
)


ROOT_CAUSE_EVENTS_ARTIFACT_NAME = "root_cause_events.json"
ROOT_CAUSE_EVENTS_ARTIFACT_SCHEMA_VERSION = "m6.1.root_cause_events_artifact.v1"


def build_root_cause_events_artifact(
    records: list[dict[str, Any]],
    *,
    source: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic root-cause events artifact payload."""

    events = build_failure_cause_events(records)
    return {
        "schema_version": ROOT_CAUSE_EVENTS_ARTIFACT_SCHEMA_VERSION,
        "event_schema_version": FAILURE_CAUSE_EVENT_SCHEMA_VERSION,
        "source": str(source or ""),
        "metadata": _jsonable_mapping(metadata),
        "event_count": len(events),
        "events": events,
    }


def write_root_cause_events_artifact(
    path: str | Path,
    records: list[dict[str, Any]],
    *,
    source: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write root-cause events to ``path`` and return the written payload."""

    payload = build_root_cause_events_artifact(
        records,
        source=source,
        metadata=metadata,
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _jsonable_mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _jsonable(item)
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    }


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
