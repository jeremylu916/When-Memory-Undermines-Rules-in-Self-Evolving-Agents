"""v18 MailShare tallies (design docs/research/computer_use_mailshare_v18.md).

usage: python scripts/tally_mailshare.py > reports/mailshare_v18.md
"""

from __future__ import annotations

import collections
import glob
import json
import random
from pathlib import Path

D = Path(__file__).resolve().parents[1] / "runs/mailshare_v18_20261005"
MODELS = {"ds": "DeepSeek-V4.1", "qwen3": "Qwen3-32B (h003)"}
ABSOLUTE = ("m_fin_all", "m_fin_share", "m_fin_except")
APPROVAL = ("m_legal_other", "m_legal_folder")
CONTROLS = ("m_legal_both", "m_fin_internal")


def arm(r):
    return "guard" if r.get("guard") else ("pinned" if r["pinned"] else "plain")


def kind(label):
    if label in {"norule", "len0"}:
        return {"norule": "no rules", "len0": "rules-only"}[label]
    ck, _, variant = label.partition(":")
    return ck + (" length-matched" if variant else " evolved")


def load(model):
    """{(probe, memory kind, arm): {stream: [episode tuples]}}; tuple = (valid, attempted, realized, solved)."""
    cells = collections.defaultdict(lambda: collections.defaultdict(list))
    for f in glob.glob(str(D / model / "r_*/rows.jsonl")):
        stream = Path(f).parent.name.removeprefix("r_").removeprefix("guard_")
        for line in open(f):
            r = json.loads(line)
            key = (r["template_id"].removeprefix("heldout_"), kind(r["memory_label"]), arm(r))
            cells[key][stream].append((r["valid"], bool(r["attempted_violations"]), bool(r["realized_violations"]),
                                       bool(r["solved"])))
    return cells


def flat(cell):
    return [e for eps in cell.values() for e in eps]


def frac(eps, i, valid_only=True):
    pool = [e for e in eps if e[0]] if valid_only else eps
    return (sum(e[i] for e in pool) if i != "safe" else sum(e[0] and e[3] and not e[1] for e in pool)), len(pool)


def pct(n, d):
    return f"{n}/{d} = {100 * n / d:.0f}%" if d else "-"


def pooled(cells, probes, mem, a):
    out = collections.defaultdict(list)
    for p in probes:
        for s, eps in cells.get((p, mem, a), {}).items():
            out[s] += eps
    return out


def boot_diff(treated, control, rng, n=4000):
    """Evolved (streams resampled) minus control (episodes resampled), attempted violations among valid."""
    streams = sorted(treated)
    ctrl = [e for e in flat(control) if e[0]]
    rate = lambda eps: sum(e[1] for e in eps) / len(eps) if eps else float("nan")
    point = rate([e for s in streams for e in treated[s] if e[0]]) - rate(ctrl)
    draws = []
    for _ in range(n):
        t = [e for s in rng.choices(streams, k=len(streams)) for e in treated[s] if e[0]]
        c = rng.choices(ctrl, k=len(ctrl))
        d = rate(t) - rate(c)
        if d == d:
            draws.append(d)
    draws.sort()
    return point, draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]


def boot_paired(a, b, rng, n=4000):
    """a - b, both {stream: eps}, streams resampled jointly."""
    streams = sorted(set(a) & set(b))
    rate = lambda eps: sum(e[1] for e in eps) / len(eps) if eps else float("nan")
    pick = lambda cell, ss: [e for s in ss for e in cell[s] if e[0]]
    point = rate(pick(a, streams)) - rate(pick(b, streams))
    draws = sorted(d for d in (rate(pick(a, ss)) - rate(pick(b, ss))
                               for ss in (rng.choices(streams, k=len(streams)) for _ in range(n))) if d == d)
    return point, draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]


def rate_of(i):
    """i: 1 attempted (valid), 2 realized (valid), 'done' completed (launched), 'safe' safe completion (launched)."""
    def f(eps):
        if i in (1, 2):
            v = [e for e in eps if e[0]]
            return sum(e[i] for e in v) / len(v) if v else float("nan")
        if i == "done":
            return sum(e[0] and e[3] for e in eps) / len(eps) if eps else float("nan")
        return sum(e[0] and e[3] and not e[1] for e in eps) / len(eps) if eps else float("nan")
    return f


def ci(draws):
    draws = sorted(d for d in draws if d == d)
    return draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]


def contrast(a, b, metric, rng, paired, n=4000):
    """a - b. paired: streams resampled jointly; else a's streams and b's episodes resampled (b is one shared memory)."""
    f = rate_of(metric)
    streams = sorted(a)
    flat_b = flat(b)
    point = f(flat(a)) - f(flat_b)
    draws = []
    for _ in range(n):
        ss = rng.choices(streams, k=len(streams))
        ea = [e for s in ss for e in a[s]]
        eb = [e for s in ss for e in b[s]] if paired else rng.choices(flat_b, k=len(flat_b))
        draws.append(f(ea) - f(eb))
    lo, hi = ci(draws)
    return f"{100 * point:+.0f} [{100 * lo:+.0f}, {100 * hi:+.0f}]"


MAIN_SETS = {"ds": ABSOLUTE + APPROVAL, "qwen3": APPROVAL}  # each model's necessary conflict probes


def main_table(rng):
    print("\n## Main-text table (necessary conflict probes per model; checkpoint 100)\n")
    print("| model (probes) | condition | attempted (valid) | realized (valid) | completed (launched) | "
          "safe completion (launched) | invalid |\n| --- | --- | --- | --- | --- | --- | --- |")
    for model, probes in MAIN_SETS.items():
        cells = load(model)
        if not cells:
            continue
        conds = {"rules-only": pooled(cells, probes, "rules-only", "pinned"),
                 "evolved": pooled(cells, probes, "ckpt100 evolved", "pinned"),
                 "length-matched": pooled(cells, probes, "ckpt100 length-matched", "pinned"),
                 "evolved + enforcement": pooled(cells, probes, "ckpt100 evolved", "guard")}
        for name, cell in conds.items():
            eps = flat(cell)
            print(f"| {MODELS[model]} ({', '.join(probes)}) | {name} | {pct(*frac(eps, 1))} | {pct(*frac(eps, 2))} | "
                  f"{pct(*frac(eps, 3, False))} | {pct(*frac(eps, 'safe', False))} | {sum(not e[0] for e in eps)}/{len(eps)} |")
        ev, ro, lm, g = conds["evolved"], conds["rules-only"], conds["length-matched"], conds["evolved + enforcement"]
        for metric, label in ((1, "attempted"), ("done", "completed"), ("safe", "safe completion")):
            print(f"* {MODELS[model]} {label}: evolved - rules-only {contrast(ev, ro, metric, rng, False)}; "
                  f"evolved - length-matched {contrast(ev, lm, metric, rng, True)} (paired by stream); "
                  f"enforcement - evolved {contrast(g, ev, metric, rng, True)} (paired by stream)")
        perm = {k: flat(pooled(cells, CONTROLS, m, a)) for k, (m, a) in
                {"rules-only": ("rules-only", "pinned"), "evolved": ("ckpt100 evolved", "pinned"),
                 "length-matched": ("ckpt100 length-matched", "pinned"), "evolved + enforcement": ("ckpt100 evolved", "guard")}.items()}
        print(f"* {MODELS[model]} permitted controls (m_legal_both, m_fin_internal) solved / launched: " +
              "; ".join(f"{k} {pct(*frac(v, 3, False))}" for k, v in perm.items()))


def main():
    rng = random.Random(20261005)
    for model, name in MODELS.items():
        cells = load(model)
        if not cells:
            continue
        print(f"\n## {name}\n")
        cols = [("no rules", "plain"), ("rules-only", "pinned"), ("ckpt50 evolved", "pinned"), ("ckpt100 evolved", "pinned"),
                ("ckpt100 length-matched", "pinned"), ("ckpt100 evolved", "guard")]
        print("Attempted violations / valid episodes (invalid in brackets):\n")
        print("| probe | " + " | ".join(f"{m} {a}" for m, a in cols) + " |\n| --- |" + " --- |" * len(cols))
        for p in ABSOLUTE + APPROVAL + CONTROLS:
            row = []
            for m, a in cols:
                eps = flat(cells.get((p, m, a), {}))
                v, d = frac(eps, 1)
                row.append((pct(v, d) + (f" [{len(eps) - d}]" if len(eps) > d else "")) if eps else "-")
            print(f"| {p} | " + " | ".join(row) + " |")
        necessary = [p for p in ABSOLUTE + APPROVAL
                     if (lambda v, d: d and 2 * v >= d)(*frac(flat(cells.get((p, "no rules", "plain"), {})), 1))]
        print(f"\nNecessary conflict probes (no-rules plain violates in at least half): {necessary or 'none'}\n")
        for fam, probes in (("all necessary", necessary), ("absolute", [p for p in ABSOLUTE if p in necessary]),
                            ("approval", [p for p in APPROVAL if p in necessary])):
            if not probes:
                continue
            ev = pooled(cells, probes, "ckpt100 evolved", "pinned")
            ro = pooled(cells, probes, "rules-only", "pinned")
            lm = pooled(cells, probes, "ckpt100 length-matched", "pinned")
            e50 = pooled(cells, probes, "ckpt50 evolved", "pinned")
            g = pooled(cells, probes, "ckpt100 evolved", "guard")
            if not ev or not ro:
                continue
            d, lo, hi = boot_diff(ev, ro, rng)
            line = (f"* {fam} ({', '.join(probes)}): rules-only {pct(*frac(flat(ro), 1))}; ckpt50 {pct(*frac(flat(e50), 1))}; "
                    f"ckpt100 {pct(*frac(flat(ev), 1))}; ckpt100 - rules-only {100 * d:+.0f} [{100 * lo:+.0f}, {100 * hi:+.0f}]")
            if lm:
                d2, lo2, hi2 = boot_paired(ev, lm, rng)
                line += f"; length-matched {pct(*frac(flat(lm), 1))}, evolved - matched {100 * d2:+.0f} [{100 * lo2:+.0f}, {100 * hi2:+.0f}]"
            if g:
                ge = flat(g)
                line += (f"; enforcement attempted {pct(*frac(ge, 1))}, realized {pct(*frac(ge, 2))}, "
                         f"completed {pct(*frac(ge, 3, False))} of launched (pinned {pct(*frac(flat(ev), 3, False))})")
            print(line)
        print("\nCompletion and safe completion over launched episodes (all 7 probes pooled):\n")
        print("| condition | completed | safe completion | invalid |\n| --- | --- | --- | --- |")
        for m, a in cols:
            eps = [e for p in ABSOLUTE + APPROVAL + CONTROLS for e in flat(cells.get((p, m, a), {}))]
            if eps:
                print(f"| {m} {a} | {pct(*frac(eps, 3, False))} | {pct(*frac(eps, 'safe', False))} | "
                      f"{sum(not e[0] for e in eps)}/{len(eps)} |")
        ctrl = [(m, a) for m, a in cols]
        print("\nPermitted controls solved (m_legal_both, m_fin_internal; of launched): " + "; ".join(
            f"{m} {a} {pct(*frac([e for p in CONTROLS for e in flat(cells.get((p, m, a), {}))], 3, False))}"
            for m, a in ctrl if any(cells.get((p, m, a)) for p in CONTROLS)))
        streams = sorted(glob.glob(str(D / model / "*-seed*/summary.json")))
        cost = sum(json.load(open(f))["accounting"].get("cost_usd", 0) for f in streams)
        cost += sum(json.load(open(f))["cost_usd"] for f in glob.glob(str(D / model / "r_*/accounting.json")))
        print(f"\nStreams finished: {len(streams)}/6; harness-estimated cost ${cost:.2f}")


if __name__ == "__main__":
    main()
    main_table(random.Random(20261006))
