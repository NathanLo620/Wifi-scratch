#!/usr/bin/env python3
"""
P99/P95/P50 delay heatmap, as a plain CSV.

Same grid content as gen_heatmap_excel.py's param_p99_heatmap_<rate>.xlsx
(STA-type sections x nPedca sections x metric blocks x CWds blocks x
QSRC(rows) x PSRC(cols)), serialized as one CSV instead of a multi-sheet
workbook -- CWds=0 and CWds=1 are stacked as separate row-blocks rather
than side-by-side columns.

Usage: gen_heatmap_csv.py <combo_percentile_summary.csv> <out.csv> [--rate 1Mbps]
"""

import csv
import sys
from pathlib import Path

QSRC = [0, 1, 2, 3, 4, 5]
PSRC = [1, 2, 3]
CWDS = [0, 1]
STA_TYPES = [("vo", "All STAs"), ("pedca", "P-EDCA STAs"), ("legacy", "Legacy STAs")]
N_FOR = {"vo": [5, 15, 30], "pedca": [5, 15, 30], "legacy": [5, 15]}
METRICS = ["P50_us", "P95_us", "P99_us"]

RATE = "1Mbps"
if "--rate" in sys.argv:
    RATE = sys.argv[sys.argv.index("--rate") + 1]

summary_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])

DATA = {}  # (sta_type, cwds, qsrc, psrc, nPedca, metric) -> value
for r in csv.DictReader(open(summary_path)):
    key_base = (r["delay_type"], int(r["CWds"]), int(r["QSRC"]), int(r["PSRC"]), int(r["nPedca"]))
    for m in METRICS:
        DATA[key_base + (m,)] = float(r[m])

rows = []
rows.append([f"P-EDCA delay heatmap (us) -- rows=QSRC, cols=PSRC -- rate={RATE}"])
rows.append([])

for sta_type, label in STA_TYPES:
    rows.append(["STA_TYPE", label])
    for n in N_FOR[sta_type]:
        rows.append([f"nPedca={n}/30"])
        for metric in METRICS:
            rows.append([metric.replace("_us", "")])
            for cwds in CWDS:
                rows.append([f"CWds={cwds}"] + [f"PSRC={p}" for p in PSRC])
                for q in QSRC:
                    row = [f"QSRC={q}"]
                    for p in PSRC:
                        v = DATA.get((sta_type, cwds, q, p, n, metric))
                        row.append(f"{v:.1f}" if v is not None else "")
                    rows.append(row)
            rows.append([])
        rows.append([])
    rows.append([])

with open(out_path, "w", newline="") as f:
    w = csv.writer(f)
    for row in rows:
        w.writerow(row)

print(f"wrote {out_path}")
