"""v20 part A analysis: rule-retention scores (A1, A2), score vs. behavior (A3), steering along the axis (A4).

usage: python scripts/analyze_rule_axis.py > reports/rule_axis_v20.md   (writes docs/figures/fig_rule_axis*.pdf/png)
"""

from __future__ import annotations

import collections
import glob
import json
import random
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "runs/rule_axis_v20_20261005"
L = json.loads((D / "layer_selection.json").read_text())["chosen"]
BLUE, ORANGE, AQUA, GREY, INK, MUTED = "#2a78d6", "#eb6834", "#1baf7a", "#8b8a86", "#0b0b0b", "#52514e"
rng = random.Random(20261005)


def scores():
    """Per state: rule-retention score, 0 = no-rule memory, 1 = rules-only memory (same probe, state and arm)."""
    rows = [json.loads(l) for l in open(D / f"projections_L{L}.jsonl")]
    ref = {}
    for r in rows:
        if r["kind"] in {"no rules", "rules-only"}:
            ref[(r["probe"], r["state"], r["arm"], r["kind"])] = r["projection"]
    for r in rows:
        lo, hi = ref[(r["probe"], r["state"], r["arm"], "no rules")], ref[(r["probe"], r["state"], r["arm"], "rules-only")]
        r["score"] = (r["projection"] - lo) / (hi - lo)
    return rows


def boot(streams, stat, n=4000):
    keys = sorted(streams)
    draws = sorted(stat([streams[k] for k in rng.choices(keys, k=len(keys))]) for _ in range(n))
    return draws[int(0.025 * n)], draws[int(0.975 * n) - 1]


def mean(x):
    return float(np.mean(x)) if len(x) else float("nan")


def curves(rows, family, arm, kind):
    """{stream: {episode: mean score over evaluation probes and both states}}."""
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        if r["group"] == "evolved" and r["family"] == family and r["arm"] == arm and r["kind"] == kind and r["split"] == "evaluation":
            out[r["stream"]][r["episode"]].append(r["score"])
    return {s: {k: mean(v) for k, v in e.items()} for s, e in out.items()}


def spearman(xs, ys):
    rx, ry = np.argsort(np.argsort(xs)), np.argsort(np.argsort(ys))
    return float(np.corrcoef(rx, ry)[0, 1])


def report_a1_a2(rows):
    print(f"## Rule-retention axis, Phi-4, layer {L}\n")
    print("Scores: 0 = no-rule memory, 1 = rules-only memory, at the same probe, decision state and arm; "
          "evaluation probes only; stream bootstrap (6 streams).\n")
    print("| family | arm | memory | mean score, all episodes | episode 100 | Spearman(episode, score) |\n"
          "| --- | --- | --- | --- | --- | --- |")
    for family in ("absolute", "records"):
        for arm in ("pinned", "plain"):
            for kind in ("evolved", "length-matched", "rule-ablated"):
                c = curves(rows, family, arm, kind)
                allm = {s: list(e.values()) for s, e in c.items()}
                lo, hi = boot(allm, lambda ls: mean([x for l in ls for x in l]))
                last = {s: [e[100]] for s, e in c.items()}
                lo2, hi2 = boot(last, lambda ls: mean([x for l in ls for x in l]))
                rho = {s: [spearman(sorted(e), [e[k] for k in sorted(e)])] for s, e in c.items()}
                lo3, hi3 = boot(rho, lambda ls: mean([x for l in ls for x in l]))
                print(f"| {family} | {arm} | {kind} | {mean([x for v in allm.values() for x in v]):.2f} [{lo:.2f}, {hi:.2f}] | "
                      f"{mean([v[0] for v in last.values()]):.2f} [{lo2:.2f}, {hi2:.2f}] | "
                      f"{mean([v[0] for v in rho.values()]):+.2f} [{lo3:+.2f}, {hi3:+.2f}] |")
    print("\nPaired contrasts (pinned, evaluation probes, all episodes; stream bootstrap):\n")
    for family in ("absolute", "records"):
        ev, lm, ab = (curves(rows, family, "pinned", k) for k in ("evolved", "length-matched", "rule-ablated"))
        for name, other in (("evolved - length-matched", lm), ("evolved - rule-ablated", ab)):
            d = {s: [ev[s][k] - other[s][k] for k in ev[s]] for s in ev}
            lo, hi = boot(d, lambda ls: mean([x for l in ls for x in l]))
            print(f"* {family}: {name} {mean([x for v in d.values() for x in v]):+.2f} [{lo:+.2f}, {hi:+.2f}]")
        d1 = {s: [ev[s][k] - 1 for k in ev[s]] for s in ev}
        lo, hi = boot(d1, lambda ls: mean([x for l in ls for x in l]))
        print(f"* {family}: evolved - rules-only {mean([x for v in d1.values() for x in v]):+.2f} [{lo:+.2f}, {hi:+.2f}]")


def marginal(rows):
    """Post hoc: the rule's marginal effect = score(memory) - score(same memory without its rule lines), so that the
    rule alone (rules-only minus no-rule) is 1. Evolved: evolved - rule-ablated at the same stream and episode;
    neutral padding: (rules + N chars) - (N chars only)."""
    key = lambda r: (r["probe"], r["state"], r["arm"], r["stream"], r["episode"])
    by = collections.defaultdict(dict)
    for r in rows:
        if r["split"] == "evaluation":
            by[key(r)][r["kind"]] = r
    evo = collections.defaultdict(lambda: collections.defaultdict(list))  # (family, arm) -> stream -> [(episode, m)]
    pad = collections.defaultdict(list)                                    # (family, arm, N) -> [m]
    for k, d in by.items():
        if "evolved" in d and "rule-ablated" in d:
            r = d["evolved"]
            evo[(r["family"], r["arm"])][r["stream"]].append((r["episode"], d["evolved"]["score"] - d["rule-ablated"]["score"]))
        if "rules+pad" in d and "pad only" in d:
            r = d["rules+pad"]
            pad[(r["family"], r["arm"], r["episode"])].append(d["rules+pad"]["score"] - d["pad only"]["score"])
    return evo, pad


def report_marginal(rows):
    evo, pad = marginal(rows)
    print("\n## Post hoc: the rule's marginal effect on the decision state (rules alone = 1)\n")
    print("| family | arm | neutral padding 250 / 1000 / 4000 chars | evolved, all episodes | evolved, episodes 5-25 | "
          "evolved, episodes 80-100 | Spearman(episode, effect) |\n| --- | --- | --- | --- | --- | --- | --- |")
    for family in ("absolute", "records"):
        for arm in ("pinned", "plain"):
            e = evo[(family, arm)]
            allm = {s: [m for _, m in v] for s, v in e.items()}
            early = {s: [m for k, m in v if k <= 25] for s, v in e.items()}
            late = {s: [m for k, m in v if k >= 80] for s, v in e.items()}
            def ci(d):
                lo, hi = boot(d, lambda ls: mean([x for l in ls for x in l]))
                return f"{mean([x for v in d.values() for x in v]):.2f} [{lo:.2f}, {hi:.2f}]"
            rho = {}
            for st, v in e.items():
                per = collections.defaultdict(list)
                for k, m in v:
                    per[k].append(m)
                ks = sorted(per)
                rho[st] = [spearman(ks, [mean(per[k]) for k in ks])]
            pads = " / ".join(f"{mean(pad[(family, arm, n)]):.2f}" for n in (250, 1000, 4000))
            print(f"| {family} | {arm} | {pads} | {ci(allm)} | {ci(early)} | {ci(late)} | {ci(rho)} |")


def fig_marginal(rows):
    """One standalone figure per rule family: fig_rule_marginal_absolute, fig_rule_marginal_records."""
    from matplotlib.lines import Line2D
    evo, pad = marginal(rows)
    for family, title, name in (("absolute", "Absolute rule (security.txt)", "absolute"),
                                ("records", "Approval-gated records rule", "records")):
        fig, ax = plt.subplots(figsize=(3.4, 2.9), constrained_layout=True)
        for arm, color, ls, marker in (("pinned", BLUE, "-", "o"), ("plain", ORANGE, "--", "s")):
            e = evo[(family, arm)]
            per = collections.defaultdict(lambda: collections.defaultdict(list))
            for st, v in e.items():
                for k, m in v:
                    per[k][st].append(m)
            ks = sorted(per)
            m = [mean([x for l in per[k].values() for x in l]) for k in ks]
            band = [boot({st: per[k][st] for st in per[k]}, lambda ls: mean([x for l in ls for x in l]), 1000) for k in ks]
            ax.plot([0] + ks, [1.0] + m, color=color, ls=ls, lw=1.6, marker=marker, ms=3.2, markeredgecolor="white",
                    markeredgewidth=0.5, label=f"evolved memory, rules {arm}")
            ax.fill_between(ks, [b[0] for b in band], [b[1] for b in band], color=color, alpha=0.15, lw=0)
            ax.axhline(mean(pad[(family, arm, 4000)]), color=color, lw=0.9, ls=":")
        ax.axhline(1, color=AQUA, lw=0.9)
        ax.axhline(0, color=MUTED, lw=0.9)
        ax.text(102, 1.0, "rules\nalone", fontsize=6, color=AQUA, va="center")
        ax.text(102, 0.0, "no\neffect", fontsize=6, color=MUTED, va="center")
        ax.set_title(title, fontsize=8, loc="left", color=INK)
        ax.set_ylim(-0.08, 1.12)
        ax.set_xlim(-3, 114)
        ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
        ax.tick_params(labelsize=7, colors=MUTED)
        ax.grid(axis="y", color="#e6e5e0", lw=0.6)
        ax.set_ylabel("rule's marginal effect", fontsize=7, color=MUTED)
        ax.set_xlabel("training episodes", fontsize=7, color=MUTED)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        handles, labels = ax.get_legend_handles_labels()
        handles.append(Line2D([], [], color=MUTED, ls=":", lw=0.9))
        labels.append("rules + 4,000 neutral chars")
        fig.legend(handles, labels, fontsize=6.5, frameon=False, loc="outside lower center", ncol=1)
        out = ROOT / f"docs/figures/fig_rule_marginal_{name}"
        fig.savefig(str(out) + ".pdf")
        fig.savefig(str(out) + ".png", dpi=200)
        plt.close(fig)


def fig_curves(rows):
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.4), constrained_layout=True, sharey=True)
    for ax, family, title in zip(axes, ("absolute", "records"), ("(a) absolute rule (security.txt)", "(b) records rule")):
        for kind, color, ls in (("evolved", BLUE, "-"), ("length-matched", GREY, "--"), ("rule-ablated", ORANGE, ":")):
            c = curves(rows, family, "pinned", kind)
            eps = sorted(next(iter(c.values())))
            m = [mean([c[s][k] for s in c]) for k in eps]
            band = [boot({s: [c[s][k]] for s in c}, lambda ls: mean([x for l in ls for x in l]), 1000) for k in eps]
            ax.plot([0] + eps, [1.0] + m, color=color, ls=ls, lw=2, marker="o", ms=3.5, label=kind)
            ax.fill_between(eps, [b[0] for b in band], [b[1] for b in band], color=color, alpha=0.15, lw=0)
        ax.axhline(1, color=AQUA, lw=1, ls="-", alpha=0.8)
        ax.axhline(0, color=MUTED, lw=1, ls="-", alpha=0.6)
        ax.text(101, 1.0, "rules-only", fontsize=6.5, color=AQUA, va="center")
        ax.text(101, 0.0, "no rule", fontsize=6.5, color=MUTED, va="center")
        ax.set_title(title, fontsize=8, loc="left", color=INK)
        ax.set_xlabel("training episodes", fontsize=7, color=MUTED)
        ax.tick_params(labelsize=7, colors=MUTED)
        ax.grid(axis="y", color="#e6e5e0", lw=0.6)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.set_xlim(-3, 112)
    axes[0].set_ylabel("rule-retention score", fontsize=7, color=MUTED)
    axes[0].legend(fontsize=6, frameon=False, loc="lower left")
    out = ROOT / "docs/figures/fig_rule_axis"
    fig.savefig(str(out) + ".pdf")
    fig.savefig(str(out) + ".png", dpi=200)


def rule_direction(H, rows):
    """Unit rule-presence direction, recomputed exactly as estimated (estimation probes, rules in memory only)."""
    idx = {(r["probe"], r["state"], r["arm"], r["episode"], r["kind"]): i for i, r in enumerate(rows)}
    diffs = []
    for i, r in enumerate(rows):
        if r["split"] == "estimation" and r["arm"] == "plain" and r["kind"] in {"rules-only", "rules+pad"}:
            partner = "no rules" if r["kind"] == "rules-only" else "pad only"
            diffs.append(H[i] - H[idx[(r["probe"], r["state"], r["arm"], r["episode"], partner)]])
    v = np.mean(diffs, axis=0)
    return v / np.linalg.norm(v)


def fig_pca(rows):
    """Decision states along the rule-presence direction (file name kept: fig_rule_axis_pca).
    (a) Strip plot: position on the direction for each memory type (0 = no-rule memory, 1 = rules-only memory),
        absolute-rule evaluation probes, both decision states, rules pinned.
    (b) c_settings_purge: x = position on the direction, y = top principal component of what is orthogonal to it.
        Evolved memories are shaded by training episode and joined to their rule-deleted twins; arrows compare the
        rule's effect alone (+1) with its mean effect inside evolved memory."""
    from matplotlib.colors import LinearSegmentedColormap
    H = np.load(D / f"states_L{L}.npy").astype(np.float32)
    u = rule_direction(H, rows)
    assert np.corrcoef(H[:500] @ u, [r["projection"] for r in rows[:500]])[0, 1] > 0.999
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.0, 3.3), constrained_layout=True,
                                   gridspec_kw={"width_ratios": [1, 1.15]})
    # (a) strip plot over the absolute-rule evaluation probes
    kinds = [("no rules", "no-rule memory", MUTED), ("rules-only", "rules-only memory", AQUA),
             ("rules+pad", "rules + neutral text", AQUA), ("length-matched", "rules + interface padding\n(length-matched)", GREY),
             ("rule-ablated", "evolved memory,\nrule lines deleted", ORANGE), ("evolved", "evolved memory", BLUE)]
    sel = [r for r in rows if r["family"] == "absolute" and r["split"] == "evaluation" and r["arm"] == "pinned"]
    jit = np.random.default_rng(0)
    for y, (kind, label, color) in enumerate(kinds):
        xs = np.array([r["score"] for r in sel if r["kind"] == kind])
        ax1.scatter(xs, y + jit.uniform(-0.28, 0.28, len(xs)), s=7 if len(xs) > 30 else 22, color=color,
                    alpha=0.35 if len(xs) > 30 else 0.9, lw=0, zorder=2)
        med = float(np.median(xs))
        ax1.plot([med, med], [y - 0.38, y + 0.38], color=INK, lw=1.4, zorder=3)
    ax1.set_yticks(range(len(kinds)), [k[1] for k in kinds], fontsize=6.5)
    ax1.axvline(0, color=MUTED, lw=0.8, ls=":")
    ax1.axvline(1, color=AQUA, lw=0.8, ls=":")
    ax1.set_xlabel("position on the rule-presence direction\n(0 = no-rule memory, 1 = rules-only memory)", fontsize=7,
                   color=MUTED)
    ax1.set_title("(a) all memory types (black tick: median)", fontsize=7.5, loc="left", color=INK)
    ax1.set_ylim(-0.6, len(kinds) - 0.4)
    # (b) 2-D view on one probe
    pts_i = [i for i, r in enumerate(rows) if r["family"] == "absolute" and r["arm"] == "pinned" and r["state"] == "s0"
             and r["probe"] == "heldout_c_settings_purge"]
    pts = [rows[i] for i in pts_i]
    X = H[pts_i]
    proj = X @ u
    find = lambda kind: next(j for j, r in enumerate(pts) if r["kind"] == kind)
    nr, ro = find("no rules"), find("rules-only")
    x = (proj - proj[nr]) / (proj[ro] - proj[nr])
    R = X - np.outer(proj, u)
    R = R - R.mean(0)
    y = R @ np.linalg.svd(R, full_matrices=False)[2][0]
    y = (y - y[nr]) / (np.abs(y).max() + 1e-9)  # scale only; orientation and spread are arbitrary
    twin = {(r["stream"], r["episode"]): j for j, r in enumerate(pts) if r["kind"] == "rule-ablated"}
    ev = [j for j, r in enumerate(pts) if r["kind"] == "evolved" and (r["stream"], r["episode"]) in twin]
    for j in ev:
        k = twin[(pts[j]["stream"], pts[j]["episode"])]
        ax2.plot([x[k], x[j]], [y[k], y[j]], color="#9fb9de", lw=0.6, zorder=1)
    lm = [j for j, r in enumerate(pts) if r["kind"] == "length-matched"]
    ax2.scatter(x[lm], y[lm], s=12, color=GREY, alpha=0.6, lw=0, marker="s", label="rules + interface padding", zorder=2)
    ab = [twin[(pts[j]["stream"], pts[j]["episode"])] for j in ev]
    ax2.scatter(x[ab], y[ab], s=12, color=ORANGE, alpha=0.75, lw=0, marker="^", label="evolved, rule lines deleted", zorder=3)
    cmap = LinearSegmentedColormap.from_list("ep", ["#bcd3f2", BLUE, "#0b3d7a"])
    sc = ax2.scatter(x[ev], y[ev], s=16, c=[pts[j]["episode"] for j in ev], cmap=cmap, vmin=5, vmax=100, lw=0.3,
                     edgecolors="white", label="evolved memory", zorder=4)
    for j, marker, color, label in ((nr, "X", INK, "no-rule memory"), (ro, "*", AQUA, "rules-only memory")):
        ax2.scatter([x[j]], [y[j]], s=110, marker=marker, color=color, edgecolors="white", linewidths=0.6, zorder=6,
                    label=label)
    # arrows: the rule alone vs. the rule inside evolved memory (mean over evolved/twin pairs)
    y0 = float(np.min(y)) - 0.22
    ax2.annotate("", xy=(1, y0), xytext=(0, y0), arrowprops=dict(arrowstyle="-|>", color=AQUA, lw=2, mutation_scale=11))
    ax2.text(0.5, y0 - 0.06, "rule alone: +1", fontsize=6.8, color=AQUA, ha="center", va="top")
    gain = float(np.mean([x[j] - x[twin[(pts[j]["stream"], pts[j]["episode"])]] for j in ev]))
    xa, ya = float(np.mean(x[ab])), float(np.max(y)) + 0.22
    ax2.annotate("", xy=(xa + gain, ya), xytext=(xa, ya),
                 arrowprops=dict(arrowstyle="-|>", color=BLUE, lw=2, mutation_scale=11))
    ax2.text(xa + gain / 2, ya + 0.06, f"rule inside evolved\nmemory: {gain:+.2f}", fontsize=6.8, color=BLUE,
             ha="center", va="bottom")
    ax2.axvline(0, color=MUTED, lw=0.8, ls=":")
    ax2.axvline(1, color=AQUA, lw=0.8, ls=":")
    cb = fig.colorbar(sc, ax=ax2, shrink=0.55, pad=0.01, aspect=25)
    cb.set_label("training episodes", fontsize=6.5, color=MUTED)
    cb.ax.tick_params(labelsize=6, colors=MUTED)
    ax2.set_xlabel("position on the rule-presence direction", fontsize=7, color=MUTED)
    ax2.set_ylabel("top orthogonal component (scaled)", fontsize=7, color=MUTED)
    ax2.set_title("(b) one probe (c_settings_purge); lines join each evolved\nmemory to the same memory without its rule lines",
                  fontsize=7.5, loc="left", color=INK)
    ax2.set_ylim(float(np.min(y)) - 0.45, float(np.max(y)) + 0.6)
    ax2.legend(fontsize=6, frameon=False, loc="upper left", markerscale=0.8, borderaxespad=0.2)
    for ax in (ax1, ax2):
        ax.tick_params(labelsize=6.5, colors=MUTED)
        ax.grid(axis="x", color="#e6e5e0", lw=0.6)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    out = ROOT / "docs/figures/fig_rule_axis_pca"
    fig.savefig(str(out) + ".pdf")
    fig.savefig(str(out) + ".png", dpi=200)
    plt.close(fig)


def behavior(prefix):
    """{(memory label, arm, template): [valid, violation, solved]} for replay dirs runs/.../<prefix>*."""
    out = {}
    for f in glob.glob(str(D / f"{prefix}*/rows.jsonl")):
        stream = Path(f).parent.name.split("_", 1)[1].rsplit("_", 1)[0] if "seed" in f else "controls"
        for line in open(f):
            r = json.loads(line)
            out[(stream, r["memory_label"], "pinned" if r["pinned"] else "plain", r["template_id"])] = (
                r["valid"], bool(r["attempted_violations"]), bool(r["solved"]))
    return out


def report_a3(rows):
    beh = behavior("b_")
    if not beh:
        return
    sc = {}
    for r in rows:
        if r["group"] == "evolved" and r["family"] == "absolute" and r["split"] == "evaluation":
            label = f"ckpt{r['episode']}" + (":length_matched" if r["kind"] == "length-matched" else "")
            if r["kind"] in {"evolved", "length-matched"}:
                sc.setdefault((r["stream"], label, r["arm"], r["probe"]), []).append(r["score"])
    pairs = [(mean(sc[k]), beh[k][1], k[0]) for k in sc if k in beh and beh[k][0]]
    by_stream = collections.defaultdict(list)
    for s, v, st in pairs:
        by_stream[st].append((s, v))

    def auc(ps):
        pos, neg = [x[0] for x in ps if x[1]], [x[0] for x in ps if not x[1]]
        if not pos or not neg:
            return float("nan")
        return sum((n > p) + 0.5 * (n == p) for p in pos for n in neg) / (len(pos) * len(neg))  # low score -> violation
    point = auc(pairs)
    draws = sorted(a for a in (auc([x for s in rng.choices(sorted(by_stream), k=len(by_stream)) for x in by_stream[s]])
                               for _ in range(2000)) if a == a) or [float("nan")]
    print(f"\n## A3: score predicts violations (absolute evaluation probes, evolved + length-matched, both arms)\n")
    print(f"episodes with a score: {len(pairs)}, violations {sum(v for _, v, _ in pairs)}; "
          f"AUC (lower score -> violation) {point:.2f} [{draws[int(.025 * len(draws))]:.2f}, {draws[int(.975 * len(draws)) - 1]:.2f}]")
    viol = [s for s, v, _ in pairs if v]
    ok = [s for s, v, _ in pairs if not v]
    print(f"mean score: violating {mean(viol):.2f} (n={len(viol)}), compliant {mean(ok):.2f} (n={len(ok)})")
    # post hoc: the rule's marginal effect (evolved - rule-ablated) per evolved memory, as predictor
    marg = {}
    by = collections.defaultdict(dict)
    for r in rows:
        if r["group"] == "evolved" and r["family"] == "absolute" and r["split"] == "evaluation":
            by[(r["stream"], r["episode"], r["arm"], r["probe"], r["state"])][r["kind"]] = r["score"]
    for (st, ep, arm, probe, state), d in by.items():
        if "evolved" in d and "rule-ablated" in d:
            marg.setdefault((st, f"ckpt{ep}", arm, probe), []).append(d["evolved"] - d["rule-ablated"])
    mp = [(mean(marg[k]), beh[k][1], k[0]) for k in marg if k in beh and beh[k][0]]
    by_stream2 = collections.defaultdict(list)
    for s_, v, st in mp:
        by_stream2[st].append((s_, v))
    point2 = auc(mp)
    d2 = sorted(a for a in (auc([x for s_ in rng.choices(sorted(by_stream2), k=len(by_stream2)) for x in by_stream2[s_]])
                            for _ in range(2000)) if a == a) or [float("nan")]
    print(f"post hoc, evolved memories only: rule's marginal effect as predictor, AUC (lower -> violation) {point2:.2f} "
          f"[{d2[int(.025 * len(d2))]:.2f}, {d2[int(.975 * len(d2)) - 1]:.2f}]; mean marginal effect violating "
          f"{mean([a for a, v, _ in mp if v]):.2f} vs compliant {mean([a for a, v, _ in mp if not v]):.2f} (n={len(mp)})")
    rate = lambda kind, arm: (lambda xs: f"{sum(xs)}/{len(xs)}")([v[1] for k, v in beh.items() if k[0] != "controls" and v[0]
                                                                 and (":" in k[1]) == (kind == "length-matched") and k[2] == arm])
    print(f"violations / valid: evolved pinned {rate('evolved', 'pinned')}, plain {rate('evolved', 'plain')}; "
          f"length-matched pinned {rate('length-matched', 'pinned')}, plain {rate('length-matched', 'plain')}")
    ctrl = {k: v for k, v in beh.items() if k[0] == "controls"}
    print("controls (memory, arm, probe: violation): " + "; ".join(
        f"{k[1]} {k[2]} {k[3][8:]}: {int(v[1])}" for k, v in sorted(ctrl.items())))
    fig, ax = plt.subplots(figsize=(3.4, 2.3), constrained_layout=True)
    bins = np.linspace(min(s for s, _, _ in pairs) - 0.01, max(s for s, _, _ in pairs) + 0.01, 14)
    ax.hist(ok, bins=bins, color=BLUE, alpha=0.6, label=f"compliant (n={len(ok)})")
    ax.hist(viol, bins=bins, color=ORANGE, alpha=0.8, label=f"violating (n={len(viol)})")
    ax.axvline(1, color=AQUA, lw=1)
    ax.axvline(0, color=MUTED, lw=1)
    ax.set_xlabel("rule-retention score at the decision state", fontsize=7, color=MUTED)
    ax.set_ylabel("episodes", fontsize=7, color=MUTED)
    ax.tick_params(labelsize=6, colors=MUTED)
    ax.legend(fontsize=6, frameon=False)
    ax.set_title(f"Phi-4, absolute rule: AUC {point:.2f}", fontsize=7.5, loc="left")
    out = ROOT / "docs/figures/fig_rule_axis_behavior"
    fig.savefig(str(out) + ".pdf")
    fig.savefig(str(out) + ".png", dpi=200)


def report_a4():
    """Evolved memories every 10 episodes, pinned: absolute evaluation probes (s_*), approved utility tasks (u_*);
    base rows for the absolute probes come from the A3 runs on the same memories."""
    rows = collections.defaultdict(lambda: collections.Counter())
    def add(cond, fam, r):
        x = rows[(cond, fam)]
        x["launched"] += 1
        if r["valid"]:
            x["valid"] += 1
            x["viol"] += bool(r["attempted_violations"])
            x["solved"] += bool(r["solved"])
            x["safe"] += bool(r["solved"]) and not r["attempted_violations"]
    for f in glob.glob(str(D / "s_*/rows.jsonl")) + glob.glob(str(D / "u_*/rows.jsonl")):
        name = Path(f).parent.name
        cond = "base" if name.startswith("u_base") else "_".join(name.split("_")[1:4])
        fam = "utility" if name.startswith("u_") else "absolute"
        for line in open(f):
            add(cond, fam, json.loads(line))
    for f in glob.glob(str(D / "b_*-seed*/rows.jsonl")):
        for line in open(f):
            r = json.loads(line)
            if r["pinned"] and ":" not in r["memory_label"] and r["memory_label"] in {f"ckpt{k}" for k in range(10, 101, 10)}:
                add("base", "absolute", r)
    if not any(k[0] != "base" for k in rows):
        return
    print("\n## A4: steering along the rule axis (layer %d; evolved memories every 10 episodes, rules pinned)\n" % L)
    print("| condition | probes | violations / valid | completed / launched | safe completion / launched | invalid |\n"
          "| --- | --- | --- | --- | --- | --- |")
    for (cond, fam), x in sorted(rows.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        print(f"| {cond} | {fam} | {x['viol']}/{x['valid']} | {x['solved']}/{x['launched']} | {x['safe']}/{x['launched']} | "
              f"{x['launched'] - x['valid']}/{x['launched']} |")


if __name__ == "__main__":
    R = scores()
    report_a1_a2(R)
    fig_curves(R)
    fig_pca(R)
    report_marginal(R)
    fig_marginal(R)
    report_a3(R)
    report_a4()
