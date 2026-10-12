"""Combine independently executed safety-retention streams after validating inputs."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from self_evolving_agent.retention.contracts import digest
from self_evolving_agent.safety_retention.experiment import summarize


def merge_runs(run_dirs: list[Path]) -> dict:
    if not run_dirs:
        raise ValueError("at least one completed run is required")
    reference_design = reference_hashes = None
    trajectories, sources = [], []
    stream_keys = set()
    totals = Counter()
    usage = Counter()
    models, fingerprints = set(), set()
    for run_dir in run_dirs:
        manifest = json.loads((run_dir / "manifest.json").read_text())
        report = json.loads((run_dir / "report.json").read_text())
        if report["report_hash"] != digest({k: v for k, v in report.items() if k != "report_hash"}):
            raise ValueError(f"report hash mismatch: {run_dir}")
        if report["manifest_hash"] != digest(manifest):
            raise ValueError(f"manifest hash mismatch: {run_dir}")
        if report["evidence_kind"] != "live_model_virtual_workspace":
            raise ValueError(f"not live-model evidence: {run_dir}")
        if not report["accounting"]["accounting_complete"]:
            raise ValueError(f"incomplete accounting: {run_dir}")
        design = {k: v for k, v in manifest.items() if k not in {"conditions", "stream_seeds"}}
        hashes = {k: report[k] for k in ("backend_hash", "actor_prompt_hash", "learner_prompt_hash",
                                         "compression_prompt_hash", "rules_hash")}
        hashes["memory_repair_prompt_hash"] = report.get("memory_repair_prompt_hash")
        if reference_design is None:
            reference_design, reference_hashes = design, hashes
        elif design != reference_design or hashes != reference_hashes:
            raise ValueError(f"design or model configuration differs: {run_dir}")
        if len(report["trajectories"]) != len(manifest["conditions"]) * len(manifest["stream_seeds"]):
            raise ValueError(f"missing trajectories: {run_dir}")
        for trajectory in report["trajectories"]:
            key = (trajectory["condition"], trajectory["stream_seed"])
            if key in stream_keys or key[0] not in manifest["conditions"] or key[1] not in manifest["stream_seeds"]:
                raise ValueError(f"duplicate or unexpected stream: {run_dir}")
            stream_keys.add(key)
            trajectories.append(trajectory)
        ledger = [json.loads(line) for line in (run_dir / "model_calls.jsonl").read_text().splitlines()]
        responses = [row for row in ledger if row["event"] == "response"]
        if len(responses) != report["accounting"]["model_calls"]:
            raise ValueError(f"response count differs from accounting: {run_dir}")
        for row in responses:
            provider = row["provider_usage"]
            for key in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
                usage[key] += provider.get(key, 0)
            models.add(row["reported_model"])
            if row.get("system_fingerprint"):
                fingerprints.add(row["system_fingerprint"])
        for key in ("model_calls", "tokens", "cost_usd"):
            totals[key] += report["accounting"][key]
        sources.append({"run_dir": str(run_dir), "manifest_hash": report["manifest_hash"],
                        "report_hash": report["report_hash"], "streams": len(report["trajectories"])})
    return {"schema_version": "safety_retention.merged.v1",
            "evidence_kind": "live_model_virtual_workspace",
            "design_hash": digest(reference_design), "shared_hashes": reference_hashes,
            "sources": sources, "streams": len(trajectories),
            "accounting": {**totals, "accounting_complete": True},
            "provider_usage": dict(usage), "reported_models": sorted(models),
            "system_fingerprints": sorted(fingerprints), "summary": summarize(trajectories),
            "analysis_note": "Provider seed was not sent for DeepSeek; local stream/trial IDs do not ensure identical sampling."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output already exists")
    result = merge_runs(args.run_dirs)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"streams": result["streams"], "accounting": result["accounting"],
                      "conditions": list(result["summary"]["conditions"])}, indent=2))


if __name__ == "__main__":
    main()
