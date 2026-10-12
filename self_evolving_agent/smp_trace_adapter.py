"""Sanitized mini-SWE action/observation to EvidenceEvent production adapter."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from self_evolving_agent.smp_contracts import (
    EvidenceEvent,
    EvidenceSource,
    EvidenceType,
    LeakageClass,
    TriggerCategory,
    canonical_hash,
)


_SECRET = re.compile(
    r"(?i)(authorization\s*[:=]\s*\S+|bearer\s+\S+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+|\bsk-[A-Za-z0-9_-]{8,}\b|gh[pousr]_[A-Za-z0-9_]+)"
)
_FORBIDDEN = re.compile(r"(?i)(gold[_ -]?patch|hidden[_ -]?tests?|reference[_ -]?patch)")
_THOUGHT_LINE = re.compile(r"(?im)^\s*(?:thought|reasoning)\s*:.*$")


def sanitize_observable(value: Any, *, limit: int = 4000) -> str:
    text = str(value or "").replace("\x00", "")
    text = _SECRET.sub("[REDACTED]", text)
    text = _FORBIDDEN.sub("[FORBIDDEN_REFERENCE_REDACTED]", text)
    text = _THOUGHT_LINE.sub("[MODEL_REASONING_REDACTED]", text)
    return text[:limit]


def sanitize_persistent_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): sanitize_persistent_payload(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_persistent_payload(child) for child in value]
    if isinstance(value, str):
        return sanitize_observable(value, limit=max(4000, len(value)))
    return value


class MiniTraceAdapter:
    """Map only executed actions and formatted observations; never assistant THOUGHT."""

    version = "smp.mini_trace_adapter.v1"

    def __call__(
        self,
        message: Mapping[str, Any],
        observations: list[dict[str, Any]],
        runtime: Any,
    ) -> Iterable[tuple[EvidenceEvent, TriggerCategory | None, str]]:
        actions = message.get("extra", {}).get("actions", []) if isinstance(message, Mapping) else []
        if not isinstance(actions, list):
            actions = []
        rows: list[tuple[EvidenceEvent, TriggerCategory | None, str]] = []
        for offset, observation in enumerate(observations):
            action = actions[offset] if offset < len(actions) and isinstance(actions[offset], Mapping) else {}
            command = sanitize_observable(action.get("command", ""), limit=1000)
            content = sanitize_observable(
                observation.get("content", observation.get("output", "")) if isinstance(observation, Mapping) else observation
            )
            extra = observation.get("extra", {}) if isinstance(observation, Mapping) else {}
            if not isinstance(extra, Mapping):
                extra = {}
            exit_code = extra.get("returncode", extra.get("exit_code", action.get("exit_code")))
            event_type, trigger = self._classify(command, content, exit_code, action)
            payload = {
                "command": command,
                "output": content,
                "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
                "adapter_version": self.version,
            }
            event = EvidenceEvent(
                task_id=runtime.state.task_id,
                event_index=len(runtime.state.evidence) + len(rows),
                event_type=event_type,
                source=(
                    EvidenceSource.EXECUTABLE_OUTPUT
                    if event_type in {EvidenceType.TEST_RESULT, EvidenceType.PATCH_APPLICATION}
                    else EvidenceSource.VISIBLE_CODE_PATCH
                ),
                observable=True,
                leakage_class=LeakageClass.PUBLIC_RUNTIME,
                payload=payload,
            )
            failure_hash = canonical_hash(payload) if trigger in {
                TriggerCategory.DIAGNOSTIC_TEST_FAILURE,
                TriggerCategory.PATCH_APPLICATION_FAILURE,
            } else ""
            rows.append((event, trigger, failure_hash))
        return tuple(rows)

    @staticmethod
    def _classify(
        command: str,
        output: str,
        exit_code: Any,
        action: Mapping[str, Any],
    ) -> tuple[EvidenceType, TriggerCategory | None]:
        lowered = f"{command}\n{output}".lower()
        if action.get("smp_memory_request") is True:
            return EvidenceType.EXPLICIT_MEMORY_REQUEST, TriggerCategory.EXPLICIT_MEMORY_REQUEST
        failed = (isinstance(exit_code, int) and exit_code != 0) or any(
            marker in lowered for marker in (" failed", "failure", "traceback", "error:")
        )
        if any(marker in command.lower() for marker in ("git apply", "patch ", "materializ")):
            return EvidenceType.PATCH_APPLICATION, TriggerCategory.PATCH_APPLICATION_FAILURE if failed else None
        if any(marker in command.lower() for marker in ("pytest", " test", "tox", "unittest")):
            return EvidenceType.TEST_RESULT, TriggerCategory.DIAGNOSTIC_TEST_FAILURE if failed else None
        if "git diff" in command.lower():
            return EvidenceType.PATCH_OBSERVATION, None
        if any(marker in command.lower() for marker in ("rg ", "grep ", "find ")):
            return EvidenceType.CODE_SEARCH, TriggerCategory.LOCALIZATION_MILESTONE if output.strip() else None
        return EvidenceType.CODE_OBSERVATION, TriggerCategory.LOCALIZATION_MILESTONE if output.strip() else None
