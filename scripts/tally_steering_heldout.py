"""v15 C held-out steering table: per condition and probe group, violations among valid episodes, and
completion and safe completion (completed with no violation) over all launched episodes; paired
cluster-bootstrap contrasts against base (clusters = source memories)."""
import collections, glob, json, random, re

GROUPS = {"stale": "held-out records conflict (c_stale_sweep)", "conf": "confirmation probes (5)",
          "u": "approved utility tasks (10)", "r": "reward proxy memories, unseen cleanups"}
CONDS = ("base", "steer_24_0.75", "rand_24_0.75", "parent_steer_24_1")
COND = re.compile(r"(base|parent_steer_24_1|steer_24_0\.75|rand_24_0\.75)")
eps = collections.defaultdict(lambda: collections.defaultdict(list))  # (group, cond) -> cluster -> [(valid, viol, solved)]
for f in glob.glob("runs/steering_v15_20261003/[hur]_*/rows.jsonl"):
    name = f.split("/")[-2]
    cond = COND.search(name).group(1)
    group = "stale" if "_stale_" in name else "conf" if "_conf_" in name else name.split("_")[0]
    cluster = name[COND.search(name).end() + 1:]
    for line in open(f):
        r = json.loads(line)
        eps[(group, cond)][cluster].append((r["valid"], bool(r["attempted_violations"]), bool(r["solved"])))


def stat(e, kind):
    if kind == "viol":
        v = [x for x in e if x[0]]
        return sum(x[1] for x in v) / len(v) if v else float("nan")
    if kind == "done":
        return sum(x[0] and x[2] for x in e) / len(e)
    return sum(x[0] and x[2] and not x[1] for x in e) / len(e)  # safe completion


def contrast(group, a, b, kind, rng):
    clusters = sorted(eps[(group, b)])
    pool = lambda cond, cs: [x for c in cs for x in eps[(group, cond)][c]]
    point = stat(pool(a, clusters), kind) - stat(pool(b, clusters), kind)
    draws = []
    for _ in range(2000):
        cs = [rng.choice(clusters) for _ in clusters]
        d = stat(pool(a, cs), kind) - stat(pool(b, cs), kind)
        if d == d:
            draws.append(d)
    draws.sort()
    lo, hi = draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]
    return f"{100 * point:+.0f} [{100 * lo:+.0f}, {100 * hi:+.0f}]"


print("| probes | condition | violations / valid | completed / launched | safe completion / launched | invalid / launched |")
print("| --- | --- | --- | --- | --- | --- |")
for g, gname in GROUPS.items():
    for cond in CONDS:
        e = [x for c in eps[(g, cond)].values() for x in c]
        if not e:
            continue
        n, viol = sum(x[0] for x in e), sum(x[0] and x[1] for x in e)
        done, safe = sum(x[0] and x[2] for x in e), sum(x[0] and x[2] and not x[1] for x in e)
        pct = lambda a, d: f"{a}/{d} = {100 * a / d:.0f}%" if d else "-"
        print(f"| {gname} | {cond} | {pct(viol, n)} | {pct(done, len(e))} | {pct(safe, len(e))} | {len(e) - n}/{len(e)} |")
rng = random.Random(0)
print("\nPaired contrasts, percentage points [95% cluster bootstrap over source memories]:\n")
print("| probes | contrast | violations (valid) | completed (launched) | safe completion (launched) |")
print("| --- | --- | --- | --- | --- |")
for g, gname in GROUPS.items():
    pairs = [(a, "base") for a in CONDS[1:]] + ([("parent_steer_24_1", "rand_24_0.75")] if g == "r" else [])
    for a, b in pairs:
        print(f"| {gname} | {a} - {b} | " + " | ".join(contrast(g, a, b, k, rng) for k in ("viol", "done", "safe")) + " |")
