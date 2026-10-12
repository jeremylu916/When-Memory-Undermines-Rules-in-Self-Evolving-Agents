"""Paper figures: violation rates with 95% intervals (stream-level bootstrap / Wilson).

Writes PDF and PNG to docs/figures/. Data come from the run directories via
cluster_bootstrap.load; memory clusters of one training stream are merged, so
intervals resample streams with all their checkpoint memories, as in the main
text and reports/naacl_stream_tables.md (Wilson over episodes below three streams).
"""

from __future__ import annotations

import random
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import cluster_bootstrap as cb  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "figures"
# Reference categorical palette, slots 1-3 in fixed order; markers double-encode identity.
SERIES = [("#2a78d6", "o"), ("#eb6834", "s"), ("#1baf7a", "D")]
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
plt.rcParams.update({"font.size": 9, "axes.edgecolor": INK2, "axes.labelcolor": INK, "xtick.color": INK2,
                     "ytick.color": INK2, "axes.spines.top": False, "axes.spines.right": False,
                     "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE})
rng = random.Random(cb.RNG_SEED)

V41 = ["conflict_v6_20260930/replay/*", "conflict_v6_20260930/mitigation/*"]
ABS1 = {"heldout_c_settings_purge"}
REC = {"heldout_c_records_tidy", "heldout_c_stale_sweep"}
SON = ["replication_bedrock_20260930/sonnet46/controls*", "replication_bedrock_20260930/sonnet46/default-seed*",
       "replication_bedrock_20260930/sonnet46/success_only-seed*"]
ABS4 = {"heldout_c_settings_purge", "heldout_c_settings_move", "heldout_c_settings_empty", "heldout_c_settings_overwrite"}
APPROVAL = {"heldout_a_other_copy", "heldout_a_archive_rest", "heldout_a_remove_folder"}


def estimate(dirs, templates, arm, group):
    data = cb.load(dirs, templates, arm)
    if group not in data:
        return None
    streams = {}
    for key, values in data[group].items():  # "stream/checkpoint" -> stream
        streams.setdefault(key.split("/")[0], []).extend(values)
    if len(streams) < 3:  # too few streams to resample: Wilson over the pooled episodes
        merged = {"all": [v for vs in streams.values() for v in vs]}
        p, lo, hi, _, n = cb.rate_ci(merged, rng)
        return p, lo, hi, n
    p, lo, hi, _, n = cb.rate_ci(streams, rng)
    return p, lo, hi, n


def dot_panel(ax, rows, title):
    """rows: [(label, (p, lo, hi, n) or None)] for one series."""
    for i, (label, est) in enumerate(rows):
        if est is None:
            continue
        p, lo, hi, n = est
        ax.plot([lo, hi], [i, i], color=SERIES[0][0], lw=2, solid_capstyle="round", clip_on=False)
        ax.plot(p, i, SERIES[0][1], color=SERIES[0][0], ms=7, mec=SURFACE, mew=1.5, clip_on=False)
        ax.text(1.04, i, f"{p:.0%} (n={n})", va="center", fontsize=7.5, color=INK2,
                transform=ax.get_yaxis_transform())
    ax.set_yticks(range(len(rows)), [r[0] for r in rows])
    ax.set_ylim(len(rows) - 0.5, -0.5)  # first row on top; safe with sharey
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_title(title, loc="left", fontsize=9.5, color=INK)


def figure_growth():
    groups = [("rules only", "rules_only"), ("+250 neutral chars", "neutral_250"), ("+1000 neutral chars", "neutral_1000"),
              ("+4000 neutral chars", "neutral_4000"), ("evolved memory", "evolved:none"),
              ("evolved, length-matched", "evolved:length_matched")]
    fig, axes = plt.subplots(1, 2, figsize=(7.6, 2.7), sharey=True, gridspec_kw={"wspace": 0.55})
    for ax, (templates, title) in zip(axes, [(ABS1, "Absolute rule (security.txt)"),
                                             (REC, "Approval-gated rule (records)")]):
        dot_panel(ax, [(label, estimate(V41, templates, "pinned", g)) for label, g in groups], title)
        ax.set_xlabel("attempted-violation rate, rules pinned")
    fig.suptitle("DeepSeek-V4.1 on conflict probes: violation rate by memory content", x=0.01, y=1.06, ha="left",
                 fontsize=10, color=INK)
    return fig, "fig_memory_growth"


def figure_mitigation():
    arms = [("rules pinned in payload", "pinned"), ("rules in system prompt", "system"),
            ("system prompt + self-check", "system_selfcheck")]
    fig, axes = plt.subplots(1, 2, figsize=(7.6, 2.0), sharey=True, gridspec_kw={"wspace": 0.55})
    for ax, (dirs, templates, title) in zip(axes, [(V41, ABS1, "DeepSeek-V4.1, evolved memory"),
                                                   (SON, ABS4, "Claude Sonnet 4.6, evolved memory")]):
        dot_panel(ax, [(label, estimate(dirs, templates, arm, "evolved:none")) for label, arm in arms], title)
        ax.set_xlabel("attempted-violation rate, absolute rule")
    fig.suptitle("Evolved memory, absolute rule: where the rules are placed", x=0.01, y=1.1, ha="left",
                 fontsize=10, color=INK)
    return fig, "fig_mitigation"


def figure_approval():
    models = [("Claude Sonnet 4.6", "replication_bedrock_20260930/sonnet46/approval_*"),
              ("DeepSeek-V3.2", "replication_bedrock_20260930/deepseek_v32/approval_*"),
              ("Qwen3-235B", "replication_bedrock_20260930/qwen3_235b/approval_*")]
    memories = [("rules only", "rules_only"), ("evolved memory", "evolved:none"),
                ("evolved, length-matched", "evolved:length_matched")]
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    for j, (mlabel, group) in enumerate(memories):
        color, marker = SERIES[j]
        for i, (_, d) in enumerate(models):
            est = estimate([d], APPROVAL, "pinned", group)
            if est is None:
                continue
            p, lo, hi, n = est
            y = i + (j - 1) * 0.22
            ax.plot([lo, hi], [y, y], color=color, lw=2, solid_capstyle="round", clip_on=False)
            ax.plot(p, y, marker, color=color, ms=7, mec=SURFACE, mew=1.5, clip_on=False,
                    label=mlabel if i == 0 else None)
    ax.set_yticks(range(len(models)), [m[0] for m in models])
    ax.set_ylim(len(models) - 0.5, -0.5)
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_xlabel("attempted-violation rate, approval-scope probes, rules pinned")
    ax.legend(frameon=False, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, fontsize=8)
    ax.set_title("Approval-scope probes: violation rate by model and memory", loc="left",
                 fontsize=10, color=INK, pad=22)
    fig.tight_layout()
    return fig, "fig_approval_scope"


def figure_native():
    seeds = [("seed 17", "native_sonnet_20260930/replay"), ("seed 29", "native_sonnet_20260930/replay_seed29")]
    memories = [("initial (rules only)", "rules_only"), ("self-evolved", "evolved:none"),
                ("length-matched", "evolved:length_matched")]
    probes = [({"heldout_c_settings_purge"}, "c_settings_purge (absolute rule)"),
              ({"heldout_c_stale_sweep"}, "c_stale_sweep (records rule)")]
    fig, axes = plt.subplots(1, 2, figsize=(7.6, 2.3), sharey=True, gridspec_kw={"wspace": 0.25})
    for ax, (templates, title) in zip(axes, probes):
        for j, (mlabel, group) in enumerate(memories):
            color, marker = SERIES[j]
            for i, (_, d) in enumerate(seeds):
                if not (cb.ROOT / d / "rows.jsonl").exists():
                    continue
                est = estimate([d], templates, "plain", group)
                if est is None:
                    continue
                p, lo, hi, n = est
                y = i + (j - 1) * 0.22
                ax.plot([lo, hi], [y, y], color=color, lw=2, solid_capstyle="round", clip_on=False)
                ax.plot(p, y, marker, color=color, ms=7, mec=SURFACE, mew=1.5, clip_on=False,
                        label=mlabel if (i == 0 and ax is axes[0]) else None)
        ax.set_yticks(range(len(seeds)), [s[0] for s in seeds])
        ax.set_ylim(len(seeds) - 0.5, -0.5)
        ax.set_xlim(0, 1)
        ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        ax.grid(axis="x", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
        ax.set_title(title, loc="left", fontsize=9.5, color=INK)
        ax.set_xlabel("attempted-violation rate, rules in memory only")
    fig.legend(frameon=False, loc="upper center", ncol=3, fontsize=8, bbox_to_anchor=(0.5, -0.08))
    fig.suptitle("Native Sonnet 4.6 self-evolution (rules in memory only)", x=0.01, y=1.08,
                 ha="left", fontsize=10, color=INK)
    return fig, "fig_native_sonnet"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for build in (figure_growth, figure_mitigation, figure_approval, figure_native):
        fig, name = build()
        for ext in ("pdf", "png"):
            fig.savefig(OUT / f"{name}.{ext}", dpi=200, bbox_inches="tight")
        plt.close(fig)
        print("wrote", OUT / f"{name}.pdf")


if __name__ == "__main__":
    main()
