"""Run computer-use safety retention; smoke is model-free, llm calls are explicit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from self_evolving_agent.safety_retention.backends import ModelBackend, ScriptedBackend
from self_evolving_agent.safety_retention.experiment import estimate_calls, run_experiment
from self_evolving_agent.safety_retention.fixtures import make_manifest
from self_evolving_agent.safety_retention.replay import write_replay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["scripted", "llm"], default="scripted")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--episodes", type=int, default=12, help="generated fixture training episodes")
    parser.add_argument("--seeds", type=int, nargs="+", default=[17, 29])
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--plan-only", action="store_true", help="write generated manifest without making calls")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text()) if args.manifest else make_manifest(args.episodes, tuple(args.seeds))
        if args.output.exists():
            raise ValueError("output already exists; choose a new directory")
        if args.plan_only:
            args.output.mkdir(parents=True)
            (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            (args.output / "call_plan.json").write_text(json.dumps(estimate_calls(manifest), indent=2) + "\n")
            print(f"Prepared manifest: {args.output / 'manifest.json'}; no model calls made.")
            return
        if args.backend == "llm":
            if args.model_config is None or args.max_calls is None or args.max_cost_usd is None:
                raise ValueError("llm requires --model-config, --max-calls, and --max-cost-usd")
            backend = ModelBackend(json.loads(args.model_config.read_text()), max_calls=args.max_calls,
                max_cost_usd=args.max_cost_usd, ledger=args.output.resolve() / "model_calls.jsonl")
        else:
            backend = ScriptedBackend()
        report = run_experiment(manifest, backend, args.output)
        if manifest.get("environment", "desktop") == "desktop":  # the trace viewer re-simulates the desktop only
            write_replay(manifest, report, args.output / "replay.html")
        print(json.dumps({"evidence_kind": report["evidence_kind"], "accounting": report["accounting"],
                          "conditions": {name: row["checkpoint_curve"][-1] for name, row in report["summary"]["conditions"].items()}}, indent=2))
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Safety retention run failed: {exc}\n")


if __name__ == "__main__":
    main()
