"""Prompt behavior fixtures for compiled v2 prompt-update proposals."""

from __future__ import annotations

from typing import Any

from self_evolving_agent.candidate_builder import build_candidate_prompt_config
from self_evolving_agent.config import get_prompt_modules


PROMPT_BEHAVIOR_FIXTURE_SCHEMA_VERSION = "prompt_behavior_fixture.v1"


def evaluate_prompt_behavior_fixtures(
    base_config: dict[str, Any],
    proposals: list[dict[str, Any]],
    compiled_updates: list[dict[str, Any]],
    *,
    diagnosis: dict[str, Any] | None = None,
    candidate_prefix: str = "v2-fixture",
) -> dict[str, Any]:
    """Materialize accepted updates and evaluate proposal acceptance tests."""

    candidate = build_candidate_prompt_config(
        base_config,
        compiled_updates,
        diagnosis=diagnosis,
        candidate_prefix=candidate_prefix,
        max_updates=max(1, len(compiled_updates) or 1),
        allow_workflow_mutations=False,
    )
    prompts = get_prompt_modules(candidate)
    candidate_builder = candidate.get("candidate_builder", {})
    results = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            continue
        results.append(
            _evaluate_one(
                index,
                proposal,
                prompts.get(str(proposal.get("target_module") or ""), ""),
            )
        )
    passed_count = sum(1 for item in results if item["passed"])
    return {
        "schema_version": PROMPT_BEHAVIOR_FIXTURE_SCHEMA_VERSION,
        "proposal_count": len(results),
        "passed_count": passed_count,
        "failed_count": len(results) - passed_count,
        "all_passed": passed_count == len(results),
        "candidate_builder": {
            "candidate_id": candidate_builder.get("candidate_id", ""),
            "applied_update_count": len(candidate_builder.get("applied_updates", [])),
            "preserved_update_count": len(candidate_builder.get("preserved_updates", [])),
            "rejected_update_count": len(candidate_builder.get("rejected_updates", [])),
            "updated_modules": candidate_builder.get("updated_modules", []),
            "rejected_updates": candidate_builder.get("rejected_updates", []),
        },
        "results": results,
    }


def _evaluate_one(
    index: int,
    proposal: dict[str, Any],
    prompt_text: str,
) -> dict[str, Any]:
    acceptance_test = proposal.get("acceptance_test")
    if not isinstance(acceptance_test, dict):
        return {
            "proposal_index": index,
            "target_module": str(proposal.get("target_module") or ""),
            "passed": False,
            "failures": [
                {
                    "kind": "missing_acceptance_test",
                    "text": "",
                }
            ],
        }
    should_contain = [
        str(item)
        for item in acceptance_test.get("should_contain", [])
        if isinstance(item, str) and item
    ]
    should_not_contain = [
        str(item)
        for item in acceptance_test.get("should_not_contain", [])
        if isinstance(item, str) and item
    ]
    failures = []
    normalized_prompt = _canonical_text(prompt_text)
    for text in should_contain:
        if _canonical_text(text) not in normalized_prompt:
            failures.append({"kind": "missing_should_contain", "text": text})
    for text in should_not_contain:
        if _canonical_text(text) in normalized_prompt:
            failures.append({"kind": "present_should_not_contain", "text": text})
    return {
        "proposal_index": index,
        "target_module": str(proposal.get("target_module") or ""),
        "passed": not failures,
        "should_contain_count": len(should_contain),
        "should_not_contain_count": len(should_not_contain),
        "failures": failures,
    }


def _canonical_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()
