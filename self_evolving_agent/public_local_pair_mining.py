"""Deterministic, public-only semantic pair mining for local SWE-bench images."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "outcome",
        "resolved",
        "resolution_status",
        "prediction",
        "predictions",
        "model_patch",
        "gold_patch",
        "reference_patch",
        "hidden_tests",
        "harness_result",
        "official_result",
    }
)

TAG_PATTERNS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "entity": {
        "timeseries": (r"\btime\s*series\b",),
        "table": (r"\btable\b", r"\bdataframe\b"),
        "column": (r"\bcolumns?\b",),
        "query": (r"\bquery\b", r"\bqueries\b", r"\bqueryset\b"),
        "parser": (r"\bparser\b", r"\bparsing\b"),
        "serializer": (r"\bserializ(?:e|er|ation|ing)\b",),
        "plot": (r"\bplot(?:s|ting)?\b", r"\bfigure\b", r"\baxes?\b"),
        "array": (r"\barrays?\b", r"\bndarray\b"),
        "model": (r"\bmodels?\b", r"\bestimators?\b"),
        "form": (r"\bforms?\b", r"\bformsets?\b"),
    },
    "operation": {
        "add": (r"\badd(?:ed|ing)?\b", r"\binsert(?:ed|ing)?\b"),
        "remove": (r"\bremov(?:e|ed|ing)\b", r"\bdelet(?:e|ed|ing)\b", r"\bdrop(?:ped|ping)?\b"),
        "rename": (r"\brenam(?:e|ed|ing)\b",),
        "convert": (r"\bconvert(?:ed|ing)?\b", r"\bcoerc(?:e|ed|ing|ion)\b"),
        "validate": (r"\bvalidat(?:e|ed|es|ing|ion)\b", r"\bcheck(?:ed|ing|s)?\b"),
        "render": (r"\brender(?:ed|ing|s)?\b", r"\boutput(?:s|ting)?\b"),
        "parse": (r"\bpars(?:e|ed|es|ing)\b",),
        "save": (r"\bsav(?:e|ed|ing)\b", r"\bwrite(?:s|ing)?\b", r"\bwrote\b"),
        "load": (r"\bload(?:ed|ing|s)?\b", r"\bread(?:s|ing)?\b"),
    },
    "behavior": {
        "required": (r"\brequir(?:e|ed|es|ing|ement|ements)\b",),
        "missing": (r"\bmissing\b", r"\babsent\b"),
        "error": (r"\berrors?\b", r"\bfails?\b", r"\bfail(?:ed|ing|ure)\b"),
        "exception": (r"\bexceptions?\b", r"\brais(?:e|ed|es|ing)\b"),
        "warning": (r"\bwarnings?\b", r"\bwarn(?:ed|ing|s)?\b"),
        "diagnostic": (r"\bdiagnostic\b", r"\bmessages?\b", r"\bmisleading\b", r"\bconfusing\b"),
        "ordering": (r"\border(?:ed|ing)?\b", r"\bfirst\b", r"\blast\b"),
        "type": (r"\btypes?\b", r"\bdtypes?\b"),
        "shape": (r"\bshapes?\b", r"\bdimensions?\b"),
    },
    "context": {
        "regression": (r"\bregression\b", r"\bpreviously\b", r"\bused to\b"),
        "edge_case": (r"\bedge[ -]?case\b", r"\bempty\b", r"\bnone\b", r"\bnull\b"),
        "round_trip": (r"\bround[ -]?trip\b", r"\bserialize.*deserialize\b", r"\bwrite.*read\b"),
    },
    # Transfer mechanisms are intentionally compound and narrow.  Broad nouns
    # and symptoms remain useful inventory metadata but cannot make a pair a
    # candidate without one of these public, observable contracts.
    "mechanism": {
        "required_column_removal_diagnostic": (
            r"time\s*series.*required columns?.*remov",
            r"remov.*required columns?.*(?:exception|missing)",
        ),
        "html_table_formats_application": (
            r"html.*formats? (?:argument|option)",
            r"html.*formatting.*(?:ignored|required)",
        ),
        "nominal_categorical_axis_finalization": (
            r"nominal.*categorical.*(?:axis|scale)",
        ),
        "scalar_formatter_offset_legend": (
            r"legend.*scalarformatter.*offset",
            r"scalarformatter.*offset.*legend",
        ),
        "ridge_classifier_cv_storage": (
            r"ridgeclassifiercv.*store_cv_values",
        ),
        "fowlkes_mallows_integer_overflow": (
            r"fowlkes_mallows_score.*overflow",
        ),
        "url_userinfo_character_validation": (
            r"urlvalidator.*username and password",
            r"invalid characters.*username and password",
        ),
        "union_queryset_ordering_projection": (
            r"union queryset.*ordering",
            r"order by position.*select list",
        ),
        "object_assignment_values_property_coercion": (
            r"setitem.*coercing types.*values property",
            r"rhs has a `?values`? property.*coerc",
        ),
        "copy_unicode_index_dtype_preservation": (
            r"copy\(deep=true\).*unicode indices.*object",
            r"copy.*indexvariable.*dtype.*object",
        ),
    },
}

WEIGHTS = {"mechanism": 20, "behavior": 5, "entity": 4, "operation": 3, "context": 1}
LOCAL_IMAGE_INVENTORY_COMMAND = (
    "docker",
    "image",
    "ls",
    "--format",
    "{{.Repository}}:{{.Tag}}",
)
ALLOW_NEW_IMAGE_COMMANDS = False
T10_ENABLED = False


class PublicInputViolation(ValueError):
    """Raised when an input contains benchmark-result-bearing fields."""


class MiningContractViolation(ValueError):
    """Raised when a mining method could execute images or expand scope."""


def validate_mining_contract(contract: object) -> dict[str, object]:
    """Fail closed unless the method is the exact read-only T=3 inventory contract."""
    if not isinstance(contract, dict):
        raise MiningContractViolation("mining contract must be an object")
    if contract.get("allow_new_image_commands") is not False:
        raise MiningContractViolation("allow_new_image_commands must be false")
    if contract.get("t10_enabled") is not False:
        raise MiningContractViolation("t10_enabled must be false")
    command = contract.get("local_image_inventory_command")
    if not isinstance(command, list) or not all(isinstance(token, str) for token in command):
        raise MiningContractViolation("local_image_inventory_command must be a string list")
    lowered = {token.lower() for token in command}
    forbidden = sorted(lowered & {"pull", "build", "run", "create"})
    if forbidden:
        raise MiningContractViolation(f"image-mutating/executing command token forbidden: {forbidden}")
    if tuple(command) != LOCAL_IMAGE_INVENTORY_COMMAND:
        raise MiningContractViolation("inventory command must be exact docker image ls format")
    return contract


def _assert_no_forbidden_keys(value: object, location: str = "$") -> None:
    if isinstance(value, dict):
        bad = sorted(set(value) & FORBIDDEN_INPUT_KEYS)
        if bad:
            raise PublicInputViolation(f"forbidden input keys at {location}: {bad}")
        for key, child in value.items():
            _assert_no_forbidden_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_forbidden_keys(child, f"{location}[{index}]")


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def semantic_tags(problem_statement: str) -> dict[str, list[str]]:
    text = normalize_text(problem_statement)
    return {
        namespace: sorted(
            tag
            for tag, patterns in definitions.items()
            if any(re.search(pattern, text) for pattern in patterns)
        )
        for namespace, definitions in TAG_PATTERNS.items()
    }


def repo_from_public_fields(instance_id: str, repo: object) -> str:
    if isinstance(repo, str) and repo.strip():
        return repo.strip()
    if "__" not in instance_id:
        raise PublicInputViolation(f"cannot derive repo from instance_id: {instance_id}")
    return instance_id.split("__", 1)[1].rsplit("-", 1)[0]


def image_task_identity(image_name: str) -> str | None:
    match = re.fullmatch(
        r"swebench/sweb\.eval\.x86_64\.([^_]+)_1776_([^:]+)(?::[^:]+)?", image_name
    )
    if not match:
        return None
    owner, repo_issue = match.groups()
    return f"{owner}__{repo_issue}"


def load_public_tasks(path: Path) -> tuple[list[dict[str, object]], str]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    _assert_no_forbidden_keys(payload)
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        raise PublicInputViolation("expected top-level tasks list")
    projected: list[dict[str, object]] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise PublicInputViolation(f"task {index} is not an object")
        instance_id = task.get("instance_id")
        base_commit = task.get("base_commit")
        statement = task.get("problem_statement")
        if not all(isinstance(item, str) and item for item in (instance_id, base_commit, statement)):
            raise PublicInputViolation(f"task {index} lacks required public fields")
        # The checked-in subset calls the public repository field `repository` and
        # leaves historical `repo` absent.  Do not consume the extra field: derive
        # the repository deterministically from the allowed instance_id instead.
        repo = repo_from_public_fields(instance_id, task.get("repo"))
        projected.append(
            {
                "instance_id": instance_id,
                "repo": repo,
                "base_commit": base_commit,
                "problem_statement": statement,
            }
        )
    return projected, hashlib.sha256(raw).hexdigest()


def mine_pairs(
    tasks: Sequence[Mapping[str, object]], local_image_ids: Iterable[str]
) -> dict[str, object]:
    local = frozenset(local_image_ids)
    inventory: list[dict[str, object]] = []
    by_id: dict[str, dict[str, object]] = {}
    for task in sorted(tasks, key=lambda row: str(row["instance_id"])):
        instance_id = str(task["instance_id"])
        record = {
            "instance_id": instance_id,
            "repo": str(task["repo"]),
            "base_commit": str(task["base_commit"]),
            "image_available": instance_id in local,
            "semantic_tags": semantic_tags(str(task["problem_statement"])),
            "decision_status": "inventory_only",
        }
        inventory.append(record)
        by_id[instance_id] = record

    candidates: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    ids = sorted(by_id)
    for left_index, source_id in enumerate(ids):
        for target_id in ids[left_index + 1 :]:
            source, target = by_id[source_id], by_id[target_id]
            same_repo = source["repo"] == target["repo"]
            both_images = bool(source["image_available"] and target["image_available"])
            shared = {
                namespace: sorted(
                    set(source["semantic_tags"][namespace])
                    & set(target["semantic_tags"][namespace])
                )
                for namespace in WEIGHTS
            }
            score = sum(WEIGHTS[name] * len(tags) for name, tags in shared.items())
            # Same repo plus broad `error`, `type`, `ordering`, or a shared noun
            # is never sufficient. Candidate generation now requires an exact
            # shared compound mechanism extracted from both public statements.
            semantic_match = bool(shared["mechanism"])
            base = {
                "source_instance_id": source_id,
                "target_instance_id": target_id,
                "repo": source["repo"] if same_repo else None,
                "same_repo": same_repo,
                "both_local_images": both_images,
                "shared_tags": shared,
                "semantic_score": score,
            }
            if same_repo and both_images and semantic_match:
                predicate_parts = [
                    f"shared_{namespace}={','.join(shared[namespace])}"
                    for namespace in ("mechanism", "entity", "behavior", "operation", "context")
                    if shared[namespace]
                ]
                candidates.append(
                    {
                        **base,
                        "decision_status": "eligible_candidate",
                        "public_shared_predicate": " AND ".join(predicate_parts),
                        "transferability_rationale": (
                            "Fixed public semantic overlap makes this a reviewable transfer hypothesis; "
                            "it does not establish narrow memory applicability."
                        ),
                        "source_target_disjoint_proof": {
                            "source_instance_id": source_id,
                            "target_instance_id": target_id,
                            "distinct": source_id != target_id,
                        },
                        "local_image_presence": {
                            "source": bool(source["image_available"]),
                            "target": bool(target["image_available"]),
                        },
                        "leakage_audit": {
                            "public_fields_only": True,
                            "outcome_fields_consumed": False,
                            "patch_prediction_harness_gold_hidden_consumed": False,
                            "local_input_limited_to_image_identity": True,
                        },
                        "required_source_public_oracle_type": (
                            "pair-specific public behavioral oracle proving the exact shared semantic "
                            "predicate without patch, outcome, or hidden-test evidence"
                        ),
                        "current_status": "needs_scope_review",
                    }
                )
            else:
                reasons = []
                if not same_repo:
                    reasons.append("different_repo")
                if not both_images:
                    reasons.append("missing_local_eval_image")
                if not semantic_match:
                    reasons.append("semantic_predicate_not_met")
                rejected.append({**base, "rejection_reasons": reasons, "decision_status": "rejected"})

    candidates.sort(
        key=lambda row: (
            -int(row["semantic_score"]),
            str(row["source_instance_id"]),
            str(row["target_instance_id"]),
        )
    )
    rejected.sort(key=lambda row: (str(row["source_instance_id"]), str(row["target_instance_id"])))
    return {"inventory": inventory, "candidates": candidates, "rejected": rejected}


def mining_gate_status(candidate_count: int) -> str:
    """Zero pairs is a completed mining result, never a threshold-relaxation signal."""
    return "GO_PAIRS_AVAILABLE" if candidate_count else "COMPLETE_ZERO_PAIRS"
