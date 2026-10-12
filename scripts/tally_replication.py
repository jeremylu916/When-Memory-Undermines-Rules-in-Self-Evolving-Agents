"""Pool replay rows across job dirs: attempts/valid by memory group and arm.

Usage: python scripts/tally_replication.py LABEL DIR [DIR ...]
Job dir names like approval_default-seed17 or default-seed17_resume map to
the evolved memories; norule / initial / len0 / lenN are controls.
"""
import collections, json, re, sys
from pathlib import Path

ARMS = ("plain", "pinned", "system", "system_selfcheck", "task_rules")


def arm_of(r):
    return ("task_rules" if r.get("rules_in_task") else "system_selfcheck" if r.get("self_check")
            else "system" if r.get("system_rules") else "pinned" if r["pinned"] else "plain")


def group_of(label):
    if label == "norule":
        return "norule"
    if label in {"initial", "len0"}:
        return "rules_only"
    if re.fullmatch(r"len\d+", label):
        return f"neutral_{label[3:]}"
    return "evolved:" + (label.partition(":")[2] or "none")


label, dirs = sys.argv[1], [Path(d) for d in sys.argv[2:]]
tab = collections.defaultdict(lambda: [0, 0, 0])
for d in dirs:
    for line in (d / "rows.jsonl").open():
        r = json.loads(line)
        k = (group_of(r["memory_label"]), arm_of(r))
        tab[k][2] += 1
        if r["valid"]:
            tab[k][1] += 1
            tab[k][0] += bool(r["attempted_violations"])
print(f"== {label}")
print(f"  {'memory':24}" + "".join(f"{a:>18}" for a in ARMS))
for g in sorted({k[0] for k in tab}, key=lambda g: (g.startswith("evolved"), g)):
    cells = [tab.get((g, a)) for a in ARMS]
    print(f"  {g:24}" + "".join(f"{(f'{c[0]}/{c[1]} ({c[2]-c[1]}i)' if c else '-'):>18}" for c in cells))
