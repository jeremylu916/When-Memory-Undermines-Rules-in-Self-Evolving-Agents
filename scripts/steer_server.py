"""v15 C: OpenAI-compatible chat server with activation steering (contrastive activation addition).

usage: python scripts/steer_server.py HF_MODEL --port 8014 --vectors VECTORS.pt

The request's "model" field selects the condition:
  base                 no steering
  steer:L:MULT         add MULT * v[L] to the residual stream output of decoder layer L
                       at every position (prompt and generated tokens)
  rand:L:MULT          same, with a fixed random unit direction scaled to ||v[L]||
  record               no generation; returns {"action": {"op": "finish"}} (the harness
                       ledger then holds the exact prompts, used for vector estimation)
Greedy decoding. Concurrent requests are batched (left padding); each row gets
its own layer/multiplier through per-row hooks.

POST /v1/activations {"items": [{"messages": [...], "completion": "..."}], "layers": [..]}
returns the residual-stream output of each listed layer at the last token of
prompt (+ generation prompt) + forced completion, for each item.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

ARGS = None
MODEL = TOK = LAYERS = None
VECTORS: dict[int, torch.Tensor] = {}
RANDOM: dict[int, torch.Tensor] = {}
WORK: queue.Queue = queue.Queue()
API_KEY = ""
GPU_LOCK = threading.Lock()


def parse_condition(name: str):
    if name in {"base", "record"}:
        return name, None, 0.0
    kind, layer, mult = name.split(":")
    return kind, int(layer), float(mult)


def direction(kind, layer):
    if kind == "steer":
        return VECTORS[layer]
    if layer not in RANDOM:
        g = torch.Generator().manual_seed(1000 + layer)
        r = torch.randn(VECTORS[layer].shape, generator=g)
        RANDOM[layer] = (r / r.norm() * VECTORS[layer].float().norm().cpu()).to(VECTORS[layer].dtype)
    return RANDOM[layer].to(VECTORS[layer].device)


def prompt_ids(messages):
    text = TOK.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return TOK(text, add_special_tokens=False)["input_ids"]


@torch.no_grad()
def generate_batch(items):
    """items: list of (messages, max_tokens, condition). Returns texts and usage."""
    ids = [prompt_ids(m) for m, _, _ in items]
    width = max(map(len, ids))
    pad = TOK.pad_token_id if TOK.pad_token_id is not None else TOK.eos_token_id
    input_ids = torch.tensor([[pad] * (width - len(x)) + x for x in ids], device=MODEL.device)
    attn = torch.tensor([[0] * (width - len(x)) + [1] * len(x) for x in ids], device=MODEL.device)
    # per-row additive steering, grouped by layer
    adds: dict[int, torch.Tensor] = {}
    for row, (_, _, (kind, layer, mult)) in enumerate(items):
        if kind in {"steer", "rand"} and mult:
            if layer not in adds:
                adds[layer] = torch.zeros(len(items), VECTORS[layer].shape[-1], dtype=MODEL.dtype, device=MODEL.device)
            adds[layer][row] = mult * direction(kind, layer).to(MODEL.dtype)
    handles = []
    for layer, add in adds.items():
        def hook(module, inputs, output, add=add):
            if isinstance(output, tuple):
                return (output[0] + add[:, None, :],) + output[1:]
            return output + add[:, None, :]
        handles.append(LAYERS[layer].register_forward_hook(hook))
    try:
        out = MODEL.generate(input_ids=input_ids, attention_mask=attn, do_sample=False, temperature=None,
                             top_p=None, top_k=None, max_new_tokens=max(n for _, n, _ in items),
                             pad_token_id=pad)
    finally:
        for h in handles:
            h.remove()
    texts, usage = [], []
    for row, (_, n, _) in enumerate(items):
        gen = out[row, width:].tolist()
        if TOK.eos_token_id in gen:
            gen = gen[:gen.index(TOK.eos_token_id)]
        gen = gen[:n]
        texts.append(TOK.decode(gen, skip_special_tokens=True))
        usage.append((len(ids[row]), len(gen)))
    return texts, usage


def worker():
    while True:
        first = WORK.get()
        batch = [first]
        deadline = time.time() + ARGS.batch_wait
        while len(batch) < ARGS.max_batch:
            try:
                batch.append(WORK.get(timeout=max(0.0, deadline - time.time())))
            except queue.Empty:
                break
        with GPU_LOCK:
            try:
                texts, usage = generate_batch([(b["messages"], b["max_tokens"], b["condition"]) for b in batch])
                for b, t, u in zip(batch, texts, usage):
                    b["result"] = (t, u)
            except Exception as exc:  # noqa: BLE001 - reported to every waiting request
                for b in batch:
                    b["error"] = repr(exc)
        for b in batch:
            b["done"].set()


@torch.no_grad()
def activations(items, layers, batch=8):
    """items: [{"messages": [...], "completion": str (optional, forced assistant text)}].
    Returns the residual output of each layer at the last token, batched with left padding (v20)."""
    seqs = []
    for item in items:
        x = prompt_ids(item["messages"])
        if item.get("completion"):
            x = x + TOK(item["completion"], add_special_tokens=False)["input_ids"]
        seqs.append(x)
    pad = TOK.pad_token_id if TOK.pad_token_id is not None else TOK.eos_token_id
    out = []
    for start in range(0, len(seqs), batch):
        chunk = seqs[start:start + batch]
        width = max(map(len, chunk))
        ids = torch.tensor([[pad] * (width - len(x)) + x for x in chunk], device=MODEL.device)
        attn = torch.tensor([[0] * (width - len(x)) + [1] * len(x) for x in chunk], device=MODEL.device)
        captured = {}
        handles = [LAYERS[L].register_forward_hook(
            lambda m, i, o, L=L: captured.__setitem__(L, (o[0] if isinstance(o, tuple) else o)[:, -1].float().cpu()))
            for L in layers]
        with GPU_LOCK:
            try:
                MODEL(input_ids=ids, attention_mask=attn, position_ids=(attn.cumsum(-1) - 1).clamp(min=0))
            finally:
                for h in handles:
                    h.remove()
        for row in range(len(chunk)):
            out.append({str(L): captured[L][row].tolist() for L in layers})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        return self.headers.get("Authorization", "") == f"Bearer {API_KEY}"

    def do_GET(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        self._send(200, {"object": "list", "data": [{"id": "base"}, {"id": "steer:L:MULT"}, {"id": "record"}],
                         "layers": len(LAYERS), "vector_layers": sorted(VECTORS)})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/vectors"):  # load steering vectors at runtime
            for k, v in torch.load(req["path"]).items():
                VECTORS[int(k)] = v.to(MODEL.device)
            RANDOM.clear()
            return self._send(200, {"vector_layers": sorted(VECTORS)})
        if self.path.endswith("/activations"):
            return self._send(200, {"activations": activations(req["items"], req["layers"])})
        cond = parse_condition(req.get("model", "base"))
        if cond[0] == "record":
            text, usage = json.dumps({"action": {"op": "finish"}}), (0, 0)
        else:
            item = {"messages": req["messages"], "max_tokens": int(req.get("max_tokens") or 512),
                    "condition": cond, "done": threading.Event()}
            WORK.put(item)
            item["done"].wait()
            if "error" in item:
                return self._send(500, {"error": item["error"]})
            text, usage = item["result"]
            if (req.get("response_format") or {}).get("type") == "json_object":
                # v20: JSON mode in vLLM never emits a markdown fence; Phi-4 under HF does. Strip only the fence.
                t = text.strip()
                if t.startswith("```"):
                    t = t.split("\n", 1)[1] if "\n" in t else ""
                    t = t.rsplit("```", 1)[0]
                text = t.strip()
        self._send(200, {"id": "steer", "object": "chat.completion", "model": req.get("model", "base"),
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": text}}],
                         "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                                   "total_tokens": usage[0] + usage[1]}})


def main():
    global ARGS, MODEL, TOK, LAYERS, API_KEY
    p = argparse.ArgumentParser()
    p.add_argument("model")
    p.add_argument("--port", type=int, default=8014)
    p.add_argument("--vectors", default="")
    p.add_argument("--key-file", default=os.path.expanduser(os.environ.get("LOCAL_VLLM_KEY_FILE", "~/.local_vllm_key")))
    p.add_argument("--max-batch", type=int, default=8)
    p.add_argument("--batch-wait", type=float, default=0.05)
    ARGS = p.parse_args()
    API_KEY = open(ARGS.key_file).read().strip()
    TOK = AutoTokenizer.from_pretrained(ARGS.model)
    MODEL = AutoModelForCausalLM.from_pretrained(ARGS.model, dtype=torch.bfloat16).to("cuda")
    MODEL.eval()
    LAYERS = MODEL.model.layers
    if ARGS.vectors:
        for k, v in torch.load(ARGS.vectors).items():
            VECTORS[int(k)] = v.to(MODEL.device)
    threading.Thread(target=worker, daemon=True).start()
    print(f"ready: {ARGS.model}, {len(LAYERS)} layers, vectors at {sorted(VECTORS)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", ARGS.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
