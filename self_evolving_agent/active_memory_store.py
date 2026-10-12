"""M6.5 active memory store: thin I/O, pure semantics.

This module materializes Memory-4 scoped operation proposals into an auditable
active-memory state. M6.5 intentionally does not implement retrieval, prompt
injection, optimizer integration, or consolidation policy.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from self_evolving_agent.memory_operations import (
    MemoryOperationValidationError,
    SCOPED_MEMORY_OPERATIONS,
    validate_scoped_memory_operation,
    validate_active_memory_item,
)


ACTIVE_MEMORY_STORE_SCHEMA_VERSION = "m6.5.active_memory_store.v1"
ACTIVE_MEMORY_STORE_ITEM_SCHEMA_VERSION = "m6.5.active_memory_store_item.v1"
MEMORY_OPERATION_APPLICATION_SCHEMA_VERSION = (
    "m6.5.memory_operation_application.v1"
)

APPLICATION_STATUSES = ("applied", "idempotent_noop")


class ActiveMemoryStoreValidationError(ValueError):
    """Raised when an active-memory store or application record is malformed."""


def build_empty_active_memory_store(
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build and validate an empty M6.5 active-memory store."""

    now = created_at or _utc_now()
    store = {
        "schema_version": ACTIVE_MEMORY_STORE_SCHEMA_VERSION,
        "created_at": now,
        "updated_at": now,
        "memories": [],
        "applied_operation_ids": [],
    }
    validate_active_memory_store(store)
    return store


def validate_active_memory_store(store: dict[str, Any]) -> None:
    """Validate a materialized active-memory store."""

    if not isinstance(store, dict):
        raise ActiveMemoryStoreValidationError("store must be a mapping")
    for key in (
        "schema_version",
        "created_at",
        "updated_at",
        "memories",
        "applied_operation_ids",
    ):
        if key not in store:
            raise ActiveMemoryStoreValidationError(f"missing store field: {key}")
    if store["schema_version"] != ACTIVE_MEMORY_STORE_SCHEMA_VERSION:
        raise ActiveMemoryStoreValidationError("unsupported store schema_version")
    for key in ("created_at", "updated_at"):
        if not isinstance(store[key], str) or not store[key]:
            raise ActiveMemoryStoreValidationError(f"{key} must be a non-empty string")
    if not isinstance(store["memories"], list):
        raise ActiveMemoryStoreValidationError("memories must be a list")
    _validate_string_list(store["applied_operation_ids"], "applied_operation_ids")
    _assert_unique_strings(store["applied_operation_ids"], "applied_operation_ids")

    memory_ids: list[str] = []
    wrapper_operation_ids: list[str] = []
    for wrapper in store["memories"]:
        validate_active_memory_store_item(wrapper)
        memory_id = str(wrapper["memory"]["memory_id"])
        memory_ids.append(memory_id)
        wrapper_operation_ids.extend(
            _string_list(wrapper["store_metadata"].get("applied_operation_ids", []))
        )
    _assert_unique_strings(memory_ids, "memory.memory_id")

    applied = set(store["applied_operation_ids"])
    missing = sorted({op_id for op_id in wrapper_operation_ids if op_id not in applied})
    if missing:
        raise ActiveMemoryStoreValidationError(
            "store applied_operation_ids missing wrapper operation ids"
        )


def validate_active_memory_store_item(wrapper: dict[str, Any]) -> None:
    """Validate a wrapper around an M6.4 active-memory item."""

    if not isinstance(wrapper, dict):
        raise ActiveMemoryStoreValidationError("store item must be a mapping")
    for key in ("schema_version", "memory", "store_metadata"):
        if key not in wrapper:
            raise ActiveMemoryStoreValidationError(f"missing store item field: {key}")
    if wrapper["schema_version"] != ACTIVE_MEMORY_STORE_ITEM_SCHEMA_VERSION:
        raise ActiveMemoryStoreValidationError(
            "unsupported active memory store item schema_version"
        )
    if not isinstance(wrapper["memory"], dict):
        raise ActiveMemoryStoreValidationError("memory must be a mapping")
    try:
        validate_active_memory_item(wrapper["memory"])
    except MemoryOperationValidationError as exc:
        raise ActiveMemoryStoreValidationError(str(exc)) from exc

    metadata = wrapper["store_metadata"]
    if not isinstance(metadata, dict):
        raise ActiveMemoryStoreValidationError("store_metadata must be a mapping")
    for key in (
        "first_seen_at",
        "last_updated_at",
        "last_operation_id",
        "applied_operation_ids",
        "supersedes_memory_ids",
        "suppressed_by",
    ):
        if key not in metadata:
            raise ActiveMemoryStoreValidationError(f"missing store metadata field: {key}")
    for key in ("first_seen_at", "last_updated_at", "last_operation_id"):
        if not isinstance(metadata[key], str) or not metadata[key]:
            raise ActiveMemoryStoreValidationError(
                f"store_metadata.{key} must be a non-empty string"
            )
    _validate_string_list(
        metadata["applied_operation_ids"],
        "store_metadata.applied_operation_ids",
    )
    _validate_string_list(
        metadata["supersedes_memory_ids"],
        "store_metadata.supersedes_memory_ids",
    )
    _assert_unique_strings(
        metadata["applied_operation_ids"],
        "store_metadata.applied_operation_ids",
    )
    _assert_unique_strings(
        metadata["supersedes_memory_ids"],
        "store_metadata.supersedes_memory_ids",
    )
    if metadata["last_operation_id"] not in metadata["applied_operation_ids"]:
        raise ActiveMemoryStoreValidationError(
            "last_operation_id must be included in applied_operation_ids"
        )
    suppressed_by = metadata["suppressed_by"]
    if suppressed_by is not None and not isinstance(suppressed_by, str):
        raise ActiveMemoryStoreValidationError("suppressed_by must be null or string")


def validate_memory_operation_application(record: dict[str, Any]) -> None:
    """Validate an append-only Memory-4 operation application record."""

    if not isinstance(record, dict):
        raise ActiveMemoryStoreValidationError("application record must be a mapping")
    for key in (
        "schema_version",
        "application_id",
        "applied_at",
        "operation_id",
        "operation",
        "candidate_id",
        "target_memory_ids",
        "result_memory_id",
        "status",
        "before_memory_ids",
        "after_memory_ids",
        "removed_memory_ids",
        "result_store_item",
        "provenance",
        "rationale",
        "safety_flags",
    ):
        if key not in record:
            raise ActiveMemoryStoreValidationError(
                f"missing application field: {key}"
            )
    if record["schema_version"] != MEMORY_OPERATION_APPLICATION_SCHEMA_VERSION:
        raise ActiveMemoryStoreValidationError(
            "unsupported application schema_version"
        )
    if record["operation"] not in SCOPED_MEMORY_OPERATIONS:
        raise ActiveMemoryStoreValidationError("invalid application operation")
    if record["status"] not in APPLICATION_STATUSES:
        raise ActiveMemoryStoreValidationError("invalid application status")
    for key in (
        "application_id",
        "applied_at",
        "operation_id",
        "operation",
        "candidate_id",
        "result_memory_id",
        "rationale",
    ):
        if not isinstance(record[key], str):
            raise ActiveMemoryStoreValidationError(f"{key} must be a string")
    for key in (
        "target_memory_ids",
        "before_memory_ids",
        "after_memory_ids",
        "removed_memory_ids",
        "safety_flags",
    ):
        _validate_string_list(record[key], key)
    if not isinstance(record["provenance"], dict):
        raise ActiveMemoryStoreValidationError("provenance must be a mapping")
    for key in (
        "source_events",
        "candidate_ids",
        "operation_created_at",
        "evidence_summary",
        "scope_decision",
    ):
        if key not in record["provenance"]:
            raise ActiveMemoryStoreValidationError(f"missing provenance field: {key}")
    _validate_string_list(record["provenance"]["source_events"], "provenance.source_events")
    _validate_string_list(record["provenance"]["candidate_ids"], "provenance.candidate_ids")
    if not isinstance(record["provenance"]["operation_created_at"], str):
        raise ActiveMemoryStoreValidationError(
            "provenance.operation_created_at must be a string"
        )
    for key in ("evidence_summary", "scope_decision"):
        if not isinstance(record["provenance"][key], dict):
            raise ActiveMemoryStoreValidationError(f"provenance.{key} must be a mapping")

    if record["status"] == "applied":
        if not record["result_memory_id"]:
            raise ActiveMemoryStoreValidationError(
                "applied record must include result_memory_id"
            )
        if not isinstance(record["result_store_item"], dict):
            raise ActiveMemoryStoreValidationError(
                "applied record must include result_store_item"
            )
        validate_active_memory_store_item(record["result_store_item"])
        memory_id = record["result_store_item"]["memory"]["memory_id"]
        if record["result_memory_id"] != memory_id:
            raise ActiveMemoryStoreValidationError(
                "result_memory_id does not match result_store_item"
            )
        if record["result_memory_id"] not in record["after_memory_ids"]:
            raise ActiveMemoryStoreValidationError(
                "after_memory_ids must include result_memory_id"
            )
    else:
        if record["result_memory_id"] != "":
            raise ActiveMemoryStoreValidationError(
                "idempotent_noop must have empty result_memory_id"
            )
        if record["result_store_item"] is not None:
            raise ActiveMemoryStoreValidationError(
                "idempotent_noop must have null result_store_item"
            )
        if record["removed_memory_ids"]:
            raise ActiveMemoryStoreValidationError(
                "idempotent_noop must not remove memories"
            )

    if record["application_id"] != memory_operation_application_id(record):
        raise ActiveMemoryStoreValidationError("application_id does not match payload")


def apply_scoped_memory_operation(
    store: dict[str, Any],
    operation: dict[str, Any],
    applied_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one Memory-4 operation to an active-memory store.

    The input store is never mutated. The returned application record is the
    append-only audit event for this pure state transition.
    """

    validate_active_memory_store(store)
    try:
        validate_scoped_memory_operation(operation)
    except MemoryOperationValidationError as exc:
        raise ActiveMemoryStoreValidationError(str(exc)) from exc

    now = applied_at or _utc_now()
    working = _jsonable(deepcopy(store))
    operation_id = str(operation["operation_id"])
    if operation_id in set(working["applied_operation_ids"]):
        record = _application_record(
            store=working,
            operation=operation,
            applied_at=now,
            status="idempotent_noop",
            result_wrapper=None,
            removed_memory_ids=[],
        )
        validate_memory_operation_application(record)
        return working, record

    memories_by_id = _memory_map(working)
    before_ids = _memory_ids(working)
    result_memory = deepcopy(operation["after_state"])
    result_id = str(result_memory["memory_id"])
    target_ids = _string_list(operation.get("target_memory_ids", []))
    removed_ids = _removed_memory_ids(
        operation=str(operation["operation"]),
        target_memory_ids=target_ids,
        result_memory_id=result_id,
    )

    for memory_id in removed_ids:
        memories_by_id.pop(memory_id, None)

    result_wrapper = _store_item_from_operation(
        operation=operation,
        memory=result_memory,
        existing_wrapper=memories_by_id.get(result_id),
        target_wrappers=[
            wrapper
            for memory_id, wrapper in _memory_map(working).items()
            if memory_id in target_ids
        ],
        removed_memory_ids=removed_ids,
        applied_at=now,
    )
    memories_by_id[result_id] = result_wrapper

    new_store = {
        **working,
        "updated_at": now,
        "memories": _sorted_wrappers(memories_by_id.values()),
        "applied_operation_ids": _dedupe(
            _string_list(working.get("applied_operation_ids", [])) + [operation_id]
        ),
    }
    validate_active_memory_store(new_store)

    record = _application_record(
        store=working,
        operation=operation,
        applied_at=now,
        status="applied",
        result_wrapper=result_wrapper,
        removed_memory_ids=removed_ids,
        after_memory_ids=_memory_ids(new_store),
        before_memory_ids=before_ids,
    )
    validate_memory_operation_application(record)
    return new_store, record


def apply_scoped_memory_operations(
    store: dict[str, Any],
    operations: list[dict[str, Any]],
    applied_at: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply a batch of Memory-4 operations in order."""

    working = _jsonable(deepcopy(store))
    records: list[dict[str, Any]] = []
    for operation in operations or []:
        working, record = apply_scoped_memory_operation(
            working,
            operation,
            applied_at=applied_at,
        )
        records.append(record)
    return working, records


def replay_memory_operation_applications(
    application_records: list[dict[str, Any]],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Rebuild active-memory state from append-only application records."""

    records = [_jsonable(deepcopy(record)) for record in application_records or []]
    initial_created_at = created_at or _first_record_time(records) or _utc_now()
    store = build_empty_active_memory_store(created_at=initial_created_at)
    memories_by_id: dict[str, dict[str, Any]] = {}
    applied_operation_ids: list[str] = []
    seen_operations: set[str] = set()

    for record in records:
        validate_memory_operation_application(record)
        operation_id = str(record["operation_id"])
        if operation_id in seen_operations:
            continue
        if record["status"] == "idempotent_noop":
            seen_operations.add(operation_id)
            continue

        for memory_id in _string_list(record.get("removed_memory_ids", [])):
            memories_by_id.pop(memory_id, None)
        result_wrapper = _jsonable(deepcopy(record["result_store_item"]))
        validate_active_memory_store_item(result_wrapper)
        memories_by_id[str(result_wrapper["memory"]["memory_id"])] = result_wrapper
        seen_operations.add(operation_id)
        applied_operation_ids.append(operation_id)
        store["updated_at"] = str(record["applied_at"])

    store["memories"] = _sorted_wrappers(memories_by_id.values())
    store["applied_operation_ids"] = _dedupe(applied_operation_ids)
    validate_active_memory_store(store)
    return store


def read_active_memory_store(path: str | Path) -> dict[str, Any]:
    """Read and validate active_memory.json."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_active_memory_store(payload)
    return payload


def write_active_memory_store(path: str | Path, store: dict[str, Any]) -> None:
    """Atomically write active_memory.json after validation."""

    validate_active_memory_store(store)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)
        json.dump(store, handle, sort_keys=True, indent=2)
        handle.write("\n")
    tmp_path.replace(target)


def read_memory_operation_applications(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate JSONL memory operation application records."""

    target = Path(path)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ActiveMemoryStoreValidationError(
                f"invalid application JSONL at line {line_number}"
            ) from exc
        validate_memory_operation_application(record)
        records.append(record)
    return records


def append_memory_operation_application(
    path: str | Path,
    record: dict[str, Any],
    *,
    append_noop: bool = False,
) -> bool:
    """Append one validated application record to JSONL.

    Returns False when an idempotent_noop record is intentionally not appended.
    """

    validate_memory_operation_application(record)
    if record["status"] == "idempotent_noop" and not append_noop:
        return False
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
    return True


def active_memory_store_fingerprint(store: dict[str, Any]) -> str:
    """Return a deterministic fingerprint for a valid active-memory store."""

    validate_active_memory_store(store)
    payload = _jsonable(deepcopy(store))
    return _stable_hash(payload)


def memory_operation_application_id(record: dict[str, Any]) -> str:
    """Return a deterministic id for an application-like payload."""

    payload = _jsonable(deepcopy(record))
    if isinstance(payload, dict):
        payload.pop("application_id", None)
        payload.pop("applied_at", None)
    return f"memapp_{_stable_hash(payload)[:20]}"


def _store_item_from_operation(
    *,
    operation: dict[str, Any],
    memory: dict[str, Any],
    existing_wrapper: dict[str, Any] | None,
    target_wrappers: list[dict[str, Any]],
    removed_memory_ids: list[str],
    applied_at: str,
) -> dict[str, Any]:
    operation_id = str(operation["operation_id"])
    target_metadata = [
        wrapper.get("store_metadata", {})
        for wrapper in target_wrappers
        if isinstance(wrapper, dict)
    ]
    existing_metadata = (
        existing_wrapper.get("store_metadata", {})
        if isinstance(existing_wrapper, dict)
        else {}
    )
    first_seen = (
        str(existing_metadata.get("first_seen_at") or "")
        or next(
            (
                str(metadata.get("first_seen_at") or "")
                for metadata in target_metadata
                if metadata.get("first_seen_at")
            ),
            "",
        )
        or applied_at
    )
    applied_ids = _dedupe(
        _string_list(existing_metadata.get("applied_operation_ids", []))
        + [
            op_id
            for metadata in target_metadata
            for op_id in _string_list(metadata.get("applied_operation_ids", []))
        ]
        + [operation_id]
    )
    supersedes = _dedupe(
        _string_list(existing_metadata.get("supersedes_memory_ids", []))
        + [
            memory_id
            for metadata in target_metadata
            for memory_id in _string_list(metadata.get("supersedes_memory_ids", []))
        ]
        + removed_memory_ids
    )
    wrapper = {
        "schema_version": ACTIVE_MEMORY_STORE_ITEM_SCHEMA_VERSION,
        "memory": _jsonable(deepcopy(memory)),
        "store_metadata": {
            "first_seen_at": first_seen,
            "last_updated_at": applied_at,
            "last_operation_id": operation_id,
            "applied_operation_ids": applied_ids,
            "supersedes_memory_ids": supersedes,
            "suppressed_by": (
                operation_id if operation["operation"] == "suppress" else None
            ),
        },
    }
    validate_active_memory_store_item(wrapper)
    return wrapper


def _application_record(
    *,
    store: dict[str, Any],
    operation: dict[str, Any],
    applied_at: str,
    status: str,
    result_wrapper: dict[str, Any] | None,
    removed_memory_ids: list[str],
    before_memory_ids: list[str] | None = None,
    after_memory_ids: list[str] | None = None,
) -> dict[str, Any]:
    result_memory_id = (
        str(result_wrapper["memory"]["memory_id"])
        if isinstance(result_wrapper, dict)
        else ""
    )
    record = {
        "schema_version": MEMORY_OPERATION_APPLICATION_SCHEMA_VERSION,
        "application_id": "",
        "applied_at": applied_at,
        "operation_id": str(operation["operation_id"]),
        "operation": str(operation["operation"]),
        "candidate_id": str(operation["candidate_id"]),
        "target_memory_ids": _string_list(operation.get("target_memory_ids", [])),
        "result_memory_id": result_memory_id,
        "status": status,
        "before_memory_ids": before_memory_ids or _memory_ids(store),
        "after_memory_ids": after_memory_ids or _memory_ids(store),
        "removed_memory_ids": _dedupe(removed_memory_ids),
        "result_store_item": _jsonable(deepcopy(result_wrapper))
        if result_wrapper is not None
        else None,
        "provenance": {
            "source_events": _string_list(operation.get("source_events", [])),
            "candidate_ids": _string_list(
                operation.get("after_state", {}).get("candidate_ids", [])
            )
            or [str(operation["candidate_id"])],
            "operation_created_at": str(operation.get("created_at") or ""),
            "evidence_summary": _jsonable(operation.get("evidence_summary", {})),
            "scope_decision": _jsonable(operation.get("scope_decision", {})),
        },
        "rationale": (
            "Operation already applied; no state change."
            if status == "idempotent_noop"
            else str(operation.get("rationale") or "")
        ),
        "safety_flags": _string_list(operation.get("safety_flags", [])),
    }
    record["application_id"] = memory_operation_application_id(record)
    return record


def _removed_memory_ids(
    *,
    operation: str,
    target_memory_ids: list[str],
    result_memory_id: str,
) -> list[str]:
    if operation in {"add", "narrow_scope", "promote_scope"}:
        return []
    if operation == "pin":
        return [memory_id for memory_id in target_memory_ids if memory_id != result_memory_id]
    if operation in {"reinforce", "merge", "contradict", "suppress"}:
        return [memory_id for memory_id in target_memory_ids if memory_id != result_memory_id]
    return []


def _memory_map(store: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(wrapper["memory"]["memory_id"]): _jsonable(deepcopy(wrapper))
        for wrapper in store.get("memories", [])
        if isinstance(wrapper, dict)
        and isinstance(wrapper.get("memory"), dict)
        and wrapper["memory"].get("memory_id")
    }


def _memory_ids(store: dict[str, Any]) -> list[str]:
    return sorted(_memory_map(store))


def _sorted_wrappers(wrappers: Any) -> list[dict[str, Any]]:
    return sorted(
        [_jsonable(deepcopy(wrapper)) for wrapper in wrappers],
        key=lambda wrapper: str(wrapper["memory"]["memory_id"]),
    )


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def _first_record_time(records: list[dict[str, Any]]) -> str:
    for record in records:
        applied_at = str(record.get("applied_at") or "")
        if applied_at:
            return applied_at
    return ""


def _validate_string_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list):
        raise ActiveMemoryStoreValidationError(f"{field_name} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise ActiveMemoryStoreValidationError(f"{field_name} must contain strings")


def _assert_unique_strings(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ActiveMemoryStoreValidationError(f"{field_name} must be unique")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
