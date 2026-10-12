"""Locked aggregate API-cost authority for a bounded v4 diagnostic smoke."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from self_evolving_agent.smp_artifacts import atomic_write_json
from self_evolving_agent.smp_contracts import canonical_hash
from self_evolving_agent.smp_protocol import FrameworkInvariantError, MAX_DIAGNOSTIC_COST_USD


class ProtocolCostLedger:
    def __init__(self, path: str | Path, *, cap_usd: float):
        if isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float)) or not math.isfinite(float(cap_usd)) or not 0 < cap_usd <= MAX_DIAGNOSTIC_COST_USD:
            raise FrameworkInvariantError("protocol_cost_cap_invalid")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.cap_usd = float(cap_usd)
        if not self.path.is_file():
            payload = {"schema_version": "openmemgov.cost_ledger.v4", "cap_usd": self.cap_usd, "actual_cost_usd": 0.0, "reservations": {}}
            atomic_write_json(self.path, {**payload, "ledger_hash": canonical_hash(payload)})

    def reserve(self, *, reservation_id: str, stage: str, maximum_cost_usd: float) -> None:
        if not isinstance(reservation_id, str) or not reservation_id or not isinstance(stage, str) or not stage:
            raise FrameworkInvariantError("protocol_cost_reservation_invalid")
        if isinstance(maximum_cost_usd, bool) or not isinstance(maximum_cost_usd, (int, float)) or not math.isfinite(float(maximum_cost_usd)) or maximum_cost_usd < 0:
            raise FrameworkInvariantError("protocol_cost_reservation_invalid")
        self._lock()
        try:
            ledger = self._load()
            if reservation_id in ledger["reservations"]:
                raise FrameworkInvariantError("protocol_cost_reservation_exists")
            open_cost = sum(float(row["maximum_cost_usd"]) for row in ledger["reservations"].values() if row["state"] == "reserved")
            if ledger["actual_cost_usd"] + open_cost + maximum_cost_usd > self.cap_usd + 1e-12:
                raise FrameworkInvariantError("protocol_aggregate_cost_cap_would_be_exceeded")
            ledger["reservations"][reservation_id] = {"stage": stage, "maximum_cost_usd": float(maximum_cost_usd), "state": "reserved", "actual_cost_usd": None}
            self._write(ledger)
        finally:
            self._unlock()

    def reconcile(self, *, reservation_id: str, actual_cost_usd: float) -> None:
        if isinstance(actual_cost_usd, bool) or not isinstance(actual_cost_usd, (int, float)) or not math.isfinite(float(actual_cost_usd)):
            raise FrameworkInvariantError("protocol_stage_cost_exceeded_reservation")
        self._lock()
        try:
            ledger = self._load()
            row = ledger["reservations"].get(reservation_id)
            if not isinstance(row, Mapping) or row.get("state") != "reserved":
                raise FrameworkInvariantError("protocol_cost_reservation_not_open")
            if actual_cost_usd < 0 or actual_cost_usd > float(row["maximum_cost_usd"]) + 1e-12:
                raise FrameworkInvariantError("protocol_stage_cost_exceeded_reservation")
            ledger["actual_cost_usd"] += float(actual_cost_usd)
            row.update({"state": "reconciled", "actual_cost_usd": float(actual_cost_usd)})
            if ledger["actual_cost_usd"] > self.cap_usd + 1e-12:
                raise FrameworkInvariantError("protocol_aggregate_cost_cap_exceeded")
            self._write(ledger)
        finally:
            self._unlock()

    def snapshot(self) -> dict[str, Any]:
        value = self._load()
        return {**value, "ledger_hash": canonical_hash(value)}

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FrameworkInvariantError("protocol_cost_ledger_invalid") from exc
        if not isinstance(value, dict):
            raise FrameworkInvariantError("protocol_cost_ledger_invalid")
        claimed = value.pop("ledger_hash", "")
        reservations = value.get("reservations")
        actual = value.get("actual_cost_usd")
        structurally_valid = (
            isinstance(reservations, dict)
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and math.isfinite(float(actual))
            and 0 <= float(actual) <= self.cap_usd + 1e-12
            and all(
                isinstance(key, str)
                and key
                and isinstance(row, dict)
                and isinstance(row.get("stage"), str)
                and bool(row.get("stage"))
                and row.get("state") in {"reserved", "reconciled"}
                and isinstance(row.get("maximum_cost_usd"), (int, float))
                and not isinstance(row.get("maximum_cost_usd"), bool)
                and math.isfinite(float(row["maximum_cost_usd"]))
                and float(row["maximum_cost_usd"]) >= 0
                and (
                    (row["state"] == "reserved" and row.get("actual_cost_usd") is None)
                    or (
                        row["state"] == "reconciled"
                        and isinstance(row.get("actual_cost_usd"), (int, float))
                        and not isinstance(row.get("actual_cost_usd"), bool)
                        and math.isfinite(float(row["actual_cost_usd"]))
                        and 0 <= float(row["actual_cost_usd"]) <= float(row["maximum_cost_usd"]) + 1e-12
                    )
                )
                for key, row in reservations.items()
            )
        )
        reconciled_total = sum(float(row["actual_cost_usd"]) for row in reservations.values() if row["state"] == "reconciled") if structurally_valid else -1.0
        if (
            value.get("schema_version") != "openmemgov.cost_ledger.v4"
            or value.get("cap_usd") != self.cap_usd
            or claimed != canonical_hash(value)
            or not structurally_valid
            or abs(float(actual) - reconciled_total) > 1e-12
        ):
            raise FrameworkInvariantError("protocol_cost_ledger_invalid")
        return value

    def _write(self, ledger: Mapping[str, Any]) -> None:
        payload = dict(ledger)
        atomic_write_json(self.path, {**payload, "ledger_hash": canonical_hash(payload)})

    def _lock(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise FrameworkInvariantError("protocol_cost_ledger_locked") from exc
        os.close(descriptor)

    def _unlock(self) -> None:
        self.lock_path.unlink(missing_ok=True)
