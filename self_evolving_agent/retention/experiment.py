"""Fixed-candidate experiments, with held-out outcomes isolated from decisions.

Each case is an intervention on its supplied old library. This permits fair
selector/gate comparisons after different methods reject different updates;
it is not a claim that their long-term library trajectories stay identical.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Callable

from .contracts import FrozenAgent, SkillEdit, SkillLibrary, TaskRecord, digest
from .evaluation import Evaluator, accounting, evaluate_pairs, panel_metrics
from .selectors import select_tasks


SELECTORS = ("random", "recent", "recent_diverse", "change_aware")
Reviser = Callable[[SkillEdit, list[dict]], tuple[SkillEdit, dict]]


def gate_decision(new_rows: list[dict], known_rows: list[dict], min_gain: float = 0.0) -> dict:
    if not new_rows or not known_rows or any(not r["valid"] for r in new_rows + known_rows):
        return {"accepted": False, "reason": "incomplete_evidence", "new_task_gain": None}
    gain = sum(r["new_solve_rate"] - r["old_solve_rate"] for r in new_rows) / len(new_rows)
    # A single suspected loss is enough to withhold promotion. Discovery's
    # stricter repeat-confirmed label must not hide uncertainty from a gate.
    regressed = [r["task_id"] for r in known_rows if r["suspected_regression"]]
    return {"accepted": gain > min_gain and not regressed,
            "reason": "regression" if regressed else ("accepted" if gain > min_gain else "no_new_task_gain"),
            "new_task_gain": gain, "counterexample_task_ids": regressed}


def run_case(*, library: SkillLibrary, edit: SkillEdit, tasks: list[TaskRecord],
             agent: FrozenAgent, evaluator: Evaluator, budget: int = 10,
             seeds: tuple[int, ...] = (17, 29, 43), selection_seed: int = 0,
             stage: str = "discover", reviser: Reviser | None = None,
             min_regression_trials: int = 2, min_gain: float = 0.0,
             save_plan: Callable[[dict], None] | None = None) -> dict:
    if stage not in {"observe", "discover", "govern"}:
        raise ValueError("stage must be observe, discover, or govern")
    if not 0 <= min_gain <= 1:
        raise ValueError("min_gain must be in [0, 1]")
    by_id = {t.task_id: t for t in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("duplicate task IDs across panels")
    # Also prevent aliases of the same issue/snapshot crossing panels.
    identities = [(t.repository, t.snapshot, t.issue) for t in tasks]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate task content across panels")
    known = [t for t in tasks if t.panel == "known"]
    fresh = [t for t in tasks if t.panel == "new"]
    heldout = [t for t in tasks if t.panel == "heldout"]
    if not known or not fresh:
        raise ValueError("known and new task panels must be nonempty")
    if max(t.sequence for t in known) >= min(t.sequence for t in fresh):
        raise ValueError("known tasks must precede the new-task batch")
    if not set(edit.source_task_ids) <= {t.task_id for t in fresh}:
        raise ValueError("skill update sources must belong to the new panel")
    candidate = edit.apply(library)
    source_repositories = {by_id[tid].repository for tid in edit.source_task_ids}
    selections = {method: select_tasks(known, edit, method=method, budget=budget,
                    seed=selection_seed, source_repositories=source_repositories)
                  for method in SELECTORS}
    common = dict(agent=agent, evaluator=evaluator, seeds=seeds,
                  min_regression_trials=min_regression_trials)
    report = {"schema_version": "retention.case.v1", "stage": stage,
              "candidate": asdict(edit), "old_library": library.to_dict(),
              "candidate_library": candidate.to_dict(), "agent": asdict(agent),
              "agent_hash": agent.fingerprint, "task_records": [asdict(t) for t in tasks],
              "budget_requested": budget, "budget_effective": min(budget, len(known)),
              "seeds": list(seeds), "selection_seed": selection_seed,
              "min_regression_trials": min_regression_trials,
              "selections": {name: [r.to_dict() for r in rows] for name, rows in selections.items()},
              "gates": {}, "scope": "fixed_candidate_intervention"}
    if save_plan is not None:
        save_plan(report)
    executed_rows: list[dict] = []
    if stage == "govern":
        # Run each gate's evidence prospectively, before the larger offline audit.
        # Equal first-round task budgets, independent executions, same candidate.
        report["gates"]["unrestricted"] = {"accepted": True, "reason": "unrestricted",
                                                   "promoted_library_hash": candidate.fingerprint,
                                                   "evaluation_cost": accounting([])}
        for method in ("recent", "change_aware"):
            selected_tasks = [by_id[r.task_id] for r in selections[method]]
            new_rows = evaluate_pairs(fresh, library, candidate, **common)
            selected_rows = evaluate_pairs(selected_tasks, library, candidate, **common)
            method_rows = new_rows + selected_rows
            decision = gate_decision(new_rows, selected_rows, min_gain)
            rounds = [{"edit": asdict(edit), "decision": decision,
                       "new_pairs": new_rows, "known_pairs": selected_rows}]
            revision_cost = {"model_calls": 0, "tokens": 0, "cost_usd": 0.0}
            promoted = candidate if decision["accepted"] else library
            if not decision["accepted"] and reviser is not None:
                # No heldout or offline-audit evidence reaches the learner.
                counterexamples = [r for r in selected_rows if r["suspected_regression"] or not r["valid"]]
                if not counterexamples:
                    counterexamples = [r for r in new_rows if not r["valid"] or
                                       r["new_solve_rate"] <= r["old_solve_rate"]]
                counterexamples = [{**r, "task": asdict(by_id[r["task_id"]])} for r in counterexamples]
                revised, revision_cost = reviser(edit, counterexamples)
                if (revised.skill_id, revised.old_version, revised.old_text,
                        revised.old_applicability, revised.source_task_ids) != (
                        edit.skill_id, edit.old_version, edit.old_text,
                        edit.old_applicability, edit.source_task_ids):
                    raise ValueError("revision must edit the same base skill and source tasks")
                from .evaluation import Outcome
                Outcome(None, executions=0, **revision_cost)  # Strict cost validation.
                revised_library = revised.apply(library)
                # Reselect for the actual revised edit, retaining discovered
                # counterexamples within the same k-task budget.
                ranked = select_tasks(known, revised, method=method, budget=budget,
                                      seed=selection_seed, source_repositories=source_repositories)
                ids = list(dict.fromkeys([r["task_id"] for r in counterexamples if r["panel"] == "known"]
                                         + [r.task_id for r in ranked]))[:budget]
                revised_new = evaluate_pairs(fresh, library, revised_library, **common)
                revised_known = evaluate_pairs([by_id[tid] for tid in ids], library, revised_library, **common)
                decision = gate_decision(revised_new, revised_known, min_gain)
                method_rows.extend(revised_new + revised_known)
                rounds.append({"edit": asdict(revised), "decision": decision,
                               "new_pairs": revised_new, "known_pairs": revised_known})
                promoted = revised_library if decision["accepted"] else library
            cost = accounting(method_rows)
            for key, value in revision_cost.items():
                cost[key] += value
            report["gates"][method] = {**decision, "rounds": rounds, "evaluation_cost": cost,
                                      "revision_cost": revision_cost,
                                      "promoted_library_hash": promoted.fingerprint,
                                      "promoted_library": promoted.to_dict()}
            executed_rows.extend(method_rows)
    # Offline audit is deliberately larger than selector budget and never fed
    # back into selection or gate revision. Its cost is reported separately.
    audit = evaluate_pairs(known, library, candidate, **common)
    fresh_audit = evaluate_pairs(fresh, library, candidate, **common)
    heldout_audit = evaluate_pairs(heldout, library, candidate, **common)
    audit_rows = audit + fresh_audit + heldout_audit
    truth = {r["task_id"] for r in audit if r["verified_regression"]}
    audit_complete = all(r["valid"] for r in audit)
    report["discovery"] = {}
    for method, selection in selections.items():
        selected_ids = {r.task_id for r in selection}
        found = sorted(truth & selected_ids)
        report["discovery"][method] = {
            "selected_task_ids": sorted(selected_ids), "detected_regressions": found,
            "audit_regressions": len(truth), "audit_complete": audit_complete,
            "recall_at_budget": len(found) / len(truth) if truth and audit_complete else None,
            "precision_at_budget": len(found) / len(selected_ids) if audit_complete else None,
            "paired_executions_at_budget": 2 * len(seeds) * len(selected_ids),
            "interpretation": "offline_selection_replay"}
    report["panels"] = {"known": panel_metrics(audit), "new": panel_metrics(fresh_audit),
                        "heldout": panel_metrics(heldout_audit)}
    report["audit_pairs"] = audit_rows
    report["audit_cost"] = accounting(audit_rows)
    report["go_no_go"] = ("inconclusive_infrastructure" if not audit_complete else
                           "measurable_forgetting" if truth else "no_verified_forgetting")
    report["verified_regression_prevalence"] = len(truth) / len(known) if audit_complete else None
    # Assess post-decision libraries on both panels. Outcomes cannot change gates.
    post_rows = []
    if stage == "govern":
        for method, gate in report["gates"].items():
            if method == "unrestricted":
                rows = audit + heldout_audit
            else:
                promoted = SkillLibrary.from_dict(gate["promoted_library"])
                if promoted.fingerprint == candidate.fingerprint:
                    rows = audit + heldout_audit
                else:
                    rows = evaluate_pairs(known + heldout, library, promoted, **common)
                    post_rows.extend(rows)
            gate["post_decision_retention"] = {
                panel: panel_metrics([r for r in rows if r["panel"] == panel])
                for panel in ("known", "heldout")}
    report["post_decision_cost"] = accounting(post_rows)
    report["post_decision_pairs"] = post_rows
    report["total_cost"] = accounting(executed_rows + audit_rows + post_rows)
    for gate in report["gates"].values():
        for key, value in gate.get("revision_cost", {}).items():
            report["total_cost"][key] += value
    report["report_hash"] = digest(report)
    return report


def summarize_cases(reports: list[dict]) -> dict:
    complete = [r for r in reports if r["go_no_go"] != "inconclusive_infrastructure"]
    return {"cases": len(reports), "complete_cases": len(complete),
            "updates_with_verified_forgetting": sum(r["go_no_go"] == "measurable_forgetting" for r in complete),
            "selectors": {method: {
                "detected_regressions": sum(len(r["discovery"][method]["detected_regressions"]) for r in complete),
                "audited_regressions": sum(r["discovery"][method]["audit_regressions"] for r in complete),
                "micro_recall": (sum(len(r["discovery"][method]["detected_regressions"]) for r in complete)
                                 / sum(r["discovery"][method]["audit_regressions"] for r in complete)
                                 if any(r["discovery"][method]["audit_regressions"] for r in complete) else None)
            } for method in SELECTORS}}
