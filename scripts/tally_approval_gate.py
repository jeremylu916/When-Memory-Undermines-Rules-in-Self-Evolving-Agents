"""v8 approval-gate tables (design: docs/research/computer_use_approval_gate_v8.md).

Per model, probe group and arm: realized violations (primary; blocked
actions never execute), attempted violations counting gate-intercepted
would-be violations, task success and validity. Evolved memories are the
checkpoint-100 memories of the six v4 streams; the interval is a cluster
bootstrap over memories. Baseline pinned/system_selfcheck safety rows are
the v7 replication rows (same memories, probes, trial seeds).
Utility: gate false blocks = blocked in-scope actions the oracle permits.
"""

from __future__ import annotations

import collections
import json
import random
import re
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "runs"
V8, V7 = RUNS / "approval_gate_v8_20261001", RUNS / "replication_bedrock_20260930"
GROUPS = {"approval scope": {"heldout_a_other_copy", "heldout_a_archive_rest", "heldout_a_remove_folder"},
          "records conflict": {"heldout_c_stale_sweep", "heldout_c_records_tidy"},
          "absolute (settings)": {"heldout_c_settings_purge", "heldout_c_settings_move",
                                  "heldout_c_settings_empty", "heldout_c_settings_overwrite"}}
ARMS = ["pinned", "system_selfcheck", "gate_quote_only", "gate_verified", "gate_verified_v2", "structured_guard"]


def arm_of(r):
    if r.get("approval_gate"):
        return "gate_" + r["approval_gate"]
    if r.get("guard"):
        return "structured_guard"
    return ("task_rules" if r.get("rules_in_task") else "system_selfcheck" if r.get("self_check")
            else "system" if r.get("system_rules") else "pinned" if r["pinned"] else "plain")


def memory_of(stream, label):
    if label in {"initial", "len0"}:
        return "rules_only", "rules_only"
    if label == "len1000":
        return "neutral_1000", "neutral_1000"
    if label == "ckpt100":
        return "evolved", stream
    return None, None


def rows(model, utility=False):
    dirs = [V8 / model] + ([] if utility else [V7 / model])
    for root in dirs:
        for f in sorted(root.glob("*/rows.jsonl")):
            stream = re.sub(r"^(approval|ap|rec|set|ok|v2ap|v2rec|v2set|v2ok)_|_resume$", "", f.parent.name)
            if utility != f.parent.name.startswith(("ok_", "v2ok_")) or f.parent.name.startswith("smoke"):
                continue
            for line in f.open():
                r = json.loads(line)
                group, cluster = memory_of(stream, r["memory_label"])
                if group:
                    yield group, cluster, r


def would_violate(r):
    return bool(r["attempted_violations"]) or any(g["blocked"] and g["would_violate"] for g in r.get("gate_log", []))


def ci(clusters, rng, draws=4000):
    keys = list(clusters)
    pooled = lambda ks: sum(sum(clusters[k]) for k in ks) / max(1, sum(len(clusters[k]) for k in ks))
    if len(keys) < 2:
        return pooled(keys), None
    boot = sorted(pooled(rng.choices(keys, k=len(keys))) for _ in range(draws))
    return pooled(keys), (boot[int(0.025 * draws)], boot[int(0.975 * draws)])


def fmt(clusters, rng):
    n = sum(len(v) for v in clusters.values())
    if not n:
        return "-"
    p, interval = ci(clusters, rng)
    return f"{100 * p:.0f}%" + (f" [{100 * interval[0]:.0f}, {100 * interval[1]:.0f}]" if interval else "") + f" (n={n})"


def main():
    rng = random.Random(20261001)
    for model in ("sonnet46", "deepseek_v32"):
        print(f"\n## {model}: safety probes (realized violation / attempted incl. intercepted / solved)")
        data = collections.defaultdict(lambda: collections.defaultdict(list))
        for group, cluster, r in rows(model):
            probe = next((g for g, ts in GROUPS.items() if r["template_id"] in ts), None)
            arm = arm_of(r)
            if probe and arm in ARMS and r["valid"]:
                key = (probe, group, arm)
                data[key + ("real",)][cluster].append(int(bool(r["realized_violations"])))
                data[key + ("att",)][cluster].append(int(would_violate(r)))
                data[key + ("ok",)][cluster].append(int(r["solved"]))
        valid = collections.Counter()
        for group, cluster, r in rows(model):
            valid[(arm_of(r), r["valid"])] += 1
        print("| probes | memory | arm | realized | attempted+intercepted | solved |\n| --- | --- | --- | --- | --- | --- |")
        for probe in GROUPS:
            for group in ("rules_only", "neutral_1000", "evolved"):
                for arm in ARMS:
                    if (probe, group, arm, "real") in data:
                        cells = [fmt(data[(probe, group, arm, m)], rng) for m in ("real", "att", "ok")]
                        print(f"| {probe} | {group} | {arm} | " + " | ".join(cells) + " |")
        print("validity by arm:", {a: f"{valid[(a, True)]}/{valid[(a, True)] + valid[(a, False)]}" for a in ARMS})

        print(f"\n## {model}: utility (legitimate tasks)")
        util = collections.defaultdict(lambda: collections.defaultdict(list))
        false_blocks = collections.Counter()
        for group, cluster, r in rows(model, utility=True):
            arm = arm_of(r)
            if not r["valid"]:
                continue
            approved = r["template_id"].removeprefix("heldout_ok_").startswith(("approved", "finalize", "share"))
            kind = "approved" if approved else "unprotected"
            util[(kind, group, arm, "ok")][cluster].append(int(r["solved"]))
            util[(kind, group, arm, "viol")][cluster].append(int(bool(r["realized_violations"])))
            if kind != "approved":
                continue
            for g in r.get("gate_log", []):
                false_blocks[(arm, "in_scope_permitted")] += not g["would_violate"]
                false_blocks[(arm, "blocked_permitted")] += g["blocked"] and not g["would_violate"]
            if any(g["blocked"] and not g["would_violate"] for g in r.get("gate_log", [])):
                false_blocks[(arm, "episodes_with_false_block")] += 1
            false_blocks[(arm, "episodes")] += 1
        print("| tasks | memory | arm | solved | realized violation |\n| --- | --- | --- | --- | --- |")
        for kind in ("approved", "unprotected"):
            for group in ("rules_only", "evolved"):
                for arm in ARMS:
                    if (kind, group, arm, "ok") in util:
                        print(f"| {kind} | {group} | {arm} | {fmt(util[(kind, group, arm, 'ok')], rng)} | "
                              f"{fmt(util[(kind, group, arm, 'viol')], rng)} |")
        for arm in ("gate_quote_only", "gate_verified", "gate_verified_v2"):
            fb = {k[1]: v for k, v in false_blocks.items() if k[0] == arm}
            print(f"{arm}: permitted in-scope actions {fb.get('in_scope_permitted', 0)}, blocked "
                  f"{fb.get('blocked_permitted', 0)}; episodes with a false block "
                  f"{fb.get('episodes_with_false_block', 0)}/{fb.get('episodes', 0)}")


if __name__ == "__main__":
    main()
