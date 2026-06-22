#!/usr/bin/env python3
"""
Properly measure MAC→PHY deferral for ALL P-EDCA stage 2 attempts.
Uses a greedy in-order matching: each backoff record claims the FIRST
unclaimed STA0 RTS event that is >= ds_end + gap_mac - tolerance.
"""
import csv
import bisect
from collections import Counter
from pathlib import Path

OUT = Path(__file__).parent
PEDCA_NODE = 0
SLACK_BACK = 2.0      # allow PHY TX up to 2us before MAC time (ns-3 slop)
SLACK_FWD = 200.0     # don't match if PHY TX > 200us after MAC time

def categorize(gap):
    if gap < 30: return None
    if gap <= 99:
        s = round((gap - 34) / 9)
        if 0 <= s <= 7:
            return f"slot{s}_pure"
    extra = gap - 97
    n = round(extra / 9)
    return f"slot7+{n}pause"


for run in range(1, 9):
    blog = OUT / f"backoff_log_run{run}.csv"
    tlog = OUT / f"tx_events_run{run}.csv"
    if not blog.exists() or not tlog.exists():
        continue

    with open(blog) as f:
        backoff = []
        for r in csv.DictReader(f):
            try:
                backoff.append({
                    "ds_end": float(r["ds_cts_end_us"]),
                    "gap_mac": float(r["gap_us"]),
                    "outcome": r["outcome"].strip(),
                })
            except (ValueError, KeyError):
                pass
    backoff.sort(key=lambda x: x["ds_end"])

    with open(tlog) as f:
        sta0_rts = sorted([float(r["time_us"]) for r in csv.DictReader(f)
                          if int(r["node_id"]) == PEDCA_NODE
                          and r["frame_type"].strip() == "RTS"])

    # Greedy in-order match
    deferrals_by_cat = {}
    rts_idx = 0
    for r in backoff:
        if r["outcome"] not in ("SUCCESS", "RTS_COLLISION"):
            continue
        target = r["ds_end"] + r["gap_mac"] - SLACK_BACK
        while rts_idx < len(sta0_rts) and sta0_rts[rts_idx] < target:
            rts_idx += 1
        if rts_idx >= len(sta0_rts):
            continue
        actual_rts = sta0_rts[rts_idx]
        deferral = actual_rts - (r["ds_end"] + r["gap_mac"])
        if deferral > SLACK_FWD:
            continue  # bad match
        rts_idx += 1  # claim this RTS
        cat = categorize(r["gap_mac"])
        if cat is None: continue
        key = (cat, r["outcome"])
        deferrals_by_cat.setdefault(key, []).append(deferral)

    print(f"\n── run {run} ──")
    print(f"  {'category':>16s} {'outcome':>14s}  {'n':>4s}  {'min':>5s} {'med':>5s} {'p95':>5s} {'max':>5s}  {'%>3us':>7s}  {'%>10us':>7s}")
    for (cat, outc) in sorted(deferrals_by_cat.keys(), key=lambda k: (k[0], k[1])):
        d = sorted(deferrals_by_cat[(cat, outc)])
        n = len(d)
        if n == 0: continue
        gt3 = sum(1 for x in d if x > 3) / n * 100
        gt10 = sum(1 for x in d if x > 10) / n * 100
        print(f"  {cat:>16s} {outc:>14s}  {n:>4d}  {d[0]:>5.1f} {d[n//2]:>5.1f} {d[int(n*0.95)]:>5.1f} {d[-1]:>5.1f}  {gt3:>6.1f}%  {gt10:>6.1f}%")
