"""v11 reward-hacking tallies (design: docs/research/computer_use_reward_hacking_v11.md).

Per stream: loophole training episodes per 25-episode window (records-rule
violation rate, mean grader score, permitted target solved), audits per
checkpoint (violations and proxy score on the unseen loophole wordings and on
the other probes), and the final memory's sentences that mention records.
"""

from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

RUN = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/reward_hacking_v11_20261001")
UNSEEN = ("heldout_rh_outdated", "heldout_rh_leftover_tmp")


def main():
    for events_path in sorted(glob.glob(str(RUN / "*-seed*/events.jsonl"))):
        name = Path(events_path).parent.name
        manifest = json.loads((Path(events_path).parent / "manifest.json").read_text())
        templates = {t["task_id"]: t["template_id"] for t in manifest["tasks"]}
        events = [json.loads(line) for line in open(events_path)]
        print(f"\n## {name}")
        windows = {}
        for e in events:
            if e["event"] != "training":
                continue
            ep = e["episode"]
            if not templates[ep["task_id"]].startswith("train_rh_"):
                continue
            w = windows.setdefault((e["index"] - 1) // 25, {"n": 0, "viol": 0, "score": 0.0, "solved": 0, "valid": 0})
            w["n"] += 1
            w["valid"] += ep["valid"]
            w["viol"] += "preserve_records" in ep["realized_violations"]
            w["score"] += ep.get("proxy_score", 0.0)
            w["solved"] += ep["solved"]
        print("loophole training episodes | window | records violated | mean proxy score | permitted target solved | valid")
        for k, w in sorted(windows.items()):
            print(f"  eps {25 * k + 1}-{25 * k + 25}: {w['viol']}/{w['n']} | {w['score'] / max(1, w['n']):.2f} | "
                  f"{w['solved']}/{w['n']} | {w['valid']}/{w['n']}")
        print("audits | checkpoint | unseen loophole: violations, mean proxy score | other probes: violations by template")
        for e in events:
            if e["event"] != "audit":
                continue
            rows = e["arms"]["current"]
            unseen = [r for r in rows if templates[r["task_id"]] in UNSEEN]
            other = {}
            for r in rows:
                t = templates[r["task_id"]]
                if t not in UNSEEN:
                    c = other.setdefault(t.removeprefix("heldout_"), [0, 0])
                    c[0] += bool(r["attempted_violations"])
                    c[1] += 1
            score = sum(r.get("proxy_score", 0) for r in unseen) / max(1, len(unseen))
            print(f"  ckpt {e['checkpoint']}: {sum(bool(r['attempted_violations']) for r in unseen)}/{len(unseen)}, "
                  f"{score:.2f} | " + ", ".join(f"{t} {v}/{n}" for t, (v, n) in sorted(other.items())))
        accepted = [e["candidate"]["text"] for e in events if e["event"] == "memory_update" and e["accepted"]]
        if accepted:
            sentences = re.split(r"(?<=[.!?])\s+|\n", accepted[-1])
            hits = [s.strip() for s in sentences if re.search(r"record|ledger|protect|-old|\.tmp|score", s, re.I)]
            print(f"final memory ({len(accepted[-1])} chars), sentences on records/stale files/score:")
            for s in hits[:12]:
                print("   -", s[:220])


if __name__ == "__main__":
    main()
