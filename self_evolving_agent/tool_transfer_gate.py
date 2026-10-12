"""Transfer-supervised promotion gate for synthesized tools (Trace2Tool, piece 3b).

A tool earns global scope only if it demonstrably transfers: adding it to the
library must produce a statistically reliable improvement on a
repository-disjoint, unseen task set. This is the tool-side analogue of
:mod:`memory_transfer_gate`, with two deliberate choices carried over from the
empirical findings:

1. Paired, per-task evidence. Single-sample resolved-rate deltas did not
   replicate in our runs, so the gate consumes the *paired* per-task gain
   ``j(x; L+{t}) - j(x; L)`` and requires a variance-aware lower bound to clear a
   margin, not a one-shot mean.

2. A graded per-task score ``j``. On a 50-task set only a few tasks flip
   resolved/unresolved, so a binary metric is nearly all noise. ``j`` credits the
   *fraction* of FAIL_TO_PASS tests passed (plus a small patch-apply term), which
   moves on many more tasks and makes the paired signal measurable.

As in the memory gate, the harness/benchmark evaluation is external: the caller
supplies the precomputed with/without per-task scores; the gate is a pure,
deterministic policy that returns an auditable decision and can drive
``ToolLibrary.recalibrate``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

# Default promotion margin and one-sided confidence for the lower bound.
DEFAULT_TAU = 0.0
DEFAULT_CONFIDENCE = 0.95

# Weights for the graded per-task score j (fraction of FAIL_TO_PASS passed is
# primary; patch-apply is a small tie-breaker so empty-patch tasks score below
# applied-but-failing ones).
FTP_WEIGHT = 1.0
PATCH_APPLY_WEIGHT = 0.05

REASON_TRANSFERS = "transfers"
REASON_BELOW_MARGIN = "lower_bound_below_margin"
REASON_NO_EVIDENCE = "no_paired_evidence"

# One-sided normal quantiles for common confidence levels.
_Z = {0.90: 1.2815515655, 0.95: 1.6448536270, 0.975: 1.9599639845, 0.99: 2.3263478740}


@dataclass(frozen=True)
class ToolTransferDecision:
    """Auditable outcome of one tool transfer-gate check."""

    allow_global: bool
    target_scope: str
    tool_name: str
    mean_gain: float | None
    lower_bound: float | None
    tau: float
    n: int
    reason: str

    def to_audit(self) -> dict[str, Any]:
        return asdict(self)


def graded_score(
    fail_to_pass_results: Mapping[str, bool] | None,
    patch_applied: bool,
    *,
    ftp_weight: float = FTP_WEIGHT,
    patch_apply_weight: float = PATCH_APPLY_WEIGHT,
) -> float:
    """Per-task graded score j(x) in [0, ftp_weight + patch_apply_weight].

    Fraction of FAIL_TO_PASS tests passing, weighted, plus a small patch-apply
    term. With no test results the score is the patch-apply term only.
    """

    results = dict(fail_to_pass_results or {})
    if results:
        frac = sum(1 for v in results.values() if v) / len(results)
    else:
        frac = 0.0
    return ftp_weight * frac + patch_apply_weight * (1.0 if patch_applied else 0.0)


def paired_gains(
    with_tool: Mapping[str, float],
    without_tool: Mapping[str, float],
) -> list[float]:
    """Per-task j-gains over the tasks scored in BOTH conditions (paired)."""

    common = sorted(set(with_tool) & set(without_tool))
    return [float(with_tool[t] - without_tool[t]) for t in common]


def lower_confidence_bound(gains: Sequence[float], confidence: float = DEFAULT_CONFIDENCE) -> float | None:
    """One-sided lower bound on the mean paired gain (normal approximation).

    Returns None if there is no evidence. With a single sample the bound is the
    point estimate (no variance information). This is a conservative,
    peeking-agnostic stand-in for a full confidence-sequence bound.
    """

    n = len(gains)
    if n == 0:
        return None
    mean = sum(gains) / n
    if n == 1:
        return mean
    var = sum((g - mean) ** 2 for g in gains) / (n - 1)
    sem = math.sqrt(var / n)
    z = _Z.get(round(confidence, 3), 1.6448536270)
    return mean - z * sem


def evaluate_tool_transfer(
    gains: Sequence[float],
    *,
    tool_name: str = "",
    tau: float = DEFAULT_TAU,
    confidence: float = DEFAULT_CONFIDENCE,
) -> ToolTransferDecision:
    """Decide a tool's calibrated scope from its paired transfer gains."""

    n = len(gains)
    if n == 0:
        return ToolTransferDecision(
            allow_global=False, target_scope="repo", tool_name=tool_name,
            mean_gain=None, lower_bound=None, tau=tau, n=0, reason=REASON_NO_EVIDENCE,
        )
    mean = sum(gains) / n
    lcb = lower_confidence_bound(gains, confidence)
    allow = lcb is not None and lcb >= tau
    return ToolTransferDecision(
        allow_global=allow,
        target_scope="global" if allow else "repo",
        tool_name=tool_name,
        mean_gain=mean,
        lower_bound=lcb,
        tau=tau,
        n=n,
        reason=REASON_TRANSFERS if allow else REASON_BELOW_MARGIN,
    )


def apply_transfer_decisions(library, decisions: Mapping[str, ToolTransferDecision]) -> None:
    """Recalibrate a ToolLibrary in place from a name->decision map.

    Global-promoted tools keep global scope; the rest are demoted to repo scope
    (bound to their source repository), never deleted.
    """

    for entry in library.tools():
        decision = decisions.get(entry.tool.name)
        if decision is None:
            continue
        if decision.allow_global:
            library.recalibrate(
                entry.tool.name, calibrated_scope="global", scope_id="",
                transfer_gain=decision.mean_gain,
            )
        else:
            library.recalibrate(
                entry.tool.name, calibrated_scope="repo",
                scope_id=entry.tool.source_repo, transfer_gain=decision.mean_gain,
            )
