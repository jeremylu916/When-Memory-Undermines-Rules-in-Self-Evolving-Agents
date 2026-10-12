"""Per-probe screen summary for a steering-candidate model: violations/valid by memory and arm."""
import collections, glob, json, sys
c = collections.defaultdict(lambda: [0, 0, 0])
for f in glob.glob(sys.argv[1] + "/*/rows.jsonl"):
    for line in open(f):
        r = json.loads(line)
        lab = r["memory_label"].replace("ckpt100", "evolved").replace("len0", "rules-only")
        key = (r["template_id"].removeprefix("heldout_"), lab, "pinned" if r["pinned"] else "plain")
        c[key][2] += 1
        if r["valid"]:
            c[key][1] += 1
            c[key][0] += bool(r["attempted_violations"])
for k in sorted(c):
    v, n, l = c[k]
    print(f"{k[0]:20s} {k[1]:24s} {k[2]:6s} {v}/{n}" + (f" ({l - n} inv.)" if l > n else ""))
