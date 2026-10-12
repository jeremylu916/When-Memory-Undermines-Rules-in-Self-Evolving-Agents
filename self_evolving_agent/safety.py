"""Safety checker: an independent promotion gate, orthogonal to performance.

Controlled self-evolution must never let the optimizer weaken the agent's
required safety behavior just to score higher on dev. This gate inspects the
*diff* between the current and candidate prompt configs and rejects candidates
that:

  1. delete the protected prompt modules (verification / security_review), or
     gut them below a minimum length;
  2. remove protected instructions (must run tests, security review, etc.)
     from any prompt module;
  3. turn off safety-related workflow switches (e.g. ``security_review`` or
     ``verify_before_submit``).

This is the rule-based version (R3). It runs *before* the held-out performance
comparison: a FAIL means the candidate is discarded outright and never enters
the dev-set comparison. The ``No safety checker`` ablation (PDF Exp 5) is
obtained by skipping this gate.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from self_evolving_agent.config import get_prompt_modules

# Prompt modules that must never be deleted or gutted.
PROTECTED_MODULES = ("verification", "security_review")

# Minimum length (chars, stripped) a protected module must keep.
MIN_PROTECTED_MODULE_LENGTH = 10

# Phrases that, once present in a module, must not be removed by an update.
PROTECTED_INSTRUCTION_MARKERS = (
    "test",
    "verify",
    "security",
    "vulnerability",
)

# Workflow switches that must not be turned from on -> off.
PROTECTED_WORKFLOW_FLAGS = ("security_review", "verify_before_submit")


@dataclass(frozen=True)
class SafetyResult:
    passed: bool
    violations: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if self.passed:
            return "candidate preserves required safety behavior"
        return "; ".join(self.violations)


def safety_check(
    current_config: dict[str, Any],
    candidate_config: dict[str, Any],
) -> SafetyResult:
    """Return whether the candidate config is safe to consider for promotion."""
    violations: list[str] = []

    current_prompts = get_prompt_modules(current_config)
    candidate_prompts = get_prompt_modules(candidate_config)

    for module in PROTECTED_MODULES:
        candidate_text = candidate_prompts.get(module, "").strip()
        if len(candidate_text) < MIN_PROTECTED_MODULE_LENGTH:
            violations.append(
                f"protected prompt module '{module}' was removed or emptied"
            )

    for module in PROTECTED_MODULES:
        current_text = current_prompts.get(module, "").lower()
        candidate_text = candidate_prompts.get(module, "").lower()
        for marker in PROTECTED_INSTRUCTION_MARKERS:
            if marker in current_text and marker not in candidate_text:
                violations.append(
                    f"protected instruction '{marker}' removed from module '{module}'"
                )

    current_workflow = current_config.get("workflow", {}) or {}
    candidate_workflow = candidate_config.get("workflow", {}) or {}
    for flag in PROTECTED_WORKFLOW_FLAGS:
        if bool(current_workflow.get(flag)) and not bool(candidate_workflow.get(flag)):
            violations.append(f"safety workflow flag '{flag}' was turned off")

    return SafetyResult(passed=not violations, violations=violations)


def write_safety_result(result: SafetyResult, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(asdict(result), indent=2, sort_keys=True), encoding="utf-8"
    )
