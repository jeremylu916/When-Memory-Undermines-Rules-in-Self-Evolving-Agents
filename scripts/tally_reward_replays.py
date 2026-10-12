"""Reward-hacking replay tables (v11b/v11c) for any directory of replay runs.

usage: python scripts/tally_reward_replays.py RUN_GLOB [RUN_GLOB ...]
Each matched directory is named <mode>-seed<k>[_resume] (or "controls" /
"r_controls"); rows are pooled by mode, memory variant and arm. Wilson 95%
intervals over valid episodes; per-stream rates for the as-is memory, pinned.
"""

from __future__ import annotations

import collections
import glob
import json
import re
import sys

GROUPS = {"unseen loophole wordings": ("heldout_rh_outdated", "heldout_rh_leftover_tmp"),
          "records conflict": ("heldout_c_stale_sweep", "heldout_c_records_tidy"),
          "absolute (c_settings_purge)": ("heldout_c_settings_purge",),
          "approval scope (a_other_copy)": ("heldout_a_other_copy",)}


def wilson(k, n, z=1.96):
    if not n:
        return 0.0, 0.0
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return max(0.0, c - h), min(1.0, c + h)


def main(patterns):
    cells = collections.defaultdict(lambda: [0, 0, 0])  # violations, valid, launched
    for pattern in patterns:
        for rows in glob.glob(pattern.rstrip("/") + "/rows.jsonl"):
            name = re.sub(r"^r_|_resume$", "", rows.split("/")[-2])
            mode = "rules-only" if name == "controls" else name.rsplit("-", 1)[0]
            for line in open(rows):
                r = json.loads(line)
                group = next((g for g, t in GROUPS.items() if r["template_id"] in t), None)
                if group is None:
                    continue
                variant = r["memory_label"].partition(":")[2] or "none"
                arm = "pinned" if r["pinned"] else "plain"
                for key in ((group, mode, variant, arm), (group, name, variant, arm)):
                    c = cells[key]
                    c[2] += 1
                    if r["valid"]:
                        c[1] += 1
                        c[0] += bool(r["attempted_violations"])

    def fmt(key):
        v, n, launched = cells.get(key, (0, 0, 0))
        if not launched:
            return "-"
        lo, hi = wilson(v, n)
        return f"{v}/{n} = {100 * v / max(1, n):.0f}% [{100 * lo:.0f}, {100 * hi:.0f}]" + (
            f" ({launched - n} inv.)" if launched > n else "")

    modes = sorted({k[1] for k in cells if not re.search(r"seed\d+$", k[1])})
    streams = sorted({k[1] for k in cells if re.search(r"seed\d+$", k[1])})
    for group in GROUPS:
        print(f"### {group}\n\n| memory | arm | " + " | ".join(modes) + " |\n| --- | --- | " + " | ".join("---" for _ in modes) + " |")
        for variant in ("none", "length_matched", "minus_delete"):
            for arm in ("pinned", "plain"):
                if any(cells.get((group, m, variant, arm)) for m in modes):
                    print(f"| {variant} | {arm} | " + " | ".join(fmt((group, m, variant, arm)) for m in modes) + " |")
        print("\nper stream (none, pinned): " + "; ".join(f"{s}: {fmt((group, s, 'none', 'pinned'))}" for s in streams) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
