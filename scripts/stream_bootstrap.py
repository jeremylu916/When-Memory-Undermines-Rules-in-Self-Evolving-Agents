"""Stream-level uncertainty and validity-complete outcomes for the paper.

Checkpoints of one training stream share training history, so the
resampling unit here is the training stream (learner prompt x seed), with all
of its checkpoint memories kept together. Paired contrasts (evolved minus
length-matched, same streams and memories) resample streams jointly.
Single-memory controls get Wilson intervals; with one or two streams no
interval across streams is reported.

Every cell reports, over all launched episodes:
  violations / launched (an attempted violation counts even if the episode later
  became invalid), violations / valid, invalid rate, task success / valid, and
  sensitivity bounds [all invalid-without-violation safe, all of them violating].
"""

from __future__ import annotations

import collections
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cluster_bootstrap import arm_of, group_of  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "runs"
DRAWS = 4000


def stream_of(rows_path: Path) -> str:
    name = re.sub(r"^(r_(dev|ap|conf|padc|pada)|approval|ap|rec|set|ok|conf|pad_c|pad_a)_|_resume$", "", rows_path.parent.name)
    if rows_path.parent.name == "replay_seed29":
        return "native-seed29"
    if rows_path.parent.name == "replay" and "native_sonnet" in str(rows_path):
        return "native-seed17"
    return name


def load(globs, templates, arm):
    """{group: {stream: [episode dicts]}} for one arm."""
    data = collections.defaultdict(lambda: collections.defaultdict(list))
    for pattern in globs:
        for rows in sorted(ROOT.glob(pattern + "/rows.jsonl")):
            stream = stream_of(rows)
            for line in rows.open():
                r = json.loads(line)
                if r["template_id"] not in templates or arm_of(r) != arm or r["memory_label"].startswith("posthoc"):
                    continue
                group = group_of(r["memory_label"])
                key = stream if group.startswith("evolved") else group
                data[group][key].append({"valid": r["valid"], "viol": bool(r["attempted_violations"]),
                                         "solved": bool(r["solved"])})
    return data


def summary(eps):
    n = len(eps)
    valid = [e for e in eps if e["valid"]]
    viol_all = sum(e["viol"] for e in eps)
    invalid_clean = sum(not e["valid"] and not e["viol"] for e in eps)
    return {"n": n, "valid": len(valid), "viol_all": viol_all,
            "viol_valid": sum(e["viol"] for e in valid), "solved_valid": sum(e["solved"] for e in valid),
            "upper": viol_all + invalid_clean}


def wilson(k, n, z=1.959964):
    if not n:
        return 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def boot(streams: dict, stat, rng):
    keys = sorted(streams)
    draws = sorted(stat([streams[k] for k in rng.choices(keys, k=len(keys))]) for _ in range(DRAWS))
    return draws[int(0.025 * DRAWS)], draws[int(0.975 * DRAWS)]


def rate(lists):
    eps = [e for lst in lists for e in lst]
    valid = [e for e in eps if e["valid"]]
    return sum(e["viol"] for e in valid) / max(1, len(valid))


def pct(x):
    return f"{100 * x:.0f}"


def cell(group_streams, rng):
    eps = [e for lst in group_streams.values() for e in lst]
    s = summary(eps)
    k = len(group_streams)
    if k >= 3:
        lo, hi = boot(group_streams, rate, rng)
        interval = f"[{pct(lo)}, {pct(hi)}] streams={k}"
    else:
        lo, hi = wilson(s["viol_valid"], s["valid"])
        interval = f"[{pct(lo)}, {pct(hi)}] Wilson" + (f", streams={k}" if k > 1 else "")
    v = max(1, s["valid"])
    return (f"{s['viol_valid']}/{s['valid']} = {pct(s['viol_valid'] / v)}% {interval} | "
            f"{s['viol_all']}/{s['n']} | {pct(1 - s['valid'] / max(1, s['n']))}% | "
            f"{pct(s['viol_all'] / max(1, s['n']))}-{pct(s['upper'] / max(1, s['n']))}% | "
            f"{pct(s['solved_valid'] / v)}%")


def paired(data, rng):
    a, b = data.get("evolved:none", {}), data.get("evolved:length_matched", {})
    keys = sorted(set(a) & set(b))
    if len(keys) < 3:
        return ""
    pairs = {k: (a[k], b[k]) for k in keys}
    diff = lambda chosen: rate([p[0] for p in chosen]) - rate([p[1] for p in chosen])
    point = diff(list(pairs.values()))
    lo, hi = boot(pairs, diff, rng)
    per_stream = ", ".join(f"{k}: {pct(rate([pairs[k][0]]))} vs {pct(rate([pairs[k][1]]))}" for k in keys)
    return (f"evolved minus length-matched: {100 * point:+.0f} points [{100 * lo:+.0f}, {100 * hi:+.0f}], "
            f"{len(keys)} streams ({per_stream})")


V6 = ["conflict_v6_20260930/replay/*", "conflict_v6_20260930/mitigation/*"]
SON = ["replication_bedrock_20260930/sonnet46/controls*", "replication_bedrock_20260930/sonnet46/default-seed*",
       "replication_bedrock_20260930/sonnet46/success_only-seed*"]
SETTINGS4 = {"heldout_c_settings_purge", "heldout_c_settings_move", "heldout_c_settings_empty",
             "heldout_c_settings_overwrite"}
APPROVAL = {"heldout_a_other_copy", "heldout_a_archive_rest", "heldout_a_remove_folder"}
CONTRASTS = [
    ("DeepSeek-V4.1 (native memories), absolute rule, c_settings_purge", V6, {"heldout_c_settings_purge"},
     ["pinned", "system", "system_selfcheck"]),
    ("DeepSeek-V4.1 (native memories), three further absolute-rule probes (v6b)",
     ["conflict_v6_20260930/absolute/*"],
     {"heldout_c_settings_move", "heldout_c_settings_empty", "heldout_c_settings_overwrite"},
     ["plain", "pinned", "system", "system_selfcheck"]),
    ("DeepSeek-V4.1 (native memories), records rule (records_tidy, stale_sweep)", V6,
     {"heldout_c_records_tidy", "heldout_c_stale_sweep"}, ["pinned", "system", "system_selfcheck"]),
    ("Sonnet 4.6 (transplanted), absolute rule, c_settings_purge", SON, {"heldout_c_settings_purge"},
     ["pinned", "system", "system_selfcheck"]),
    ("Sonnet 4.6 (transplanted), absolute rule, other three settings probes", SON,
     SETTINGS4 - {"heldout_c_settings_purge"}, ["pinned", "system", "system_selfcheck"]),
    ("Approval scope, Sonnet 4.6", ["replication_bedrock_20260930/sonnet46/approval_*"], APPROVAL,
     ["pinned", "system", "system_selfcheck"]),
    ("Approval scope, DeepSeek-V3.2", ["replication_bedrock_20260930/deepseek_v32/approval_*"], APPROVAL,
     ["pinned", "system"]),
    ("Approval scope, Qwen3-235B", ["replication_bedrock_20260930/qwen3_235b/approval_*"], APPROVAL,
     ["pinned", "system"]),
    ("v9 Sonnet 4.6 (transplanted), six new absolute-rule probes", ["confirmation_v9_20261001/sonnet46/*"],
     {f"heldout_v_{x}" for x in ("purge_reorder", "purge_casual", "purge_ratio", "purge_list", "overwrite_blank",
                                 "move_rest")}, ["plain", "pinned"]),
    ("v9 Sonnet 4.6 (transplanted), three new records probes", ["confirmation_v9_20261001/sonnet46/*"],
     {"heldout_v_tmp_sweep", "heldout_v_old_space", "heldout_v_archive_old"}, ["plain", "pinned"]),
    ("v9 Sonnet 4.6, approval case 1 (explicit limit)", ["confirmation_v9_20261001/sonnet46/*"],
     {"heldout_v_limit_explicit", "heldout_v_limit_move"}, ["plain", "pinned"]),
    ("v9 Sonnet 4.6, approval case 2 (unnamed sibling)", ["confirmation_v9_20261001/sonnet46/*"],
     {"heldout_v_unnamed_sibling", "heldout_v_unnamed_move"}, ["plain", "pinned"]),
    ("v9 Sonnet 4.6, approval case 3 (both approved; permitted control)", ["confirmation_v9_20261001/sonnet46/*"],
     {"heldout_v_both_approved"}, ["plain", "pinned"]),
    ("v9 DeepSeek-V4.1 (native), four new purge-family absolute probes", ["confirmation_v9_20261001/v41/conf_*"],
     {f"heldout_v_{x}" for x in ("purge_reorder", "purge_casual", "purge_ratio", "purge_list")},
     ["plain", "pinned", "system_selfcheck"]),
    ("v9 DeepSeek-V4.1 (native), new overwrite/move absolute probes", ["confirmation_v9_20261001/v41/conf_*"],
     {"heldout_v_overwrite_blank", "heldout_v_move_rest"}, ["plain", "pinned", "system_selfcheck"]),
    ("v9 DeepSeek-V4.1 (native), three new records probes", ["confirmation_v9_20261001/v41/conf_*"],
     {"heldout_v_tmp_sweep", "heldout_v_old_space", "heldout_v_archive_old"}, ["plain", "pinned", "system_selfcheck"]),
    ("v9 DeepSeek-V4.1, approval case 1 (explicit limit)", ["confirmation_v9_20261001/v41/conf_*"],
     {"heldout_v_limit_explicit", "heldout_v_limit_move"}, ["plain", "pinned", "system_selfcheck"]),
    ("v9 DeepSeek-V4.1, approval case 2 (unnamed sibling)", ["confirmation_v9_20261001/v41/conf_*"],
     {"heldout_v_unnamed_sibling", "heldout_v_unnamed_move"}, ["plain", "pinned", "system_selfcheck"]),
    ("v9 DeepSeek-V4.1, approval case 3 (both approved; permitted control)", ["confirmation_v9_20261001/v41/conf_*"],
     {"heldout_v_both_approved"}, ["plain", "pinned", "system_selfcheck"]),
    ("Native Sonnet 4.6 (two streams), c_settings_purge, plain",
     ["native_sonnet_20260930/replay", "native_sonnet_20260930/replay_seed29"], {"heldout_c_settings_purge"},
     ["plain"]),
    ("Native Sonnet 4.6 (two streams), c_stale_sweep, plain",
     ["native_sonnet_20260930/replay", "native_sonnet_20260930/replay_seed29"], {"heldout_c_stale_sweep"},
     ["plain"]),
]
ORDER = ["norule", "rules_only", "neutral_250", "neutral_1000", "neutral_4000",
         "evolved:none", "evolved:length_matched", "evolved:rules_last"]


def main():
    rng = random.Random(20261001)
    print("Columns: violations/valid with 95% interval (stream bootstrap if >=3 streams, else Wilson) | "
          "violations/launched | invalid rate | bounds over launched (invalid safe .. invalid violating) | "
          "task success among valid.\n")
    for title, globs, templates, arms in CONTRASTS:
        print(f"### {title}\n")
        print("| memory | arm | viol/valid [95%] | viol/launched | invalid | bounds | solved |\n"
              "| --- | --- | --- | --- | --- | --- | --- |")
        notes = []
        for arm in arms:
            data = load(globs, templates, arm)
            for group in sorted(data, key=lambda g: ORDER.index(g) if g in ORDER else 99):
                print(f"| {group} | {arm} | {cell(data[group], rng)} |")
                if group.startswith("evolved") and title.startswith("Native"):
                    for stream in sorted(data[group]):
                        print(f"| \u2003{stream} | {arm} | {cell({stream: data[group][stream]}, rng)} |")
            p = paired(data, rng)
            if p:
                notes.append(f"* {arm}: {p}")
        print("\n" + "\n".join(notes) + ("\n" if notes else ""))


if __name__ == "__main__":
    main()
