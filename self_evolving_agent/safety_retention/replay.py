"""Standalone, read-only GUI trace viewer for inspecting checkpoint failures."""

from __future__ import annotations

import json
from pathlib import Path

from .desktop import Desktop
from .environment import Rule, Task, Workspace


def write_replay(manifest: dict, report: dict, destination: Path) -> None:
    rules = tuple(Rule(**r) for r in manifest["rules"])
    tasks = {row["task_id"]: Task(**row) for row in manifest["tasks"]}
    examples = []
    for stream in report["trajectories"]:
        # Keep the viewer small: first trial of each final-checkpoint probe.
        seen = set()
        for row in stream["audits"][-1]["arms"]["current"]:
            if row["task_id"] in seen:
                continue
            seen.add(row["task_id"])
            task = tasks[row["task_id"]]
            workspace = Workspace(task, rules, row["guard"])
            # Re-simulate with the run's own GUI-error mode and start folder.
            desktop = Desktop(workspace, recoverable_errors=manifest.get("recoverable_gui_errors", False),
                              start_folder=task.start_folder or "/workspace/project")
            frames = [{"view": desktop.view(), "last_action": None, "observation": {},
                       "attempted": [], "realized": [], "files": sorted(workspace.files)}]
            for event in row["gui_trace"]:
                desktop.step(event["action"])
                outcome = workspace.result()
                frames.append({"view": desktop.view(), "last_action": event["action"],
                               "observation": event["observation"],
                               "attempted": outcome["attempted_violations"],
                               "realized": outcome["realized_violations"], "files": sorted(workspace.files)})
            examples.append({"label": f"{stream['condition']} · seed {stream['stream_seed']} · {task.task_id}",
                             "instruction": task.instruction, "frames": frames})
    payload = json.dumps({"evidence_kind": report["evidence_kind"], "rules": [r.text for r in rules],
                          "examples": examples}).replace("<", "\\u003c").replace("&", "\\u0026")
    html = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Computer-use safety trace replay</title>
<style>
body{font:16px system-ui;margin:2rem;background:#f4f6f8;color:#17212b;max-width:1200px}
select{font:inherit;width:100%;padding:.6rem}input[type=range]{width:100%}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:1rem}.card{background:white;border:1px solid #d5dbe1;border-radius:8px;padding:1rem;margin:1rem 0}
.control{padding:.5rem;border-bottom:1px solid #e6e9ec;white-space:pre-wrap;overflow-wrap:anywhere}
.selected{background:#dceeff}.muted{color:#586776}.danger{color:#9b1625;font-weight:600}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}button{font:inherit;padding:.3rem .8rem}
@media(max-width:750px){.grid{grid-template-columns:1fr}}
</style>
<p><b>Computer-use constraint retention — recorded trace replay</b></p>
<p id="evidence" class="danger"></p><p class="muted">Read-only virtual desktop. Controls below replay recorded actions; they do not operate your computer or send messages.</p>
<select id="example" aria-label="Select trajectory"></select><p id="instruction"></p>
<button id="back">Previous</button> <button id="next">Next</button> <span id="position"></span>
<input id="step" type="range" min="0" value="0" aria-label="Recorded action step">
<div class="grid"><div class="card"><p id="app"></p><div id="desktop"></div></div>
<div><div class="card"><b>Last action and feedback</b><pre id="action"></pre></div>
<div class="card"><b>Policy violations</b><pre id="violations"></pre></div>
<div class="card"><b>Current virtual files</b><pre id="files"></pre></div></div></div>
<div class="card"><b>Original user constraints</b><div id="rules"></div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const data=JSON.parse(document.getElementById('data').textContent);
const el=id=>document.getElementById(id);
el('evidence').textContent=data.evidence_kind==='synthetic_smoke_only'?'SYNTHETIC SMOKE ONLY — planted behavior, not a real-model research result.':data.evidence_kind;
data.examples.forEach((row,i)=>{const o=document.createElement('option');o.value=i;o.textContent=row.label;el('example').append(o)});
data.rules.forEach(rule=>{const p=document.createElement('p');p.textContent=rule;el('rules').append(p)});
function draw(){const row=data.examples[Number(el('example').value)||0];if(!row)return;
el('step').max=row.frames.length-1;const i=Math.min(Number(el('step').value),row.frames.length-1);const f=row.frames[i];
el('instruction').textContent=row.instruction;el('position').textContent=`Step ${i} / ${row.frames.length-1}`;
el('app').textContent=`${f.view.application} — ${f.view.folder}`;el('desktop').replaceChildren();
f.view.elements.forEach(item=>{const d=document.createElement('div');d.className='control'+(item.selected?' selected':'');
d.textContent=`${item.role==='textbox'?'Text field':item.kind||item.role}: ${item.label||item.id}${item.value!==undefined?' = '+item.value:''}`;el('desktop').append(d)});
el('action').textContent=JSON.stringify({action:f.last_action,feedback:f.observation},null,2);
el('violations').textContent=JSON.stringify({attempted:f.attempted,executed:f.realized},null,2);
el('files').textContent=f.files.join('\n')||'(no files)';}
el('example').onchange=()=>{el('step').value=0;draw()};el('step').oninput=draw;
el('back').onclick=()=>{el('step').value=Math.max(0,Number(el('step').value)-1);draw()};
el('next').onclick=()=>{el('step').value=Math.min(Number(el('step').max),Number(el('step').value)+1);draw()};draw();
</script></html>"""
    destination.write_text(html.replace("__DATA__", payload))
