"""Summarize safety-retention stream directories: rates, violations, mechanisms.

Usage: python scripts/summarize_safety_retention.py RUN_DIR [RUN_DIR ...]
Each RUN_DIR holds events.jsonl (and summary.json once complete). Incomplete
streams are summarized from their events and marked incomplete.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(run: Path):
    events = [json.loads(line) for line in (run / "events.jsonl").open()]
    manifest = json.loads((run / "manifest.json").read_text())
    templates = {t["task_id"]: t["template_id"] for t in manifest["tasks"]}
    return events, manifest, templates


def fmt_rate(rows):
    valid = [r for r in rows if r["valid"]]
    attempts = sum(bool(r["attempted_violations"]) for r in valid)
    solved = sum(r["solved"] and not r["attempted_violations"] for r in valid)
    return f"{attempts}/{len(valid)} att, {solved} safe-solved, {len(rows) - len(valid)} inv"


def summarize(run: Path) -> dict:
    events, manifest, templates = load(run)
    rules = {r["rule_id"]: r["text"] for r in manifest["rules"]}
    audits = [e for e in events if e["event"] == "audit"]
    training = [e["episode"] for e in events if e["event"] == "training"]
    updates = [e for e in events if e["event"] == "memory_update"]
    status = "complete" if (run / "summary.json").exists() else (
        "interrupted" if (run / "interrupted.json").exists() else "running")
    print(f"\n=== {run.name} [{status}] training {len(training)} episodes, "
          f"{sum(u['accepted'] for u in updates)}/{len(updates)} updates accepted")
    for audit in audits:
        arms = "; ".join(f"{name}: {fmt_rate(rows)}" for name, rows in sorted(audit["arms"].items()))
        print(f"  ckpt {audit['checkpoint']:>3}: {arms}")
    train_viol = [(i + 1, templates[r["task_id"]], r["attempted_violations"])
                  for i, r in enumerate(training) if r["attempted_violations"]]
    print(f"  training: {fmt_rate(training)}, gui_errors {sum(r.get('gui_errors', 0) for r in training)}; "
          f"violations {train_viol}")
    findings = []
    for audit in audits:
        by_key = {name: {(r["task_id"], r["seed"]): r for r in rows} for name, rows in audit["arms"].items()}
        for arm_name, arm_rows in by_key.items():
          for key, row in arm_rows.items():
            if not (row["valid"] and row["attempted_violations"]):
                continue
            paired = {name: ("invalid" if not rows[key]["valid"] else rows[key]["attempted_violations"] or "safe")
                      for name, rows in by_key.items() if name != arm_name}
            unsafe_steps = [e["action"] for e in row["events"] if e["violations"]]
            findings.append({"checkpoint": audit["checkpoint"], "arm": arm_name, "template": templates[row["task_id"]],
                             "violations": row["attempted_violations"],
                             "realized": row["realized_violations"], "memory_version": row["memory_version"],
                             "rule_text_present": row["verbatim_rule_coverage"], "paired_arms": paired,
                             "unsafe_actions": unsafe_steps})
    for f in findings:
        print(f"  VIOLATION ckpt {f['checkpoint']} arm={f['arm']} {f['template']}: {f['violations']} realized={f['realized']} "
              f"mem v{f['memory_version']} rule_text={f['rule_text_present']} paired={f['paired_arms']}")
        print(f"    unsafe actions: {f['unsafe_actions']}")
    accepted = [u for u in updates if u["accepted"]]
    if accepted:
        final = accepted[-1]["candidate"]["text"]
        missing = [rid for rid, text in rules.items() if text not in final]
        print(f"  final memory: {len(final)} chars; rule text missing verbatim: {missing}")
    return {"run": run.name, "status": status, "findings": findings}


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        summarize(Path(arg))
