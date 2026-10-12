"""Public-only batch scope review for the frozen top-five pair cohort."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from self_evolving_agent.public_local_pair_mining import (
    PublicInputViolation,
    _assert_no_forbidden_keys,
    image_task_identity,
    load_public_tasks,
)


SCHEMA = "gem.public_pair_scope_review.v1"
ALLOWED_DECISIONS = {"eligible", "rejected", "needs_more_public_evidence"}
PAIR_ORDER = (
    ("astropy__astropy-13033", "astropy__astropy-13453"),
    ("mwaskom__seaborn-3069", "mwaskom__seaborn-3187"),
    ("scikit-learn__scikit-learn-10297", "scikit-learn__scikit-learn-10844"),
    ("django__django-10097", "django__django-10554"),
    ("pydata__xarray-2905", "pydata__xarray-3095"),
)

# These decisions are derived solely from the public statements named by the
# frozen cohort. They deliberately describe observable behavior, not patches.
REVIEW_CONTRACTS: Mapping[tuple[str, str], Mapping[str, Any]] = {
    PAIR_ORDER[0]: {
        "exact_shared_public_semantic_predicate": None,
        "public_overlap_rejected_as_insufficient": "Astropy tabular data and formatting/validation vocabulary",
        "source_public_oracle_design": "Construct a public TimeSeries with an additional required column, remove that column, and assert that the public exception diagnoses missing required columns rather than a misleading first-column condition.",
        "maximum_memory_claim": "For TimeSeries removal of an additional required column, validate that the later diagnostic identifies missing required columns; no repair or HTML-formatting claim.",
        "target_applicability_contract": "Target must concern TimeSeries required-column removal followed by the same missing-column diagnostic. HTML table writer formats are explicitly outside scope.",
        "decision": "rejected",
        "rejection_reason": "Source is a TimeSeries required-column-removal diagnostic; target is HTML table writer application of supplied formats. No shared operation or observable contract.",
    },
    PAIR_ORDER[1]: {
        "exact_shared_public_semantic_predicate": None,
        "public_overlap_rejected_as_insufficient": "Seaborn plot presentation",
        "source_public_oracle_design": "Create a public nominal-scale plot and compare axis extent, grid visibility, and y-axis inversion with documented categorical-scale behavior.",
        "maximum_memory_claim": "Nominal scales should reproduce the three enumerated categorical-axis finalization behaviors only.",
        "target_applicability_contract": "Target must exercise nominal/categorical axis finalization. ScalarFormatter multiplicative offsets in legend labels do not match.",
        "decision": "rejected",
        "rejection_reason": "Source concerns nominal-axis extent/grid/inversion; target concerns ScalarFormatter offset propagation into legends. Shared plotting vocabulary is a broad-noun false positive.",
    },
    PAIR_ORDER[2]: {
        "exact_shared_public_semantic_predicate": None,
        "public_overlap_rejected_as_insufficient": "Scikit-learn numeric/model error behavior",
        "source_public_oracle_design": "Instantiate RidgeClassifierCV with store_cv_values=True and cv=None, fit public numeric data, and check that the documented cross-validation values are retained.",
        "maximum_memory_claim": "RidgeClassifierCV publicly accepts store_cv_values and exposes its documented CV values under the stated configuration.",
        "target_applicability_contract": "Target must concern RidgeClassifierCV CV-value storage. Integer overflow in fowlkes_mallows_score is explicitly outside scope.",
        "decision": "rejected",
        "rejection_reason": "Source is estimator constructor/API storage support; target is integer-overflow-safe cluster metric arithmetic. Generic model/error tags are insufficient.",
    },
    PAIR_ORDER[3]: {
        "exact_shared_public_semantic_predicate": None,
        "public_overlap_rejected_as_insufficient": "Django validation/query failure language",
        "source_public_oracle_design": "Apply public URLValidator inputs whose userinfo contains unescaped colon, at-sign, or slash and assert rejection while encoded equivalents remain admissible.",
        "maximum_memory_claim": "URLValidator rejects unescaped RFC-1738-invalid userinfo delimiter characters; no ORM claim.",
        "target_applicability_contract": "Target must concern URL userinfo character validation. UNION queryset ordering/projection compilation does not match.",
        "decision": "rejected",
        "rejection_reason": "Source is URL userinfo regex validation; target is SQL UNION ordering projection. The earlier ordering/query overlap was an ontology false positive.",
    },
    PAIR_ORDER[4]: {
        "exact_shared_public_semantic_predicate": "An xarray operation unexpectedly changes object/value dtype representation instead of preserving the public operation's input semantics.",
        "public_overlap_rejected_as_insufficient": "Both report unintended coercion, but at different operations and representation boundaries.",
        "source_public_oracle_design": "Assign a scalar object with a public .values property into an object-dtype DataArray and assert that the object itself, not its .values payload, is stored.",
        "maximum_memory_claim": "Non-broadcasted object-dtype assignment preserves an RHS object that exposes .values; no copy or Unicode-index claim.",
        "target_applicability_contract": "Eligibility would require public evidence that assignment-time .values unwrapping and copy-time Unicode IndexVariable casting share one xarray coercion boundary. Mere dtype preservation is insufficient.",
        "decision": "needs_more_public_evidence",
        "rejection_reason": "Source trigger is assignment of an object exposing .values; target trigger is shallow/deep copy of Unicode IndexVariable. Public statements do not establish a shared coercion mechanism.",
    },
}


class ScopeReviewViolation(ValueError):
    """Raised when the fixed review cohort or public-only contract drifts."""


def build_scope_reviews(
    tasks: Sequence[Mapping[str, Any]], local_image_ids: set[str]
) -> dict[str, Any]:
    _assert_no_forbidden_keys(list(tasks))
    by_id = {str(row.get("instance_id")): row for row in tasks}
    expected_ids = {item for pair in PAIR_ORDER for item in pair}
    missing = sorted(expected_ids - set(by_id))
    if missing:
        raise ScopeReviewViolation(f"missing cohort tasks: {missing}")
    rows = []
    for rank, pair in enumerate(PAIR_ORDER, 1):
        source_id, target_id = pair
        if source_id == target_id:
            raise ScopeReviewViolation("source and target must be distinct")
        source, target = by_id[source_id], by_id[target_id]
        if source_id not in local_image_ids or target_id not in local_image_ids:
            raise ScopeReviewViolation(f"local image identity missing for {pair}")
        contract = dict(REVIEW_CONTRACTS[pair])
        if contract["decision"] not in ALLOWED_DECISIONS:
            raise ScopeReviewViolation("invalid review decision")
        rows.append({
            "rank": rank,
            "source_task": {
                **{k: source[k] for k in ("instance_id", "repo", "base_commit")},
                "public_problem_statement_sha256": hashlib.sha256(str(source["problem_statement"]).encode()).hexdigest(),
            },
            "target_task": {
                **{k: target[k] for k in ("instance_id", "repo", "base_commit")},
                "public_problem_statement_sha256": hashlib.sha256(str(target["problem_statement"]).encode()).hexdigest(),
            },
            "source_target_disjoint": True,
            "local_image_presence": {"source": True, "target": True},
            **contract,
            "leakage_analysis": {
                "public_fields_only": True,
                "local_input_limited_to_image_identity": True,
                "patch_prediction_outcome_gold_reference_hidden_consumed": False,
                "credential_consumed": False,
                "solver_or_harness_run": False,
            },
        })
    counts = {status: sum(row["decision"] == status for row in rows) for status in sorted(ALLOWED_DECISIONS)}
    return {
        "schema_version": SCHEMA,
        "review_status": "complete",
        "cohort_size": len(rows),
        "pairs": rows,
        "decision_counts": counts,
        "zero_eligible": counts["eligible"] == 0,
        "next_goal_gate": "GO_SOURCE_MEMORY_PREPARATION" if counts["eligible"] else "COMPLETE_ZERO_ELIGIBLE",
        "predicate_relaxed": False,
        "api_calls": 0,
        "paid_calls": 0,
        "solver_runs": 0,
        "official_harness_runs": 0,
        "new_image_pulls": 0,
        "t10_enabled": False,
        "future_ab_contract": "Any real A/B must use identical target tasks for NoMem and VerifiedMem; five targets imply 5+5 task-condition runs.",
    }


def validate_scope_review(payload: Mapping[str, Any]) -> None:
    _assert_no_forbidden_keys(payload)
    if payload.get("review_status") != "complete" or payload.get("cohort_size") != 5:
        raise ScopeReviewViolation("review must be complete for exactly five pairs")
    if payload.get("predicate_relaxed") is not False or payload.get("t10_enabled") is not False:
        raise ScopeReviewViolation("predicate relaxation and T=10 are forbidden")
    for key in ("api_calls", "paid_calls", "solver_runs", "official_harness_runs", "new_image_pulls"):
        if payload.get(key) != 0:
            raise ScopeReviewViolation(f"{key} must be zero")
    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != 5:
        raise ScopeReviewViolation("exact five-pair cohort required")
    for row in pairs:
        if row.get("decision") not in ALLOWED_DECISIONS:
            raise ScopeReviewViolation("invalid decision")
        if not row.get("source_target_disjoint") or not all(row.get("local_image_presence", {}).values()):
            raise ScopeReviewViolation("disjoint local tasks required")
        if row.get("decision") != "eligible" and not row.get("rejection_reason"):
            raise ScopeReviewViolation("non-eligible decision needs a reason")


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()
