"""v15 A tallies: frontier models (Opus 5.5, GPT-6.1) on transplanted and native memories.

usage: python scripts/tally_frontier.py > reports/frontier_v15.md
"""

from __future__ import annotations

import collections
import glob
import json
import re
from pathlib import Path

D = Path(__file__).resolve().parents[1] / "runs/frontier_v15_20261003"
NAMES = {"opus55": "Claude Opus 5.5", "gpt61": "GPT-6.1-sol"}


def cells(pattern, key):
    c = collections.defaultdict(lambda: [0, 0, 0, 0])  # viol, valid, launched, solved
    for f in glob.glob(str(D / pattern)):
        for line in open(f):
            r = json.loads(line)
            k = key(f, r)
            if k is None:
                continue
            c[k][2] += 1
            if r["valid"]:
                c[k][1] += 1
                c[k][0] += bool(r["attempted_violations"])
                c[k][3] += bool(r["solved"])
    return c


def fmt(v):
    viol, valid, launched, solved = v
    return f"{viol}/{valid}" + (f" ({launched - valid} inv.)" if launched > valid else "")


def main():
    for m, name in NAMES.items():
        print(f"\n## {name}\n")
        arm = lambda r: "pinned" if r["pinned"] else "plain"
        lab = lambda r: r["memory_label"].replace("ckpt100", "evolved").replace("len0", "rules-only").replace("len1000", "+1000 chars")
        t = cells(f"{m}/t_*/rows.jsonl", lambda f, r: (r["template_id"].removeprefix("heldout_"), lab(r), arm(r)))
        print("### Transplanted DeepSeek-V4.1 benign memories (violations / valid)\n")
        print("| probe | memory | arm | violations |\n| --- | --- | --- | --- |")
        for k in sorted(t):
            print(f"| {k[0]} | {k[1]} | {k[2]} | {fmt(t[k])} |")
        tr = cells(f"{m}/tr_*/rows.jsonl", lambda f, r: (re.sub(r"-seed\d+$", "", Path(f).parent.name[3:]),
                                                        "length_matched" if "length_matched" in r["memory_label"] else "as-is"))
        print("\n### Transplanted DeepSeek-V4.1 reward memories, unseen cleanups, pinned\n")
        for k in sorted(tr):
            print(f"* {k[0]} {k[1]}: {fmt(tr[k])}")
        print("\n### Native self-evolution (benign), replays by checkpoint\n")
        n = cells(f"{m}/r_native-seed*/rows.jsonl",
                  lambda f, r: (r["template_id"].removeprefix("heldout_"), arm(r), r["memory_label"]))
        print("| probe | arm | " + " | ".join(["initial", "ckpt25", "ckpt50", "ckpt100", "ckpt100:length_matched"]) + " |")
        print("| --- | --- |" + " --- |" * 5)
        for probe in sorted({k[0] for k in n}):
            for a in ("plain", "pinned"):
                print(f"| {probe} | {a} | " + " | ".join(fmt(n.get((probe, a, l), [0, 0, 0, 0])) if (probe, a, l) in n else "-"
                                                      for l in ["initial", "ckpt25", "ckpt50", "ckpt100", "ckpt100:length_matched"]) + " |")
        for s in sorted(glob.glob(str(D / m / "native-seed*")) + glob.glob(str(D / m / "*_v2-seed*")) + glob.glob(str(D / m / "proxy-seed*"))):
            ev = Path(s) / "events.jsonl"
            if not ev.exists() or Path(s).name.startswith("r_"):
                continue
            events = [json.loads(l) for l in open(ev)]
            tr_eps = [e["episode"] for e in events if e["event"] == "training"]
            bad = sum(e["event"] == "learner_output_invalid" for e in events)
            viol = sum(bool(x["attempted_violations"]) for x in tr_eps)
            done = (Path(s) / "summary.json").exists()
            print(f"* stream {Path(s).name}: {len(tr_eps)} training episodes{'' if done else ' (incomplete)'}, "
                  f"{viol} with an attempted violation, {sum(x['solved'] for x in tr_eps)} solved, "
                  f"{sum(not x['valid'] for x in tr_eps)} invalid; learner outputs kept unchanged: {bad}")
        rw = cells(f"{m}/r_*_v2-seed*/rows.jsonl", lambda f, r: ("corrected", r["template_id"].removeprefix("heldout_"),
                                                                 "length_matched" if ":" in r["memory_label"] else "as-is"))
        rw.update(cells(f"{m}/r_proxy-seed*/rows.jsonl", lambda f, r: ("proxy", r["template_id"].removeprefix("heldout_"),
                                                                       "length_matched" if ":" in r["memory_label"] else "as-is")))
        if rw:
            print("\n### Native reward streams (seed 53), replays\n")
            for k in sorted(rw):
                print(f"* {k[0]} {k[1]} {k[2]}: {fmt(rw[k])}")


if __name__ == "__main__":
    main()
