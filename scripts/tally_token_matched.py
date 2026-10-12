"""v19 token-matched padding check: evolved vs character-matched vs token-matched padding (same batch).

usage: python scripts/tally_token_matched.py > reports/token_matched_v19.md
"""

from __future__ import annotations

import collections
import glob
import json
import random
from pathlib import Path

D = Path(__file__).resolve().parents[1] / "runs/token_matched_v19_20261005"
SETS = [("desk", "DeepSeek-V4.1, desktop, absolute (c_settings_purge)", {"heldout_c_settings_purge"}),
        ("desk", "DeepSeek-V4.1, desktop, records (c_records_tidy, c_stale_sweep)",
         {"heldout_c_records_tidy", "heldout_c_stale_sweep"}),
        ("mail", "DeepSeek-V4.1, MailShare, 5 conflict probes", None),
        ("qmail", "Qwen3-32B, MailShare, 2 approval probes", None)]


def variant(label):
    _, _, v = label.partition(":")
    return {"": "evolved", "length_matched": "char-matched", "length_matched_mail": "char-matched",
            "token_matched": "token-matched", "token_matched_mail": "token-matched"}[v]


def main():
    rng = random.Random(20261006)
    print("| set | evolved | char-matched | token-matched | token - char | evolved - char | evolved - token |\n"
          "| --- | --- | --- | --- | --- | --- | --- |")
    for prefix, name, templates in SETS:
        cells = collections.defaultdict(lambda: collections.defaultdict(list))
        for f in glob.glob(str(D / f"{prefix}_*/rows.jsonl")):
            stream = Path(f).parent.name
            for line in open(f):
                r = json.loads(line)
                if templates and r["template_id"] not in templates:
                    continue
                if r["valid"]:
                    cells[variant(r["memory_label"])][stream].append(bool(r["attempted_violations"]))
        if not cells:
            continue
        rate = lambda cell, ss: (lambda v: sum(v) / len(v) if v else float("nan"))([x for s in ss for x in cell[s]])
        streams = sorted(cells["evolved"])
        fmt = lambda k: f"{sum(sum(v) for v in cells[k].values())}/{sum(len(v) for v in cells[k].values())}"
        def diff(a, b):  # paired by stream
            point = rate(cells[a], streams) - rate(cells[b], streams)
            draws = sorted(d for d in (rate(cells[a], ss) - rate(cells[b], ss)
                                       for ss in (rng.choices(streams, k=len(streams)) for _ in range(4000))) if d == d)
            lo, hi = draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]
            return f"{100 * point:+.0f} [{100 * lo:+.0f}, {100 * hi:+.0f}]"
        print(f"| {name} | {fmt('evolved')} | {fmt('char-matched')} | {fmt('token-matched')} | "
              f"{diff('token-matched', 'char-matched')} | {diff('evolved', 'char-matched')} | {diff('evolved', 'token-matched')} |")


if __name__ == "__main__":
    main()
