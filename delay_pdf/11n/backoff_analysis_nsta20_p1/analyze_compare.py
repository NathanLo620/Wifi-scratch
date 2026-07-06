#!/usr/bin/env python3
"""
For each Stage 2 attempt, walk forward in the log until we find ONE of:
  - "STAGE2 FAIL:COLLISION"  -> RTS_COLLISION
  - "STAGE2 FAIL:CTS_TIMEOUT" -> CTS_TIMEOUT
  - "P-EDCA VO SUCCESS ... Stage2-PEDCA" -> SUCCESS
  - "STALE STAGE2" (in the Payload line) -> STALE_ABORT
  - Next "STAGE2 BACKOFF" before any of above -> close as UNKNOWN
"""
import re, glob, os
from collections import defaultdict, Counter

ROOT = "/home/wmnlab/Desktop/ns-3.45/scratch/delay_pdf/backoff_analysis_nsta20_p1"

def parse_clog(path):
    lines = open(path).readlines()
    events = []
    cur = None
    for L in lines:
        m = re.search(r"\[P-EDCA STAGE2 BACKOFF\] t=\d+us\s+initialBackoff=(\d+)", L)
        if m:
            if cur is not None and cur["outcome"] is None:
                cur["outcome"] = "UNKNOWN"
                events.append(cur)
            cur = {"bo": int(m.group(1)), "outcome": None}
            continue
        if cur is None or cur["outcome"] is not None:
            continue
        if "STAGE2] Payload" in L and "STALE" in L:
            cur["outcome"] = "STALE_ABORT"; events.append(cur); cur = None
        elif "STAGE2 FAIL:COLLISION" in L:
            cur["outcome"] = "RTS_COLLISION"; events.append(cur); cur = None
        elif "STAGE2 FAIL:CTS_TIMEOUT" in L:
            cur["outcome"] = "CTS_TIMEOUT"; events.append(cur); cur = None
        elif "P-EDCA VO SUCCESS" in L and "Stage2-PEDCA" in L:
            cur["outcome"] = "SUCCESS"; events.append(cur); cur = None
    if cur is not None:
        if cur["outcome"] is None: cur["outcome"] = "UNKNOWN"
        events.append(cur)
    return events

def aggregate(label, files):
    by_bo = defaultdict(Counter)
    overall = Counter()
    for fn in files:
        for ev in parse_clog(fn):
            by_bo[ev["bo"]][ev["outcome"]] += 1
            overall[ev["outcome"]] += 1
    print(f"\n========== {label} ({len(files)} seeds) ==========")
    print(f"Total Stage 2 attempts: {sum(overall.values())}")
    print(f"Outcomes: {dict(overall)}")
    succ = overall.get("SUCCESS", 0); total = sum(overall.values())
    print(f"Overall SUCCESS rate: {succ}/{total} = {100*succ/total:.2f}%\n")
    print(f"{'BO':>3} | {'attempts':>8} | {'SUCC':>5} | {'COLL':>5} | {'STALE':>5} | {'CTS_TO':>6} | {'UNK':>4} | {'success%':>9}")
    print('-'*70)
    for bo in range(8):
        row = by_bo[bo]; att = sum(row.values())
        s = row.get("SUCCESS", 0); c = row.get("RTS_COLLISION", 0)
        st = row.get("STALE_ABORT", 0); cto = row.get("CTS_TIMEOUT", 0)
        u = row.get("UNKNOWN", 0)
        rate = 100*s/att if att else 0
        print(f"{bo:>3} | {att:>8} | {s:>5} | {c:>5} | {st:>5} | {cto:>6} | {u:>4} | {rate:>8.2f}%")
    return by_bo, overall

files6 = sorted(glob.glob(os.path.join(ROOT, "clog_OfdmRate6Mbps_seed*.log")))
files9 = sorted(glob.glob(os.path.join(ROOT, "clog_OfdmRate9Mbps_seed*.log")))

bo6, ov6 = aggregate("OfdmRate6Mbps (RTS=52us)", files6)
bo9, ov9 = aggregate("OfdmRate9Mbps (RTS=44us)", files9)

print("\n=========== Side-by-side success rates by BO ===========")
print(f"{'BO':>3} | {'6M atts':>7} {'6M S':>5} {'6M succ%':>8} | {'9M atts':>7} {'9M S':>5} {'9M succ%':>8} | {'Δsucc%':>7}")
print('-'*78)
for bo in range(8):
    a6=sum(bo6[bo].values()); s6=bo6[bo].get("SUCCESS",0)
    a9=sum(bo9[bo].values()); s9=bo9[bo].get("SUCCESS",0)
    r6 = 100*s6/a6 if a6 else 0
    r9 = 100*s9/a9 if a9 else 0
    diff = r9-r6 if (a6 and a9) else 0
    print(f"{bo:>3} | {a6:>7} {s6:>5} {r6:>7.2f}% | {a9:>7} {s9:>5} {r9:>7.2f}% | {diff:>+6.2f}%")
