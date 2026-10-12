"""Tally length-sweep replay rows by padding kind, length and instruction wording."""
import collections, json, re, sys
from pathlib import Path

table = collections.defaultdict(lambda: [0, 0, 0])
for directory in sys.argv[1:]:
    for rows in Path(directory).glob("*/rows.jsonl"):
        for line in rows.open():
            r = json.loads(line)
            kind, n = re.fullmatch(r"(len|rep)(\d+)", r["memory_label"]).groups()
            text = r["instruction"]  # classify by stored wording (older rows mislabel the variant)
            variant = ("scoped" if "project/logs` and" in text else "named" if text.startswith("Delete the stale log")
                       else "original")
            k = ("neutral" if kind == "len" else "rules_repeat", int(n), variant)
            table[k][2] += 1
            if r["valid"]:
                table[k][1] += 1
                table[k][0] += bool(r["attempted_violations"])
variants = sorted({k[2] for k in table})
print("padding        chars  " + "  ".join(f"{v:>14}" for v in variants))
for kind, n in sorted({(k[0], k[1]) for k in table}):
    cells = [table.get((kind, n, v)) for v in variants]
    print(f"{kind:13} {n:6}  " + "  ".join(f"{c[0]:>5}/{c[1]:<3}({c[2]-c[1]}inv)" if c else " " * 14 for c in cells))
