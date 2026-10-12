"""v15 C steering tallies: violations / valid, permitted part solved, invalid, per condition and probe."""
import collections, glob, json, re, sys
c = collections.defaultdict(lambda: [0, 0, 0, 0])
for f in glob.glob(sys.argv[1] + "/rows.jsonl"):
    cond = re.sub(r"_(default|success_only)-seed\d+$", "", f.split("/")[-2])
    for line in open(f):
        r = json.loads(line)
        k = (cond, r["template_id"].removeprefix("heldout_"))
        c[k][3] += 1
        if r["valid"]:
            c[k][2] += 1
            c[k][0] += bool(r["attempted_violations"])
            c[k][1] += bool(r["solved"])
conds = sorted({k[0] for k in c})
probes = sorted({k[1] for k in c})
print("| condition | " + " | ".join(probes) + " | all: viol / solved / invalid |")
print("| --- |" + " --- |" * (len(probes) + 1))
for cond in conds:
    cells, tot = [], [0, 0, 0, 0]
    for p in probes:
        v, s, n, l = c.get((cond, p), [0, 0, 0, 0])
        tot = [a + b for a, b in zip(tot, (v, s, n, l))]
        cells.append(f"{v}/{n} viol, {s} solved" + (f", {l - n} inv." if l > n else ""))
    v, s, n, l = tot
    print(f"| {cond} | " + " | ".join(cells) + f" | {100 * v / max(1, n):.0f}% / {100 * s / max(1, n):.0f}% / {l - n} |")
