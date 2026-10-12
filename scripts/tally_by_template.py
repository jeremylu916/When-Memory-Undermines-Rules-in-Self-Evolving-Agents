"""Attempted violations / valid by memory group, arm and template for replay dirs."""
import collections, json, sys
from pathlib import Path

def group(stream, label):
    if label in {"norule", "initial", "len0"}:
        return "rules_only" if label in {"initial", "len0"} else "norule"
    if label.startswith("len"):
        return f"neutral_{label[3:]}"
    variant = label.partition(":")[2] or "none"
    return f"{stream.rsplit('-seed', 1)[0]}:{variant}"

tab = collections.defaultdict(lambda: [0, 0, 0])
for d in sys.argv[1:]:
    for rows in Path(d).glob("*/rows.jsonl"):
        for line in rows.open():
            r = json.loads(line)
            arm = ("task_rules" if r.get("rules_in_task") else "system_selfcheck" if r.get("self_check") else "system" if r.get("system_rules") else "pinned" if r["pinned"] else "plain")
            k = (group(rows.parent.name, r["memory_label"]), arm, r["template_id"].replace("heldout_", ""))
            tab[k][2] += 1
            if r["valid"]:
                tab[k][1] += 1; tab[k][0] += bool(r["attempted_violations"])
templates = sorted({k[2] for k in tab})
print(f"{'memory':28} {'arm':7} " + " ".join(f"{t[:18]:>18}" for t in templates) + "     TOTAL")
for g, arm in sorted({(k[0], k[1]) for k in tab}):
    cells = [tab.get((g, arm, t), [0, 0, 0]) for t in templates]
    a, v = sum(c[0] for c in cells), sum(c[1] for c in cells)
    print(f"{g:28} {arm:7} " + " ".join(f"{c[0]:>9}/{c[1]:<3}({c[2]-c[1]}i)" for c in cells) + f"  {a:>4}/{v}")
