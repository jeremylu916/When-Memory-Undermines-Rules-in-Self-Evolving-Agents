"""Does self-evolution help? Training success, GUI steps and held-out audit
success per 25-episode window / checkpoint, for every finished stream."""

import collections
import glob
import json

STREAMS = sorted(glob.glob("runs/drift_v4_20260930/*-seed*/events.jsonl")) + \
    sorted(glob.glob("runs/native_sonnet_20260930/sonnet46-seed*/events.jsonl"))

print("| stream | train solved (ep 1-25 / 76-100) | mean GUI steps (1-25 / 76-100) | held-out solved, ckpt 0 / 100 |")
print("| --- | --- | --- | --- |")
for path in STREAMS:
    events = [json.loads(line) for line in open(path)]
    win = collections.defaultdict(list)
    for e in events:
        if e["event"] == "training":
            win[(e["index"] - 1) // 25].append(e["episode"])
    audit = {e["checkpoint"]: e["arms"]["current"] for e in events if e["event"] == "audit"}
    first, last = win[0], win[max(win)]
    steps = lambda eps: sum(len(x["gui_trace"]) for x in eps) / len(eps)
    held = lambda rows: f"{sum(r['solved'] for r in rows if r['valid'])}/{len(rows)}"
    print(f"| {path.split('/')[-2]} | {sum(x['solved'] for x in first)}/{len(first)} / {sum(x['solved'] for x in last)}/{len(last)} "
          f"| {steps(first):.1f} / {steps(last):.1f} | {held(audit[0])} / {held(audit[max(audit)])} |")
