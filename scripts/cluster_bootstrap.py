"""Cluster-bootstrap violation rates for the paper's key contrasts.

Episodes under one memory are correlated, so the resampling unit is the
memory (source stream x checkpoint label). Groups with a single memory
(rules-only, neutral padding) get a Wilson score interval and are flagged.
Prints rate, 95% interval, and the number of clusters and episodes.
"""

from __future__ import annotations

import collections
import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "runs"
RNG_SEED, DRAWS = 20260930, 4000


def arm_of(r):
    if r.get("approval_gate"):
        return "gate_" + r["approval_gate"]
    if r.get("guard"):
        return "structured_guard"
    return ("task_rules" if r.get("rules_in_task") else "system_selfcheck" if r.get("self_check")
            else "system" if r.get("system_rules") else "pinned" if r["pinned"] else "plain")


def group_of(label):
    if label == "norule":
        return "norule"
    if label in {"initial", "len0"}:
        return "rules_only"
    if re.fullmatch(r"len\d+", label):
        return "neutral_" + label[3:]
    return "evolved:" + (label.partition(":")[2] or "none")


def load(dirs, templates, arm):
    """{group: {cluster: [0/1 per valid episode]}}"""
    data = collections.defaultdict(lambda: collections.defaultdict(list))
    for d in dirs:
        for rows in sorted(ROOT.glob(d + "/rows.jsonl")):
            stream = re.sub(r"^approval_|_resume$", "", rows.parent.name)
            for line in rows.open():
                r = json.loads(line)
                if not r["valid"] or r["template_id"] not in templates or arm_of(r) != arm:
                    continue
                if r["memory_label"].startswith("posthoc"):  # selected after seeing outcomes; excluded
                    continue
                group = group_of(r["memory_label"])
                cluster = f"{stream}/{r['memory_label'].partition(':')[0]}" if group.startswith("evolved") else group
                data[group][cluster].append(int(bool(r["attempted_violations"])))
    return data


def rate_ci(clusters, rng):
    keys = list(clusters)
    pooled = lambda ks: sum(sum(clusters[k]) for k in ks) / max(1, sum(len(clusters[k]) for k in ks))
    point = pooled(keys)
    if len(keys) == 1:  # single memory: Wilson score interval over its episodes
        values = clusters[keys[0]]
        n, z = len(values), 1.959964
        centre = (point + z * z / (2 * n)) / (1 + z * z / n)
        half = z * ((point * (1 - point) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
        return point, max(0.0, centre - half), min(1.0, centre + half), 1, n
    else:
        draws = sorted(pooled(rng.choices(keys, k=len(keys))) for _ in range(DRAWS))
    return point, draws[int(0.025 * DRAWS)], draws[int(0.975 * DRAWS)], len(keys), sum(len(v) for v in clusters.values())


CONTRASTS = [
    ("DeepSeek-V4.1, absolute rule (c_settings_purge)", ["conflict_v6_20260930/replay/*", "conflict_v6_20260930/mitigation/*"],
     {"heldout_c_settings_purge"}, ["pinned", "system", "system_selfcheck"]),
    ("DeepSeek-V4.1, approval-gated records (records_tidy, stale_sweep)",
     ["conflict_v6_20260930/replay/*", "conflict_v6_20260930/mitigation/*"],
     {"heldout_c_records_tidy", "heldout_c_stale_sweep"}, ["pinned", "system", "system_selfcheck"]),
    ("Sonnet 4.6 (transplanted), absolute rule (4 settings probes)",
     ["replication_bedrock_20260930/sonnet46/controls*", "replication_bedrock_20260930/sonnet46/default-seed*",
      "replication_bedrock_20260930/sonnet46/success_only-seed*"],
     {"heldout_c_settings_purge", "heldout_c_settings_move", "heldout_c_settings_empty", "heldout_c_settings_overwrite"},
     ["pinned", "system", "system_selfcheck"]),
    ("Approval scope (3 probes), Sonnet 4.6", ["replication_bedrock_20260930/sonnet46/approval_*"],
     {"heldout_a_other_copy", "heldout_a_archive_rest", "heldout_a_remove_folder"}, ["pinned", "system", "system_selfcheck"]),
    ("Approval scope (3 probes), DeepSeek-V3.2", ["replication_bedrock_20260930/deepseek_v32/approval_*"],
     {"heldout_a_other_copy", "heldout_a_archive_rest", "heldout_a_remove_folder"}, ["pinned", "system"]),
    ("Approval scope (3 probes), Qwen3-235B", ["replication_bedrock_20260930/qwen3_235b/approval_*"],
     {"heldout_a_other_copy", "heldout_a_archive_rest", "heldout_a_remove_folder"}, ["pinned", "system"]),
    ("Native Sonnet seed 17, absolute rule (c_settings_purge), plain", ["native_sonnet_20260930/replay"],
     {"heldout_c_settings_purge"}, ["plain"]),
    ("Native Sonnet seed 17, c_stale_sweep, plain", ["native_sonnet_20260930/replay"],
     {"heldout_c_stale_sweep"}, ["plain"]),
    ("Native Sonnet seed 29, absolute rule (c_settings_purge), plain", ["native_sonnet_20260930/replay_seed29"],
     {"heldout_c_settings_purge"}, ["plain"]),
    ("Native Sonnet seed 29, c_stale_sweep, plain", ["native_sonnet_20260930/replay_seed29"],
     {"heldout_c_stale_sweep"}, ["plain"]),
    # appended last so the shared RNG leaves the earlier intervals unchanged
    ("DeepSeek-V4.1, three further absolute-rule probes (v6b: settings_move/empty/overwrite)",
     ["conflict_v6_20260930/absolute/*"],
     {"heldout_c_settings_move", "heldout_c_settings_empty", "heldout_c_settings_overwrite"},
     ["plain", "pinned", "system", "system_selfcheck"]),
]


def main():
    rng = random.Random(RNG_SEED)
    order = ["norule", "rules_only", "neutral_250", "neutral_1000", "neutral_4000",
             "evolved:none", "evolved:length_matched", "evolved:rules_last"]
    for title, dirs, templates, arms in CONTRASTS:
        print(f"\n### {title}")
        print("| memory | arm | rate | 95% CI | memories | episodes |\n| --- | --- | --- | --- | --- | --- |")
        for arm in arms:
            data = load(dirs, templates, arm)
            for group in sorted(data, key=lambda g: order.index(g) if g in order else 99):
                p, lo, hi, k, n = rate_ci(data[group], rng)
                note = "" if k > 1 else " (Wilson)"
                print(f"| {group} | {arm} | {p:.2f} | [{lo:.2f}, {hi:.2f}]{note} | {k} | {n} |")


if __name__ == "__main__":
    main()
