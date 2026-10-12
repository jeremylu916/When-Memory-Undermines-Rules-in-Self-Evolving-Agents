"""Build immutable paired official reports from sealed task-condition roots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from self_evolving_agent.smp_contracts import canonical_hash
from self_evolving_agent.smp_experiment import assert_pair_parity
from self_evolving_agent.smp_gate_u_rows import load_official_arm
from self_evolving_agent.smp_manifest import SMPExperimentManifest


def build_paired_official_report(
    *,
    manifest: SMPExperimentManifest,
    generation_report_path: str | Path,
) -> dict[str, Any]:
    generation = json.loads(Path(generation_report_path).read_text(encoding="utf-8"))
    generation_hash = generation.pop("report_hash", "")
    if generation_hash != canonical_hash(generation) or generation.get("manifest_hash") != manifest.manifest_hash:
        raise ValueError("paired generation report linkage is invalid")
    split = str(generation["split"])
    if split not in {"dev-tune", "dev-cert", "test-static"}:
        raise ValueError("paired official report split is unsupported")
    expected_treatment = "smp_active" if split == "test-static" else "smp_utility_trial"
    grouped: dict[tuple[str, int, int], dict[str, dict[str, Any]]] = {}
    for record in generation.get("records", []):
        arm = load_official_arm(Path(record["root"]), manifest=manifest, required_split=split)
        identity = arm["identity"]
        key = (identity.task_id, identity.seed, identity.run_index)
        if identity.condition in grouped.setdefault(key, {}):
            raise ValueError("duplicate official paired arm")
        grouped[key][identity.condition] = arm
    pairs = []
    for (task_id, seed, run_index), arms in sorted(grouped.items()):
        if set(arms) != {"nomem", expected_treatment}:
            raise ValueError("official pair is missing its NoMem or treatment arm")
        nomem, treatment = arms["nomem"], arms[expected_treatment]
        assert_pair_parity(nomem["plan"], treatment["plan"])
        if not nomem["outcome"].evaluation_terminal or not treatment["outcome"].evaluation_terminal:
            raise ValueError("paired official report requires completed outcomes")
        pairs.append(
            {
                "task_id": task_id,
                "seed": seed,
                "run_index": run_index,
                "parity_hash": canonical_hash(nomem["plan"].parity),
                "arms": {
                    condition: {
                        "identity_hash": arm["identity"].identity_hash,
                        "plan_hash": arm["plan"].plan_hash,
                        "outcome_hash": arm["outcome"].outcome_hash,
                        "resolved": arm["outcome"].resolved,
                        "runtime_accounting": {
                            name: arm["runtime"].get(name, 0)
                            for name in ("total_calls", "total_cost", "total_input_tokens", "total_output_tokens", "memory_visible_tokens")
                        },
                    }
                    for condition, arm in sorted(arms.items())
                },
            }
        )
    expected_tasks = {str(row["task_id"]) for row in manifest.splits[split]}
    if {pair["task_id"] for pair in pairs} != expected_tasks:
        raise ValueError("official pairs do not cover the complete manifest split")
    report = {
        "schema_version": "smp.paired_official_report.v1",
        "manifest_hash": manifest.manifest_hash,
        "generation_report_hash": generation_hash,
        "split": split,
        "treatment_condition": expected_treatment,
        "pairs": pairs,
    }
    report["report_hash"] = canonical_hash(report)
    return report
