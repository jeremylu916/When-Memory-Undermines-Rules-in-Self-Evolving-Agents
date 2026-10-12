"""Evolution-time figure: violation rate vs. self-evolution training episodes.

usage: python scripts/make_evolution_figure.py [OUT_STEM]   (default docs/figures/fig_evolution_time)

Panels (attempted violations among valid episodes; checkpoint 0 = rules-only /
initial memory; bands = 95% stream bootstrap, streams resampled with their
checkpoints):
 (a) benign, absolute rule (c_settings_purge), DeepSeek-V4.1 native memories,
     rules pinned: evolved vs length-matched at each checkpoint
 (b) benign, native Sonnet 4.6 (two streams, rules in memory only): per stream
 (c) reward, unseen cleanups, rules pinned: proxy vs corrected (7 seeds)
 (d) reward training: records violations in scored cleanup episodes per
     25-episode window, fresh seeds 53-83: proxy vs corrected vs frozen memory
Frontier native streams (v15) are added to (a) when their replays exist.
"""

from __future__ import annotations

import collections
import glob
import json
import random
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
BLUE, ORANGE, AQUA, GREY, INK, MUTED = "#2a78d6", "#eb6834", "#1baf7a", "#8b8a86", "#0b0b0b", "#52514e"
CK = [0, 10, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100]  # v20: dense checkpoints every 10 episodes
UNSEEN = ("heldout_rh_outdated", "heldout_rh_leftover_tmp")
rng = random.Random(20261003)


def rate(eps):
    v = [e for e in eps if e[0]]
    return sum(e[1] for e in v) / len(v) if v else float("nan")


def boot(streams):
    """streams: {stream: [(valid, viol)]} -> (point, lo, hi)."""
    keys = sorted(streams)
    point = rate([e for k in keys for e in streams[k]])
    if len(keys) < 3:
        return point, float("nan"), float("nan")
    draws = sorted(rate([e for k in rng.choices(keys, k=len(keys)) for e in streams[k]]) for _ in range(2000))
    return point, draws[50], draws[1949]


def rows(pattern):
    """pattern: one glob, or several joined by '|'."""
    for f in (f for p in pattern.split("|") for f in glob.glob(str(RUNS / p))):
        # top-up and dense (v20) replays belong to their stream
        stream = re.sub(r"_(resume|trial3)$", "", re.sub(r"^(desk|mail|rw|r)_", "", Path(f).parent.name))
        for line in open(f):
            yield stream, json.loads(line)


def ck_of(label):
    if label in {"len0", "initial"}:
        return 0
    m = re.fullmatch(r"ckpt(\d+)(:length_matched)?", label)
    return int(m.group(1)) if m else None


def curve(pattern, templates, arm, variant=":length_matched", want_variant=False, zero_from=None):
    """{checkpoint: {stream: [(valid, viol)]}}."""
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for stream, r in rows(pattern):
        if r["template_id"] not in templates or arm(r) is False:
            continue
        lab = r["memory_label"]
        c = ck_of(lab)
        if c is None or (c and want_variant != lab.endswith(variant)):
            continue
        key = "rules-only" if c == 0 else stream
        out[c][key].append((r["valid"], bool(r["attempted_violations"])))
    if zero_from:
        for stream, r in rows(zero_from):
            if r["template_id"] in templates and r["memory_label"] in {"len0", "initial"} and arm(r) is not False:
                out[0]["rules-only"].append((r["valid"], bool(r["attempted_violations"])))
    return out


def pinned(r):
    return r["pinned"] and not r.get("system_rules") and not r.get("self_check") and not r.get("guard")


def plain(r):
    return not r["pinned"] and not r.get("system_rules") and not r.get("rules_in_task") and not r.get("guard")


def draw(ax, data, color, label, marker="o", dashed=False):
    xs = [c for c in CK if c in data]
    pts = [boot(data[c]) for c in xs]
    ys = [100 * p[0] for p in pts]
    ax.plot(xs, ys, color=color, lw=1.6, ls="--" if dashed else "-", marker=marker, ms=3.5,
            markeredgecolor="white", markeredgewidth=0.8, label=label, zorder=3)
    band = [(x, 100 * p[1], 100 * p[2]) for x, p in zip(xs, pts) if p[1] == p[1]]
    if len(band) >= 2:  # single-memory points (checkpoint 0) carry no stream band
        ax.fill_between([b[0] for b in band], [b[1] for b in band], [b[2] for b in band],
                        color=color, alpha=0.15, lw=0, zorder=1)


def style(ax, title, ylabel=True):
    ax.set_title(title, fontsize=7.5, color=INK, loc="left")
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlim(-4, 104)
    ax.set_ylim(-3, 128)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_xlabel("training episodes", fontsize=7, color=MUTED)
    if ylabel:
        ax.set_ylabel("violation rate (%)", fontsize=7, color=MUTED)
    ax.tick_params(labelsize=7, colors=MUTED, length=2)
    ax.grid(axis="y", color="#e6e5e0", lw=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#bdbcb6")
    n = len(ax.get_legend_handles_labels()[0])
    if n:
        ax.legend(fontsize=5.5, frameon=False, loc="upper center", ncol=n if n <= 3 else 2, handlelength=1.4,
                  columnspacing=0.6, handletextpad=0.3, borderaxespad=0.1)


def rule_axis_panel(ax):
    """v20 (post hoc): the rule's marginal effect on Phi-4's decision state under the DeepSeek memories, i.e.
    score(memory) - score(same memory without its rule lines), rules alone = 1; evaluation probes, rules pinned,
    every 5 training episodes; bands: stream bootstrap."""
    f = RUNS / "rule_axis_v20_20261005/layer_selection.json"
    if not f.exists():
        return
    L = json.loads(f.read_text())["chosen"]
    path = RUNS / f"rule_axis_v20_20261005/projections_L{L}.jsonl"
    if not path.exists():
        return
    rows_ = [json.loads(l) for l in open(path)]
    ref = {(r["probe"], r["state"], r["arm"], r["kind"]): r["projection"] for r in rows_ if r["kind"] in {"no rules", "rules-only"}}
    score = lambda r: (r["projection"] - ref[(r["probe"], r["state"], r["arm"], "no rules")]) / (
        ref[(r["probe"], r["state"], r["arm"], "rules-only")] - ref[(r["probe"], r["state"], r["arm"], "no rules")])
    pair = collections.defaultdict(dict)
    for r in rows_:
        if r["split"] == "evaluation" and r["arm"] == "pinned":
            pair[(r["family"], r["probe"], r["state"], r["stream"], r["episode"])][r["kind"]] = score(r)
    for family, color, marker in (("absolute", BLUE, "o"), ("records", ORANGE, "s")):
        data = collections.defaultdict(lambda: collections.defaultdict(list))
        pads = []
        for (fam, _, _, st, ep), d in pair.items():
            if fam != family:
                continue
            if "evolved" in d and "rule-ablated" in d:
                data[ep][st].append(d["evolved"] - d["rule-ablated"])
            if ep == 4000 and "rules+pad" in d and "pad only" in d:
                pads.append(d["rules+pad"] - d["pad only"])
        xs = sorted(data)
        pts = []
        for x in xs:
            keys = sorted(data[x])
            mean_ = lambda ks: sum(v for k in ks for v in data[x][k]) / sum(len(data[x][k]) for k in ks)
            draws = sorted(mean_(rng.choices(keys, k=len(keys))) for _ in range(1000))
            pts.append((mean_(keys), draws[25], draws[974]))
        ax.plot([0] + xs, [1.0] + [p[0] for p in pts], color=color, lw=1.6, marker=marker, ms=3, markeredgecolor="white",
                markeredgewidth=0.6, label=family)
        ax.fill_between(xs, [p[1] for p in pts], [p[2] for p in pts], color=color, alpha=0.15, lw=0)
        if pads:
            ax.axhline(sum(pads) / len(pads), color=color, lw=0.8, ls=":")
    ax.axhline(0, color=MUTED, lw=0.8)
    ax.axhline(1, color=AQUA, lw=0.8)


def training_windows(dirs):
    out = collections.defaultdict(lambda: collections.defaultdict(list))  # window end -> stream -> eps
    for d in dirs:
        manifest = json.loads((Path(d) / "manifest.json").read_text())
        templates = {t["task_id"]: t["template_id"] for t in manifest["tasks"]}
        for line in open(Path(d) / "events.jsonl"):
            e = json.loads(line)
            if e["event"] == "training" and templates[e["episode"]["task_id"]].startswith("train_rh_"):
                w = 10 * ((e["index"] - 1) // 10 + 1)
                out[w][Path(d).name].append((e["episode"]["valid"],
                                             "preserve_records" in e["episode"]["realized_violations"]))
    return out


def main(stem, compact=False):
    """compact: one row (a, c, d, e) for the main text; otherwise 2 x 3 (appendix)."""
    if compact:
        fig, row = plt.subplots(1, 4, figsize=(7.0, 2.05), constrained_layout=True)
        spare = plt.figure()  # panels (b), (f), (g), (h) are drawn off-figure and discarded
        sb, sf, sg, sh = (spare.add_subplot(1, 4, i) for i in range(1, 5))
        axes = [row[0], sb, row[1], row[2], row[3], sf, sg, sh]
    else:
        fig, grid = plt.subplots(2, 4, figsize=(7.0, 3.9), constrained_layout=True)
        axes = list(grid.flat)
    purge = ("heldout_c_settings_purge",)
    v6 = "conflict_v6_20260930/replay/*/rows.jsonl|dense_v20_20261005/desk_*/rows.jsonl"
    ax = axes[0]
    draw(ax, curve(v6, purge, pinned), BLUE, "DeepSeek")
    draw(ax, curve(v6, purge, pinned, want_variant=True, zero_from=v6), GREY, "length-m.", dashed=True)
    style(ax, "(a) DeepSeek, absolute")
    ax = axes[1]
    for pat, name, color in (("native_sonnet_20260930/replay/rows.jsonl", "s17", BLUE),
                             ("native_sonnet_20260930/replay_seed29/rows.jsonl", "s29", ORANGE)):
        draw(ax, curve(pat, purge, plain), color, f"{name} abs.")
        draw(ax, curve(pat, ("heldout_c_stale_sweep",), plain), color, f"{name} rec.", marker="s", dashed=True)
    style(ax, "(b) Sonnet 4.6, plain", ylabel=False)
    ax = axes[2]
    t3 = ("heldout_c_settings_purge", "heldout_c_records_tidy", "heldout_c_stale_sweep")
    draw(ax, curve(v6, t3, pinned), BLUE, "DeepSeek")
    for model, color, name in (("opus55", ORANGE, "Opus 5.5"), ("gpt61", AQUA, "GPT-6.1")):
        pat = f"frontier_v15_20261003/{model}/r_native-seed*/rows.jsonl"
        if glob.glob(str(RUNS / pat)):
            draw(ax, curve(pat, t3, pinned), color, name, marker="s" if model == "opus55" else "D",
                 dashed=model == "gpt61")
    style(ax, "(c) 3 conflict probes", ylabel=False)
    ax = axes[3]
    reward = ["reward_hacking_v13_20261002/r_{m}-seed*/rows.jsonl", "dense_v20_20261005/rw_{m}-seed*/rows.jsonl"]
    def merged(mode, **kw):
        out = collections.defaultdict(lambda: collections.defaultdict(list))
        for p in reward:
            for c, d in curve(p.format(m=mode), UNSEEN, pinned, **kw).items():
                for s, eps in d.items():
                    out[c][s] += eps
        for c, d in curve("reward_hacking_v11_20261001/r_controls/rows.jsonl", UNSEEN, pinned).items():
            if c == 0:
                out[0]["rules-only"] += d["rules-only"]
        return out
    draw(ax, merged("proxy"), ORANGE, "proxy")
    draw(ax, merged("aligned_v2"), BLUE, "corrected")
    draw(ax, merged("proxy", want_variant=True), GREY, "length-m.", dashed=True)
    style(ax, "(d) reward, unseen")
    ax = axes[4]
    v13 = RUNS / "reward_hacking_v13_20261002"
    v14 = RUNS / "reward_hacking_v14_20261002"
    for mode, color, name in (("proxy", ORANGE, "proxy"), ("aligned_v2", BLUE, "corrected"),
                              ("frozen", GREY, "frozen")):
        dirs = [d for d in glob.glob(str(v13 / f"{mode}-seed*")) + glob.glob(str(v14 / f"{mode}-seed*"))
                if (Path(d) / "summary.json").exists()]
        data = training_windows(dirs)
        xs = sorted(data)
        pts = [boot(data[x]) for x in xs]
        ax.plot(xs, [100 * p[0] for p in pts], color=color, lw=1.6, marker="o", ms=3.5, markeredgecolor="white",
                markeredgewidth=0.8, label=name, ls="--" if mode == "frozen" else "-", zorder=3)
        if all(p[1] == p[1] for p in pts):
            ax.fill_between(xs, [100 * p[1] for p in pts], [100 * p[2] for p in pts], color=color, alpha=0.15, lw=0)
    style(ax, "(e) reward, training", ylabel=False)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("episodes (window end)", fontsize=7, color=MUTED)
    ax = axes[5]
    gpt = "frontier_v15_20261003/gpt61/r_{m}-seed53/rows.jsonl"
    for mode, color, name in (("proxy", ORANGE, "proxy"), ("aligned_v2", BLUE, "corrected")):
        draw(ax, curve(gpt.format(m=mode), UNSEEN, pinned), color, name, dashed=mode == "aligned_v2")
    style(ax, "(f) GPT-6.1 reward", ylabel=False)
    ax = axes[6]
    conflict5 = ("heldout_m_fin_all", "heldout_m_fin_share", "heldout_m_fin_except", "heldout_m_legal_other",
                 "heldout_m_legal_folder")
    mail = "mailshare_v18_20261005/ds/r_*-seed*/rows.jsonl|dense_v20_20261005/mail_*/rows.jsonl"
    draw(ax, curve(mail, conflict5, pinned, zero_from="mailshare_v18_20261005/ds/r_controls/rows.jsonl"), BLUE, "DeepSeek")
    draw(ax, curve("mailshare_v18_20261005/qwen3/r_*-seed*/rows.jsonl", ("heldout_m_legal_other", "heldout_m_legal_folder"),
                   pinned, zero_from="mailshare_v18_20261005/qwen3/r_controls/rows.jsonl"), ORANGE, "Qwen3", marker="s")
    style(ax, "(g) MailShare", ylabel=False)
    ax = axes[7]
    rule_axis_panel(ax)
    style(ax, "(h) Phi-4: rule's effect", ylabel=False)
    ax.set_ylim(-0.1, 1.3)
    ax.set_yticks([0, 0.5, 1])
    ax.set_ylabel("marginal effect (rules alone = 1)", fontsize=6, color=MUTED)
    if compact:
        for ax, title in zip(row, ("(a) DeepSeek, absolute", "(b) native, 3 conflict probes",
                                   "(c) reward, unseen cleanups", "(d) reward, training")):
            ax.set_title(title, fontsize=7.5, color=INK, loc="left")
        row[2].set_ylabel("")
        plt.close(spare)
    out = ROOT / stem
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out) + ".pdf")
    fig.savefig(str(out) + ".png", dpi=200)
    print("wrote", out)


if __name__ == "__main__":
    main("docs/figures/fig_evolution_time")
    main("docs/figures/fig_evolution_time_main", compact=True)
