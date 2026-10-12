"""v20 part A: a rule-retention axis in Phi-4's residual stream (docs/research/computer_use_rule_axis_v20.md).

usage (code-opt env, steering server running):
  python scripts/rule_axis.py select  OUT_DIR        # layer choice on estimation probes only
  python scripts/rule_axis.py project OUT_DIR LAYER  # scores for every memory, probe, state and arm; axis vector

The axis contrasts *inputs* that differ only in whether the standing rules are in memory (rules-only vs.
no-rule memory, and rules + N neutral characters vs. the same number of neutral characters without rules),
read at the last prompt token of the actor's decision state, never the model's actions.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from self_evolving_agent.safety_retention.desktop import Desktop  # noqa: E402
from self_evolving_agent.safety_retention.environment import Rule, Task, Workspace, violations  # noqa: E402
from self_evolving_agent.safety_retention.experiment import ACTOR_PROMPT  # noqa: E402
from self_evolving_agent.safety_retention.memory_ablation import HEADER, ablate, neutral_padding, padded_rules  # noqa: E402
from run_memory_replay import stream_memories  # noqa: E402

URL = os.environ.get("STEER_URL", "http://h012:8014")
MANIFESTS = ["runs/replication_bedrock_20260930/probes-manifest.json", "runs/confirmation_v9_20261001/confirmation-manifest.json"]
EST = {"absolute": ["heldout_c_settings_move", "heldout_c_settings_empty", "heldout_v_purge_list"],
       "records": ["heldout_c_records_tidy", "heldout_v_old_space"]}
EVAL = {"absolute": ["heldout_c_settings_purge", "heldout_v_purge_reorder", "heldout_v_purge_casual"],
        "records": ["heldout_c_stale_sweep", "heldout_v_tmp_sweep"]}
STREAMS = [f"runs/drift_v4_20260930/{l}-seed{s}" for l in ("default", "success_only") for s in (17, 29, 43)]
EPISODES = list(range(5, 101, 5))
PADS = [250, 1000, 4000]


def load_tasks():
    rules, tasks = None, {}
    for f in MANIFESTS:
        m = json.loads((ROOT / f).read_text())
        rules = rules or tuple(Rule(**r) for r in m["rules"])
        for t in m["tasks"]:
            if t["split"] == "heldout":
                tasks.setdefault(t["template_id"], Task(**t))
    return rules, tasks


def messages(task, rules, memory, pinned, state):
    ws = Workspace(task, rules)
    desk = Desktop(ws, recoverable_errors=True, start_folder=task.start_folder or "/workspace/project")
    step = 0
    if state == "s1":  # after selecting the first permitted file in the start folder (records folders: first file)
        rows = [e["id"][6:] for e in desk.view()["elements"] if e["id"].startswith("entry:") and e.get("kind") == "file"]
        safe = [p for p in rows if not violations({"op": "delete", "path": p}, rules, ws.files, task.approvals)]
        desk.step({"op": "click", "element": "entry:" + (safe or rows)[0]})
        step = 1
    payload = {"task": {"instruction": task.instruction, "approvals": list(task.approvals)},
               "original_constraints": [r.text for r in rules] if pinned else [],
               "memory": memory, "step": step, "desktop": desk.view(), "trajectory": list(desk.events)}
    return [{"role": "system", "content": ACTOR_PROMPT}, {"role": "user", "content": json.dumps(payload, sort_keys=True)}]


def rule_ablated(text, rule_texts):
    """Same memory with the header and rule lines replaced by neutral text of equal length."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        out.append(neutral_padding(len(line)) if (s == HEADER or s in rule_texts) and line else line)
    return "\n".join(out)


def memory_set(rule_texts, with_streams):
    mems = [("control", "", 0, "no rules", "No stored memory yet."),
            ("control", "", 0, "rules-only", "Standing user requirements:\n" + "\n".join(rule_texts))]
    for n in PADS:
        with_rules = padded_rules(rule_texts, n, "neutral")
        mems += [("pair", "", n, "rules+pad", with_rules),
                 ("pair", "", n, "pad only", "Interface notes:\n" + neutral_padding(len(with_rules) - 17))]
    if with_streams:
        for run in STREAMS:
            _, chosen = stream_memories(ROOT / run, [], EPISODES)
            for label, m in chosen:
                k = int(label[4:])
                if k not in EPISODES:
                    continue
                name = Path(run).name
                mems += [("evolved", name, k, "evolved", m.text),
                         ("evolved", name, k, "length-matched", ablate(m.text, rule_texts, "length_matched")),
                         ("evolved", name, k, "rule-ablated", rule_ablated(m.text, rule_texts))]
    return mems


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + os.environ["LOCAL_VLLM_KEY"]})
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.loads(r.read())


def collect(items, layers, chunk=32):
    out = []
    for i in range(0, len(items), chunk):
        acts = post("/v1/activations", {"items": [{"messages": m} for _, m in items[i:i + chunk]], "layers": layers})
        out += acts["activations"]
        print(f"  {min(i + chunk, len(items))}/{len(items)}", flush=True)
    return {L: np.array([a[str(L)] for a in out], dtype=np.float32) for L in layers}


def build(probes, arms, states, with_streams):
    rules, tasks = load_tasks()
    rule_texts = [r.text for r in rules]
    meta, items = [], []
    for family, names in probes.items():
        for name in names:
            for state in states:
                for arm in arms:
                    for group, stream, k, kind, text in memory_set(rule_texts, with_streams):
                        meta.append({"family": family, "probe": name, "state": state, "arm": arm, "group": group,
                                     "stream": stream, "episode": k, "kind": kind,
                                     "split": "estimation" if name in sum(EST.values(), []) else "evaluation"})
                        items.append((meta[-1], messages(tasks[name], rules, text, arm == "pinned", state)))
    return meta, items


def axis_from(meta, H, mask):
    """Mean paired difference (with rules - without rules) over the rule-presence pairs selected by mask."""
    idx = {(m["probe"], m["state"], m["arm"], m["episode"], m["kind"]): i for i, m in enumerate(meta)}
    diffs = []
    for i, m in enumerate(meta):
        if not mask(m) or m["kind"] not in {"rules-only", "rules+pad"}:
            continue
        partner = "no rules" if m["kind"] == "rules-only" else "pad only"
        j = idx[(m["probe"], m["state"], m["arm"], m["episode"], partner)]
        diffs.append(H[i] - H[j])
    return np.mean(diffs, axis=0)


def select(out):
    layers = list(range(0, 40, 2)) + [39]
    meta, items = build(EST, ["plain"], ["s0", "s1"], False)
    print(f"selection: {len(items)} states x {len(layers)} layers")
    acts = collect(items, layers)
    probes = sorted({m["probe"] for m in meta})
    rows = []
    for L in layers:
        H = acts[L]
        margins, wins, total = [], 0, 0
        for held in probes:  # leave one estimation probe out
            r = axis_from(meta, H, lambda m: m["probe"] != held)
            u = r / np.linalg.norm(r)
            for i, m in enumerate(meta):
                if m["probe"] != held or m["kind"] not in {"rules-only", "rules+pad"}:
                    continue
                partner = "no rules" if m["kind"] == "rules-only" else "pad only"
                j = next(j for j, n in enumerate(meta) if n["probe"] == held and n["state"] == m["state"]
                         and n["episode"] == m["episode"] and n["kind"] == partner)
                d = float((H[i] - H[j]) @ u)
                margins.append(d)
                wins += d > 0
                total += 1
        spread = float(np.std(H @ (axis_from(meta, H, lambda m: True) / np.linalg.norm(axis_from(meta, H, lambda m: True)))))
        rows.append({"layer": L, "heldout_pairs_correct": f"{wins}/{total}", "accuracy": wins / total,
                     "dprime": float(np.mean(margins) / (spread + 1e-9))})
        print(rows[-1], flush=True)
    best = max(rows, key=lambda r: (r["accuracy"], r["dprime"]))
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "layer_selection.json").write_text(json.dumps({"rows": rows, "chosen": best["layer"]}, indent=1))
    print("chosen layer", best["layer"])


def project(out, L):
    import torch
    probes = {f: EST[f] + EVAL[f] for f in EST}
    meta, items = build(probes, ["plain", "pinned"], ["s0", "s1"], True)
    print(f"projection: {len(items)} states at layer {L}")
    H = collect(items, [L])[L]
    r = axis_from(meta, H, lambda m: m["split"] == "estimation" and m["arm"] == "plain")
    u = r / np.linalg.norm(r)
    proj = H @ u
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / f"states_L{L}.npy", H.astype(np.float16))
    torch.save({L: torch.tensor(r, dtype=torch.bfloat16)}, out / f"axis_L{L}.pt")
    with (out / f"projections_L{L}.jsonl").open("w") as f:
        for m, p in zip(meta, proj):
            f.write(json.dumps({**m, "projection": float(p)}) + "\n")
    print("wrote", out, "||r|| =", float(np.linalg.norm(r)))


if __name__ == "__main__":
    cmd, out = sys.argv[1], sys.argv[2]
    select(out) if cmd == "select" else project(out, int(sys.argv[3]))
