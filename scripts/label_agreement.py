"""v14 audit of the surgery labels with a second, independent model labeller.

usage: python scripts/label_agreement.py MODEL_CONFIG OUT.json LABELS.json [LABELS.json ...]

Relabels every sentence in the given DeepSeek-V4.1 label files with the same
rubric and batching (make_exploit_surgery.py) under another model, then reports
agreement: raw agreement, Cohen's kappa, the confusion matrix, and how many of
the sentences DeepSeek marked EXPLOIT (the ones minus_exploit replaced) the
second labeller also marks EXPLOIT.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_exploit_surgery as surgery  # noqa: E402
from self_evolving_agent.safety_retention.backends import ModelBackend  # noqa: E402
from self_evolving_agent.safety_retention.drift_fixtures import RULES  # noqa: E402


def kappa(pairs):
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    ca, cb = collections.Counter(a for a, _ in pairs), collections.Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in surgery.LABELS) / (n * n)
    return po, (po - pe) / (1 - pe)


def main(config, out, label_files):
    first = {}
    for f in label_files:
        first.update(json.loads(Path(f).read_text()))
    out = Path(out)
    second = json.loads(out.read_text()) if out.exists() else {}
    backend = ModelBackend(json.loads(Path(config).read_text()), max_calls=200, max_cost_usd=3.0,
                           ledger=out.with_name(out.stem + "_calls.jsonl").resolve())
    surgery.label_sentences(list(first), [dict(r)["text"] for r in RULES], second, backend)
    out.write_text(json.dumps(second, indent=1, sort_keys=True) + "\n")
    pairs = [(first[s], second[s]) for s in first]
    po, k = kappa(pairs)
    print(f"sentences {len(pairs)}; agreement {100 * po:.1f}%; Cohen's kappa {k:.2f}")
    conf = collections.Counter(pairs)
    print("rows = DeepSeek-V4.1, columns = second labeller")
    print("| | " + " | ".join(surgery.LABELS) + " |\n| --- |" + " --- |" * len(surgery.LABELS))
    for a in surgery.LABELS:
        print(f"| {a} | " + " | ".join(str(conf[(a, b)]) for b in surgery.LABELS) + " |")
    ex = [s for s in first if first[s] == "EXPLOIT"]
    print(f"replaced (DeepSeek EXPLOIT) also EXPLOIT for second labeller: {sum(second[s] == 'EXPLOIT' for s in ex)}/{len(ex)}")
    for lab in surgery.LABELS:
        k1 = [s for s in first if first[s] == lab]
        print(f"  {lab}: {sum(second[s] == lab for s in k1)}/{len(k1)} agree")
    print("cost", backend.totals())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
