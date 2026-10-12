"""Real Judge/Exec processing for the fixed SQLite-v2 GEM T=3 candidates.

The production launcher is the only caller.  This module validates its
process-local child authorization before creating output or invoking an API,
Docker oracle, admission builder, or official harness.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from self_evolving_agent.gem_public_oracle import validate_observation_record
from self_evolving_agent.gem_sqlite_v2_oracle import (
    TASK_ID as DJANGO_TASK_ID,
    compare_base_candidate,
    validate_semantic_observation,
)
from self_evolving_agent.gem_t3_production_launcher import (
    APPROVAL_ID,
    TASK_IDS,
    canonical_digest,
    load_json,
    file_sha256,
    validate_authorization_envelope,
    validate_manifest,
    _validate_verification_report,
    _canonical_envelope_path,
)


JUDGE_RESULT_SCHEMA = "gem.t3_judge_result.v2"
EXEC_RESULT_SCHEMA = "gem.t3_exec_result.v2"
ADMISSION_SCHEMA = "gem.t3_exec_admission.v2"
SIDECAR_SCHEMA = "gem.t3_verified_memory_sidecar.v2"

_TASK_IMAGES = {
    "astropy__astropy-13033": "swebench/sweb.eval.x86_64.astropy_1776_astropy-13033:latest",
    DJANGO_TASK_ID: "swebench/sweb.eval.x86_64.django_1776_django-10554:latest",
    "matplotlib__matplotlib-14623": "swebench/sweb.eval.x86_64.matplotlib_1776_matplotlib-14623:latest",
}
_ORACLE_SCRIPTS = {
    "astropy__astropy-13033": "scripts/gem_public_oracles/astropy_13033.py",
    DJANGO_TASK_ID: "scripts/gem_public_oracles/django_10554_sqlite_v2.py",
    "matplotlib__matplotlib-14623": "scripts/gem_public_oracles/matplotlib_14623.py",
}


class ConditionRunnerError(RuntimeError):
    """The authorized T=3 condition could not produce complete evidence."""


def authorize_condition(
    manifest: dict[str, Any],
    project_root: Path,
    *,
    condition: str,
    task_id: str,
    output_dir: Path,
    env: Mapping[str, str],
) -> dict[str, str]:
    digest = validate_manifest(manifest, project_root)
    if condition not in ("Judge", "Exec") or task_id not in TASK_IDS:
        raise ConditionRunnerError("condition/task is outside fixed T=3")
    expected_output = (project_root / manifest["outputs"][condition][task_id]).resolve()
    if output_dir.resolve() != expected_output:
        raise ConditionRunnerError("condition output does not match manifest")
    if env.get("GEM_T3_V2_APPROVAL_ID") != APPROVAL_ID:
        raise ConditionRunnerError("child approval id mismatch")
    if env.get("GEM_T3_V2_MANIFEST_DIGEST") != digest:
        raise ConditionRunnerError("child manifest digest mismatch")
    if env.get("GEM_T3_V2_CONDITION") != condition or env.get(
        "GEM_T3_V2_TASK_ID"
    ) != task_id:
        raise ConditionRunnerError("child task/condition binding mismatch")
    token = env.get("GEM_T3_V2_CHILD_AUTHORIZATION_TOKEN", "")
    envelope_path = _canonical_envelope_path(project_root)
    if not envelope_path.is_file():
        raise ConditionRunnerError("runtime authorization envelope is absent")
    envelope = load_json(envelope_path)
    try:
        validate_authorization_envelope(envelope, digest)
    except Exception as exc:
        raise ConditionRunnerError("runtime authorization envelope is invalid") from exc
    if envelope["state"] != "verified_authorized":
        raise ConditionRunnerError("runtime authorization envelope is not GO")
    key = f"{condition}:{task_id}"
    if not token or not hmac.compare_digest(
        hashlib.sha256(token.encode("utf-8")).hexdigest(),
        envelope["child_token_hashes"].get(key, ""),
    ):
        raise ConditionRunnerError("child authorization token mismatch")
    try:
        _validate_verification_report(project_root, envelope)
    except Exception as exc:
        raise ConditionRunnerError("independent verification report is not bound GO") from exc
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise ConditionRunnerError("condition output is occupied")
    return {
        "manifest_digest": digest,
        "condition": condition,
        "task_id": task_id,
        "output_dir": str(output_dir.resolve()),
        "child_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "envelope_sha256": file_sha256(envelope_path),
        "_child_token": token,
    }


def run_judge(
    manifest: dict[str, Any],
    project_root: Path,
    task_id: str,
    output_dir: Path,
    *,
    completion: Any,
    cost_calculator: Any,
    authorization_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    _validate_authorization_context(
        manifest, project_root, "Judge", task_id, output_dir, authorization_context
    )
    candidate = _candidate(project_root, task_id)
    problem = _problem_statement(project_root, task_id)
    prompt = {
        "task_id": task_id,
        "candidate_id": candidate["candidate_id"],
        "public_problem_statement": problem,
        "candidate_claim": candidate["claim"],
        "failure_signature": candidate["failure_signature"],
        "intervention_summary": candidate["intervention"]["summary"],
        "question": (
            "Using only this public problem and sanitized candidate, should the "
            "candidate be admitted as reusable memory? Return JSON only."
        ),
        "schema": {
            "decision": "admitted|quarantined",
            "confidence": "number 0..1",
            "reason": "short public-only rationale",
        },
    }
    response = completion(
        model=manifest["model"],
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a conservative memory-admission judge. Never infer "
                    "hidden tests, gold patches, reference solutions, or official outcomes. "
                    "Return exactly one JSON object."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
        ],
        base_url=manifest["network"]["allowed_api_base"],
        reasoning_effort="medium",
        verbosity="medium",
        max_completion_tokens=500,
        timeout=manifest["timeouts_seconds"]["llm"],
        max_retries=0,
        drop_params=True,
    )
    payload = _response_json(response)
    decision = str(payload.get("decision") or "")
    if decision not in ("admitted", "quarantined"):
        raise ConditionRunnerError("judge returned invalid decision")
    confidence = float(payload.get("confidence", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ConditionRunnerError("judge confidence outside [0,1]")
    usage = _response_usage(response, cost_calculator, manifest["cost_policy"])
    if usage["cost_usd"] > manifest["budget"]["per_task_per_condition"]:
        raise ConditionRunnerError("Judge response exceeded per-task budget")
    record = {
        "schema_version": JUDGE_RESULT_SCHEMA,
        "created_at": _utc_now(),
        "authorization_ref": APPROVAL_ID,
        "condition": "Judge",
        "task_id": task_id,
        "candidate_id": candidate["candidate_id"],
        "provider": manifest["provider"],
        "model": manifest["model"],
        "decision": decision,
        "confidence": confidence,
        "reason": _safe_reason(payload.get("reason")),
        "usage": usage,
        "admission_event_created": False,
        "sidecar_created": False,
        "task_solved": False,
        "leakage_flags": [],
    }
    _validate_judge_result(record, manifest)
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "judge_result.json", record)
    return record


def run_exec(
    manifest: dict[str, Any],
    project_root: Path,
    task_id: str,
    output_dir: Path,
    *,
    env: Mapping[str, str],
    runner: Any = subprocess.run,
    authorization_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    _validate_authorization_context(
        manifest, project_root, "Exec", task_id, output_dir, authorization_context
    )
    candidate = _candidate(project_root, task_id)
    coverage = _coverage(project_root, task_id)
    prediction = _prediction(project_root, task_id)
    if _patch_hash_in_disposable_image(
        project_root, task_id, coverage["base_commit"], prediction["model_patch"], runner
    ) != candidate["intervention"]["patch_hash"]:
        raise ConditionRunnerError("candidate patch hash does not match frozen linkage")
    base = _run_oracle(
        manifest, project_root, task_id, coverage["base_commit"], "frozen_base", "", env, runner
    )
    post = _run_oracle(
        manifest,
        project_root,
        task_id,
        coverage["base_commit"],
        "candidate",
        prediction["model_patch"],
        env,
        runner,
    )
    if task_id == DJANGO_TASK_ID:
        pair = compare_base_candidate(base, post)
        admitted = bool(pair["admission_eligible"])
        outcome = pair["status"]
    else:
        validate_observation_record(base)
        validate_observation_record(post)
        admitted = bool(
            base["subject_role"] == "frozen_base"
            and base["reproduction_observed"] is True
            and base["outcome"] == "fail"
            and post["subject_role"] == "candidate"
            and post["public_observable_satisfied"] is True
            and post["outcome"] == "pass"
        )
        outcome = "fail_before_pass_after" if admitted else "oracle_not_discriminating"
    result = {
        "schema_version": EXEC_RESULT_SCHEMA,
        "created_at": _utc_now(),
        "authorization_ref": APPROVAL_ID,
        "condition": "Exec",
        "task_id": task_id,
        "candidate_id": candidate["candidate_id"],
        "coverage_id": coverage["coverage_id"],
        "base_commit": coverage["base_commit"],
        "candidate_patch_hash": candidate["intervention"]["patch_hash"],
        "base_observation_hash": _record_hash(base),
        "candidate_observation_hash": _record_hash(post),
        "oracle_status": outcome,
        "decision": "admitted" if admitted else "quarantined",
        "task_solved": False,
        "partial": False,
        "interrupted": False,
        "leakage_flags": [],
    }
    _validate_exec_result(result, candidate, coverage)
    admission = _admission(result, coverage)
    sidecar = _sidecar(admission, coverage) if admitted else None
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "base_observation.json", base)
    _write_json(output_dir / "candidate_observation.json", post)
    _write_json(output_dir / "exec_result.json", result)
    _write_json(output_dir / "admission_event.json", admission)
    if sidecar is not None:
        _write_json(output_dir / "verified_memory_sidecar.json", sidecar)
    return result


def _run_oracle(manifest, root, task_id, base, role, patch, env, runner):
    script = _ORACLE_SCRIPTS[task_id]
    oracle_args = ["--base-commit", base, "--mode", "public-reproduction"]
    if task_id == DJANGO_TASK_ID:
        oracle_args[2:2] = ["--database", "sqlite"]
    elif task_id == "matplotlib__matplotlib-14623":
        oracle_args[2:2] = ["--backend", "Agg"]
    # Preserve image-provided ignored build artifacts (for example Astropy C
    # extensions) while still removing ordinary untracked files.  `-x` made an
    # otherwise valid SWE-bench image unimportable before the oracle ran.
    inner = "cd /testbed && git checkout --quiet --detach %s && git reset --hard --quiet %s && git clean -fd --quiet" % (base, base)
    if patch:
        inner += " && git apply -"
    inner += " ; PYTHONPATH=/application /opt/miniconda3/envs/testbed/bin/python /application/%s %s" % (
        script,
        " ".join(oracle_args),
    )
    child = _oracle_env_args(env, task_id, role)
    argv = [
        "docker", "run", "--rm", "-i", "--network", "none",
        "-v", "%s:/application:ro" % root,
    ] + child + [_TASK_IMAGES[task_id], "bash", "-lc", inner]
    completed = runner(
        argv,
        input=patch if patch else None,
        text=True,
        capture_output=True,
        timeout=manifest["timeouts_seconds"]["oracle"],
        check=False,
        env=dict(env),
    )
    if completed.returncode not in (0, 1):
        raise ConditionRunnerError("public oracle infrastructure failed: %s" % task_id)
    lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    if len(lines) != 1:
        raise ConditionRunnerError("public oracle did not emit one observation")
    return json.loads(lines[0])


def _patch_hash_in_disposable_image(root, task_id, base, patch, runner):
    command = (
        "cd /testbed && git checkout --quiet --detach %s && "
        "git reset --hard --quiet %s && git clean -fdx --quiet && git apply - && "
        "git diff --binary HEAD | sha256sum"
    ) % (base, base)
    completed = runner(
        ["docker", "run", "--rm", "-i", "--network", "none", _TASK_IMAGES[task_id], "bash", "-lc", command],
        input=patch,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        raise ConditionRunnerError("candidate patch cannot be applied at frozen base")
    digest = completed.stdout.strip().split()[0]
    return "sha256:" + digest


def _oracle_env_args(env, task_id, role):
    names = (
        "GEM_T3_V2_APPROVAL_ID",
        "GEM_T3_V2_MANIFEST_DIGEST",
        "GEM_T3_V2_CHILD_AUTHORIZATION_TOKEN",
    )
    result = ["-e", "GEM_T3_ORACLE_SUBJECT_ROLE=%s" % role, "-e", "GEM_T3_V2_TASK_ID=%s" % task_id, "-e", "GEM_T3_V2_CONDITION=Exec"]
    for name in names:
        value = env.get(name, "")
        if not value:
            raise ConditionRunnerError("oracle authorization environment is incomplete")
        result.extend(["-e", name])
    return result


def _validate_authorization_context(manifest, root, condition, task_id, output_dir, context):
    if not isinstance(context, dict) or set(context) != {
        "manifest_digest", "condition", "task_id", "output_dir",
        "child_token_sha256", "envelope_sha256", "_child_token",
    }:
        raise ConditionRunnerError("authorized library context is required")
    if (
        context["manifest_digest"] != validate_manifest(manifest, root)
        or context["condition"] != condition
        or context["task_id"] != task_id
        or context["output_dir"] != str(output_dir.resolve())
        or len(context["child_token_sha256"]) != 64
        or len(context["envelope_sha256"]) != 64
    ):
        raise ConditionRunnerError("authorized library context mismatch")
    revalidated = authorize_condition(
        manifest,
        root,
        condition=condition,
        task_id=task_id,
        output_dir=output_dir,
        env={
            "GEM_T3_V2_APPROVAL_ID": APPROVAL_ID,
            "GEM_T3_V2_MANIFEST_DIGEST": context["manifest_digest"],
            "GEM_T3_V2_CONDITION": condition,
            "GEM_T3_V2_TASK_ID": task_id,
            "GEM_T3_V2_CHILD_AUTHORIZATION_TOKEN": context["_child_token"],
        },
    )
    if revalidated != context:
        raise ConditionRunnerError("authorized library context revalidation mismatch")


def _candidate(root, task_id):
    records = _read_jsonl(root / "runs/gem_t3_nomem_trajectory_collection/offline_gem/memory_candidates.jsonl")
    return _one(records, "task_id", task_id)


def _coverage(root, task_id):
    records = _read_json(root / "runs/gem_t3_public_oracle_coverage_sqlite_v2/coverage_records.json")
    return _one(records, "task_id", task_id)


def _prediction(root, task_id):
    records = _read_json(root / "runs/gem_t3_nomem_trajectory_collection/preds_t3_nomem.json")
    if isinstance(records, dict):
        record = records.get(task_id)
        if not isinstance(record, dict):
            raise ConditionRunnerError("prediction linkage is missing")
        return record
    return _one(records, "instance_id", task_id)


def _problem_statement(root, task_id):
    subset = _read_json(root / "configs/swe_verified_subset_40.json")
    return _one(subset["tasks"], "instance_id", task_id)["problem_statement"]


def _admission(result, coverage):
    admitted = result["decision"] == "admitted"
    event = {
        "schema_version": ADMISSION_SCHEMA,
        "authorization_ref": result["authorization_ref"],
        "policy": "Exec",
        "task_id": result["task_id"],
        "candidate_id": result["candidate_id"],
        "coverage_id": result["coverage_id"],
        "decision": result["decision"],
        "reason": result["oracle_status"],
        "oracle_result_hash": _record_hash(result),
        "maximum_claim": coverage["maximum_claim"],
        "maximum_retrieval_policy": coverage["maximum_retrieval_policy"],
        "partial": False,
        "interrupted": False,
    }
    event["admission_event_id"] = _stable_id("gemt3v2adm", event)
    if admitted != (event["decision"] == "admitted"):
        raise ConditionRunnerError("admission decision mismatch")
    return event


def _sidecar(admission, coverage):
    if admission["decision"] != "admitted":
        raise ConditionRunnerError("quarantined candidate cannot create sidecar")
    if coverage["maximum_retrieval_policy"] != "narrow_only":
        return {
            "schema_version": SIDECAR_SCHEMA,
            "publication_state": "blocked_pending_scope_review",
            "retrieval_policy": "blocked",
            "task_id": admission["task_id"],
            "candidate_id": admission["candidate_id"],
            "coverage_id": admission["coverage_id"],
            "admission_event_id": admission["admission_event_id"],
            "verified_memory_created": False,
        }
    return {
        "schema_version": SIDECAR_SCHEMA,
        "publication_state": "admission_verified",
        "retrieval_policy": "narrow_only",
        "task_id": admission["task_id"],
        "candidate_id": admission["candidate_id"],
        "coverage_id": admission["coverage_id"],
        "admission_event_id": admission["admission_event_id"],
        "verified_memory_created": True,
    }


def _validate_judge_result(record, manifest):
    if set(record) != {
        "schema_version", "created_at", "authorization_ref", "condition", "task_id",
        "candidate_id", "provider", "model", "decision", "confidence", "reason", "usage",
        "admission_event_created", "sidecar_created", "task_solved", "leakage_flags",
    }:
        raise ConditionRunnerError("judge result fields are not closed")
    if record["schema_version"] != JUDGE_RESULT_SCHEMA or record["condition"] != "Judge":
        raise ConditionRunnerError("invalid Judge result schema/condition")
    if record["provider"] != manifest["provider"] or record["model"] != manifest["model"]:
        raise ConditionRunnerError("judge provider/model drift")
    if record["admission_event_created"] or record["sidecar_created"] or record["task_solved"]:
        raise ConditionRunnerError("Judge cannot create admission, sidecar, or task-solved claim")
    if record["leakage_flags"] != []:
        raise ConditionRunnerError("Judge result has leakage flags")
    _validate_usage(record["usage"], manifest["cost_policy"])


def _validate_exec_result(record, candidate, coverage):
    if set(record) != {
        "schema_version", "created_at", "authorization_ref", "condition", "task_id",
        "candidate_id", "coverage_id", "base_commit", "candidate_patch_hash",
        "base_observation_hash", "candidate_observation_hash", "oracle_status",
        "decision", "task_solved", "partial", "interrupted", "leakage_flags",
    }:
        raise ConditionRunnerError("Exec result fields are not closed")
    if record["schema_version"] != EXEC_RESULT_SCHEMA or record["condition"] != "Exec":
        raise ConditionRunnerError("invalid Exec result schema/condition")
    if record["task_id"] != candidate["task_id"] or record["candidate_id"] != candidate["candidate_id"]:
        raise ConditionRunnerError("Exec candidate linkage mismatch")
    if record["coverage_id"] != coverage["coverage_id"] or record["base_commit"] != coverage["base_commit"]:
        raise ConditionRunnerError("Exec coverage/base linkage mismatch")
    if record["candidate_patch_hash"] != candidate["intervention"]["patch_hash"]:
        raise ConditionRunnerError("Exec candidate patch linkage mismatch")
    if record["partial"] or record["interrupted"] or record["task_solved"]:
        raise ConditionRunnerError("partial/interrupted/task-solved Exec result is forbidden")
    if record["leakage_flags"] != []:
        raise ConditionRunnerError("Exec result has leakage flags")
    admitted_outcomes = {"fail_before_pass_after", "discriminating_pass"}
    if (record["decision"] == "admitted") != (record["oracle_status"] in admitted_outcomes):
        raise ConditionRunnerError("Exec decision/oracle outcome mismatch")
    if record["decision"] not in ("admitted", "quarantined"):
        raise ConditionRunnerError("invalid Exec decision")


def _response_json(response):
    choices = getattr(response, "choices", None) or response.get("choices", [])
    content = choices[0].message.content if hasattr(choices[0], "message") else choices[0]["message"]["content"]
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ConditionRunnerError("judge did not return JSON")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ConditionRunnerError("judge JSON is not an object")
    return value


def _response_usage(response, cost_calculator, cost_policy):
    usage = getattr(response, "usage", None) or {}
    getter = lambda key: getattr(usage, key, None) if not isinstance(usage, dict) else usage.get(key)
    prompt_tokens = _required_token_count(getter("prompt_tokens"), "prompt")
    completion_tokens = _required_token_count(getter("completion_tokens"), "completion")
    total_tokens = _required_token_count(getter("total_tokens"), "total")
    if total_tokens != prompt_tokens + completion_tokens or total_tokens == 0:
        raise ConditionRunnerError("Judge token usage is incomplete or inconsistent")
    frozen_cost = (
        prompt_tokens * float(cost_policy["input_cost_per_token"])
        + completion_tokens * float(cost_policy["output_cost_per_token"])
    )
    calculator_status = "unavailable_frozen_rate_fallback"
    try:
        calculated = float(
            cost_calculator(
                completion_response=response,
                model=cost_policy["billing_model"],
            )
        )
    except Exception:
        cost = frozen_cost
    else:
        if abs(calculated - frozen_cost) > max(1e-9, frozen_cost * 1e-6):
            raise ConditionRunnerError("Judge cost calculator disagrees with frozen rates")
        cost = calculated
        calculator_status = "litellm_explicit_model_matched"
    if cost < 0 or not _finite(cost):
        raise ConditionRunnerError("Judge cost evidence is negative")
    record = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(cost, 12),
        "cost_evidence": {
            "evidence_kind": cost_policy["evidence_kind"],
            "billing_model": cost_policy["billing_model"],
            "input_cost_per_token": cost_policy["input_cost_per_token"],
            "output_cost_per_token": cost_policy["output_cost_per_token"],
            "calculator_status": calculator_status,
            "provider_invoice": False,
        },
    }
    _validate_usage(record, cost_policy)
    return record


def _validate_usage(usage, cost_policy):
    if not isinstance(usage, dict) or set(usage) != {
        "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "cost_evidence",
    }:
        raise ConditionRunnerError("Judge usage fields are not closed")
    prompt = _required_token_count(usage["prompt_tokens"], "prompt")
    completion = _required_token_count(usage["completion_tokens"], "completion")
    total = _required_token_count(usage["total_tokens"], "total")
    if total != prompt + completion or total == 0:
        raise ConditionRunnerError("Judge token usage is incomplete or inconsistent")
    evidence = usage["cost_evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "evidence_kind", "billing_model", "input_cost_per_token",
        "output_cost_per_token", "calculator_status", "provider_invoice",
    }:
        raise ConditionRunnerError("Judge cost evidence fields are not closed")
    if (
        evidence["evidence_kind"] != cost_policy["evidence_kind"]
        or evidence["billing_model"] != cost_policy["billing_model"]
        or evidence["input_cost_per_token"] != cost_policy["input_cost_per_token"]
        or evidence["output_cost_per_token"] != cost_policy["output_cost_per_token"]
        or evidence["provider_invoice"] is not False
        or evidence["calculator_status"] not in {
            "litellm_explicit_model_matched", "unavailable_frozen_rate_fallback",
        }
    ):
        raise ConditionRunnerError("Judge cost evidence policy drift")
    expected = (
        prompt * float(cost_policy["input_cost_per_token"])
        + completion * float(cost_policy["output_cost_per_token"])
    )
    cost = usage["cost_usd"]
    if not isinstance(cost, (int, float)) or not _finite(float(cost)) or abs(float(cost) - expected) > max(1e-9, expected * 1e-6):
        raise ConditionRunnerError("Judge cost evidence does not match token usage")


def _required_token_count(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConditionRunnerError("Judge %s token usage is unavailable" % label)
    return value


def _finite(value):
    return value == value and value not in (float("inf"), float("-inf"))


def _safe_reason(value):
    return re.sub(r"[^A-Za-z0-9_.: -]+", "_", str(value or ""))[:500]


def _stable_id(prefix, value):
    return "%s_%s" % (prefix, canonical_digest(value)[:20])


def _record_hash(value):
    return "sha256:" + canonical_digest(value)


def _one(records, key, value):
    matches = [item for item in records if item.get(key) == value]
    if len(matches) != 1:
        raise ConditionRunnerError("expected exactly one %s linkage" % key)
    return matches[0]


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()
