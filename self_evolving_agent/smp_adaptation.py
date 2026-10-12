"""One-attempt registry and recoverable diagnostic adaptation transaction."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from self_evolving_agent.smp_artifacts import atomic_write_json
from self_evolving_agent.smp_contracts import canonical_hash
from self_evolving_agent.smp_protocol import FrameworkInvariantError, PROTOCOL_VERSION, StudyEligibility


@dataclass(frozen=True)
class AdaptationAttemptBinding:
    manifest_hash: str
    runtime_bundle_hash: str
    task_identity_hash: str
    seal_hash: str
    official_outcome_hash: str
    extractor_binding_hash: str
    attempt_id: str
    retry_policy_hash: str
    schema_version: str = "openmemgov.adaptation_attempt.v4"

    @property
    def semantic_tuple_hash(self) -> str:
        payload = asdict(self)
        payload.pop("attempt_id")
        payload.pop("retry_policy_hash")
        payload.pop("schema_version")
        return canonical_hash(payload)

    @property
    def binding_hash(self) -> str:
        return canonical_hash(asdict(self))


class AdaptationAttemptRegistry:
    """Durably reserves the semantic attempt before the provider is called."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def reserve(self, binding: AdaptationAttemptBinding) -> Path:
        if not binding.attempt_id or Path(binding.attempt_id).name != binding.attempt_id:
            raise FrameworkInvariantError("invalid_adaptation_attempt_id")
        path = self.root / f"{binding.semantic_tuple_hash[7:]}.json"
        record = {
            "schema_version": binding.schema_version,
            "state": "reserved_before_provider_call",
            "binding": asdict(binding),
            "binding_hash": binding.binding_hash,
            "semantic_tuple_hash": binding.semantic_tuple_hash,
            "retry_count": 0,
            "retry_policy_hash": binding.retry_policy_hash,
        }
        try:
            _exclusive_json(path, record)
        except FrameworkInvariantError as exc:
            raise FrameworkInvariantError("semantic_adaptation_attempt_already_reserved") from exc
        return path

    def finalize(self, binding: AdaptationAttemptBinding, *, retry_count: int, status: str) -> None:
        path = self.root / f"{binding.semantic_tuple_hash[7:]}.json"
        if not path.is_file():
            raise FrameworkInvariantError("adaptation_attempt_not_reserved")
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("binding_hash") != binding.binding_hash or row.get("state") != "reserved_before_provider_call":
            raise FrameworkInvariantError("adaptation_attempt_registry_inconsistent")
        if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
            raise FrameworkInvariantError("adaptation_retry_count_invalid")
        row.update({"state": status, "retry_count": retry_count})
        row["record_hash"] = canonical_hash(row)
        atomic_write_json(path, row)


def _exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise FrameworkInvariantError("adaptation_attempt_id_already_exists") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def empty_diagnostic_ledger() -> dict[str, Any]:
    eligibility = asdict(StudyEligibility(protocol_conformant=True))
    payload = {
        "schema_version": "openmemgov.disposable_diagnostic_ledger.v4",
        "protocol_version": PROTOCOL_VERSION,
        "diagnostic_only": True,
        "eligibility": eligibility,
        "candidates": [],
        "transactions": [],
    }
    return {**payload, "ledger_hash": canonical_hash(payload)}


def validate_diagnostic_ledger(value: Mapping[str, Any]) -> dict[str, Any]:
    if (
        value.get("schema_version") != "openmemgov.disposable_diagnostic_ledger.v4"
        or value.get("protocol_version") != PROTOCOL_VERSION
        or value.get("diagnostic_only") is not True
    ):
        raise FrameworkInvariantError("production_ledger_untargetable")
    payload = {key: child for key, child in value.items() if key != "ledger_hash"}
    if value.get("ledger_hash") != canonical_hash(payload):
        raise FrameworkInvariantError("diagnostic_ledger_hash_mismatch")
    StudyEligibility(**dict(value.get("eligibility", {})))
    return dict(value)


class AdaptationTransaction:
    """Filesystem CAS transaction whose commit marker is the visibility boundary."""

    def __init__(self, root: str | Path, *, transaction_id: str, ledger_path: str | Path):
        if not transaction_id or Path(transaction_id).name != transaction_id:
            raise FrameworkInvariantError("invalid_adaptation_transaction_id")
        self.root = Path(root)
        self.transaction_id = transaction_id
        self.ledger_path = Path(ledger_path)
        self.stage_dir = self.root / ".staging" / transaction_id
        self.commit_dir = self.root / "committed" / transaction_id
        self.lock_path = self.root / ".adaptation.lock"

    def stage(
        self,
        *,
        expected_previous_ledger_hash: str,
        adaptation_artifact: Mapping[str, Any],
        candidate: Mapping[str, Any] | None,
    ) -> None:
        if self.stage_dir.exists() or self.commit_dir.exists():
            raise FrameworkInvariantError("adaptation_transaction_already_staged")
        current = self._load_ledger()
        if current["ledger_hash"] != expected_previous_ledger_hash:
            raise FrameworkInvariantError("ledger_compare_and_swap_conflict")
        next_payload = {key: child for key, child in current.items() if key != "ledger_hash"}
        if candidate is not None:
            bound_candidate = {**dict(candidate), "transaction_id": self.transaction_id}
            next_payload["candidates"] = [*next_payload["candidates"], bound_candidate]
        adaptation_hash = canonical_hash(adaptation_artifact)
        candidate_hash = canonical_hash(candidate) if candidate is not None else ""
        if candidate is not None:
            next_payload["transactions"] = [
                *next_payload["transactions"],
                {
                    "transaction_id": self.transaction_id,
                    "adaptation_artifact_hash": adaptation_hash,
                    "candidate_hash": candidate_hash,
                },
            ]
            next_ledger = {**next_payload, "ledger_hash": canonical_hash(next_payload)}
        else:
            next_ledger = current
        self.stage_dir.mkdir(parents=True)
        atomic_write_json(self.stage_dir / "adaptation.json", dict(adaptation_artifact))
        atomic_write_json(self.stage_dir / "ledger.next.json", next_ledger)
        atomic_write_json(
            self.stage_dir / "transaction.json",
            {
                "schema_version": "openmemgov.adaptation_transaction.v4",
                "transaction_id": self.transaction_id,
                "expected_previous_ledger_hash": expected_previous_ledger_hash,
                "next_ledger_hash": next_ledger["ledger_hash"],
                "adaptation_artifact_hash": adaptation_hash,
                "candidate_hash": candidate_hash,
            },
        )
        _fsync_directory(self.stage_dir)

    def commit(self, *, fault: str = "") -> dict[str, Any]:
        if fault == "before_ledger_compare_and_swap":
            raise InterruptedError(fault)
        self._acquire_lock()
        try:
            transaction = _object(self.stage_dir / "transaction.json")
            current = self._load_ledger()
            next_ledger = validate_diagnostic_ledger(_object(self.stage_dir / "ledger.next.json"))
            if current["ledger_hash"] == transaction["expected_previous_ledger_hash"]:
                if fault == "ledger_hash_conflict":
                    raise FrameworkInvariantError("ledger_compare_and_swap_conflict")
                if transaction.get("candidate_hash"):
                    _replace_json_durable(self.ledger_path, next_ledger)
            elif current["ledger_hash"] != transaction["next_ledger_hash"]:
                raise FrameworkInvariantError("ledger_compare_and_swap_conflict")
            if fault == "before_commit_marker":
                raise InterruptedError(fault)
            self.commit_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.stage_dir, self.commit_dir)
            if fault == "after_commit_directory_rename":
                raise InterruptedError(fault)
            return self._finish_marker(transaction, next_ledger)
        finally:
            self.lock_path.unlink(missing_ok=True)

    def recover(self) -> dict[str, Any] | None:
        if (self.commit_dir / "commit.json").is_file():
            return _object(self.commit_dir / "commit.json")
        if self.commit_dir.is_dir():
            transaction = _object(self.commit_dir / "transaction.json")
            ledger = self._load_ledger()
            if ledger["ledger_hash"] != transaction["next_ledger_hash"]:
                raise FrameworkInvariantError("adaptation_recovery_ledger_mismatch")
            return self._finish_marker(transaction, ledger)
        if not self.stage_dir.is_dir():
            return None
        return self.commit()

    def visible_candidates(self) -> tuple[Mapping[str, Any], ...]:
        ledger = self._load_ledger()
        committed = {
            path.parent.name
            for path in (self.root / "committed").glob("*/commit.json")
            if _valid_commit_marker(path)
        }
        return tuple(row for row in ledger["candidates"] if row.get("transaction_id") in committed)

    def _finish_marker(self, transaction: Mapping[str, Any], next_ledger: Mapping[str, Any]) -> dict[str, Any]:
        marker = {
            **transaction,
            "status": "committed",
            "ledger_hash": next_ledger["ledger_hash"],
        }
        marker["commit_hash"] = canonical_hash(marker)
        atomic_write_json(self.commit_dir / "commit.json", marker)
        _fsync_directory(self.commit_dir)
        _fsync_directory(self.commit_dir.parent)
        return marker

    def _load_ledger(self) -> dict[str, Any]:
        if not self.ledger_path.is_file():
            raise FrameworkInvariantError("diagnostic_ledger_must_be_precreated")
        return validate_diagnostic_ledger(_object(self.ledger_path))

    def _acquire_lock(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise FrameworkInvariantError("adaptation_transaction_writer_locked") from exc
        os.close(descriptor)


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise FrameworkInvariantError("adaptation_transaction_artifact_malformed")
    return dict(value)


def _replace_json_durable(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _valid_commit_marker(path: Path) -> bool:
    try:
        row = _object(path)
    except (OSError, ValueError, json.JSONDecodeError, FrameworkInvariantError):
        return False
    claimed = row.pop("commit_hash", "")
    return row.get("status") == "committed" and claimed == canonical_hash(row)
