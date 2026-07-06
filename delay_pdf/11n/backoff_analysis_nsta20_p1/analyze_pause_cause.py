#!/usr/bin/env python3
"""
Investigate WHO is TXing during P-EDCA Stage 2 backoff that causes the
backoff timer to be paused (creating "7+pause" cases with MAC gap > 97us).

For each "slot 7+N*9" case (i.e., MAC gap in {106, 115, 124, ...}, where the
backoff was paused for N slots), list all TX events from any node in the
window [ds_end - 5, ds_end + gap_mac]. This shows what was happening on the
medium during our Stage 2 backoff.
"""

import csv
import bisect
from collections import Counter, defaultdict
from pathlib import Path

OUT_DIR = Path(__file__).parent
PEDCA_NODE_ID = 0
AP_NODE_ID = 20

# Slot 7 pure should be at 97us; observed at 99us due to 2us simulator slop.
# Categorize "pure" if gap ∈ [97, 102], "+N pause" if gap ≈ 97 + N*9.
def categorize_gap(gap):
    if gap < 30: return None  # invalid
    # Find pure backoff slot (rounded), assuming AIFS=34, slot=9, with up to ±2us slop
    # Pure values: 34, 43, 52, 61, 70, 79, 88, 97
    # Anything > 97 + 2 means paused
    if gap <= 99:
        slot = round((gap - 34) / 9)
        if 0 <= slot <= 7:
            return f"slot{slot}_pure"
        return None
    # gap > 99: paused
    extra = gap - 97
    n_pause_slots = round(extra / 9)
    return f"slot7+{n_pause_slots}pause"


def load(path, ks):
    with open(path, newline="") as f:
        return [{k: r[k] for k in ks} for r in csv.DictReader(f)]


def analyze(run_idx, n_examples=4):
    blog = OUT_DIR / f"backoff_log_run{run_idx}.csv"
    tlog = OUT_DIR / f"tx_events_run{run_idx}.csv"
    if not blog.exists() or not tlog.exists():
        return None

    backoff = []
    with open(blog, newline="") as f:
        for r in csv.DictReader(f):
            try:
                backoff.append({
                    "ds_end": float(r["ds_cts_end_us"]),
                    "gap": float(r["gap_us"]),
                    "outcome": r["outcome"].strip(),
                })
            except (ValueError, KeyError):
                pass

    tx_events = []
    with open(tlog, newline="") as f:
        for r in csv.DictReader(f):
            try:
                tx_events.append((float(r["time_us"]),
                                  int(r["node_id"]),
                                  r["frame_type"].strip()))
            except (ValueError, KeyError):
                pass
    tx_events.sort()
    tx_times = [e[0] for e in tx_events]

    # Group by category
    by_cat = defaultdict(list)
    for r in backoff:
        if r["outcome"] not in ("RTS_COLLISION", "SUCCESS", "TIMING_EXPIRED"):
            continue
        cat = categorize_gap(r["gap"])
        if cat is None:
            continue
        by_cat[cat].append(r)

    return by_cat, tx_events, tx_times


def what_txed_during_backoff(records, tx_events, tx_times, ds_end, gap):
    """Return list of (rel_time_us, node, frame) for events in [ds_end, ds_end+gap]."""
    lo = ds_end - 1
    hi = ds_end + gap + 1
    lo_i = bisect.bisect_left(tx_times, lo)
    hi_i = bisect.bisect_right(tx_times, hi)
    res = []
    for i in range(lo_i, hi_i):
        t, n, ft = tx_events[i]
        # Skip our own DS-CTS recordings (which start before ds_end)
        if t < ds_end - 0.5:
            continue
        res.append((t - ds_end, n, ft))
    return res


def main():
    runs = sorted(int(p.stem.replace("tx_events_run", ""))
                  for p in OUT_DIR.glob("tx_events_run*.csv"))

    # Aggregate: for each category, count what frame types appear during the
    # Stage 2 backoff window
    agg_during_bo = defaultdict(lambda: Counter())  # cat -> Counter of (node_role, frame_type)
    cat_count = Counter()

    examples = defaultdict(list)  # cat -> [(run, record, events_during_bo), ...]

    for run_idx in runs:
        result = analyze(run_idx)
        if not result:
            continue
        by_cat, tx_events, tx_times = result
        for cat, recs in by_cat.items():
            for r in recs:
                cat_count[cat] += 1
                evts = what_txed_during_backoff(
                    [r], tx_events, tx_times, r["ds_end"], r["gap"]
                )
                for rel, n, ft in evts:
                    role = "AP" if n == AP_NODE_ID else (
                        "P-EDCA-STA" if n == PEDCA_NODE_ID else "legacy")
                    agg_during_bo[cat][(role, ft)] += 1
                if len(examples[cat]) < 3:
                    examples[cat].append((run_idx, r, evts))

    # Print summary
    cat_order = ["slot0_pure","slot1_pure","slot2_pure","slot3_pure","slot4_pure",
                 "slot5_pure","slot6_pure","slot7_pure",
                 "slot7+1pause","slot7+2pause","slot7+3pause","slot7+4pause"]
    print(f"\n{'='*78}")
    print(f"  What TXed during Stage 2 backoff window?  (window = [ds_end, ds_end+gap])")
    print(f"{'='*78}")
    print(f"\n{'category':>16s} {'n':>5s}  {'TX events seen DURING our Stage2 backoff':<40s}")
    for cat in cat_order:
        if cat not in cat_count:
            continue
        n = cat_count[cat]
        c = agg_during_bo[cat]
        if not c:
            print(f"  {cat:>14s}  {n:>5d}  (no other TX seen during backoff)")
            continue
        print(f"  {cat:>14s}  {n:>5d}")
        for (role, ft), count in c.most_common(8):
            avg_per_attempt = count / n
            print(f"  {'':>14s}         {role:>10s}  {ft:>15s}: {count:>5d}  "
                  f"({avg_per_attempt:.2f} per attempt)")

    # Show concrete examples for "slot7+1pause" — the dominant case
    print(f"\n{'='*78}")
    print(f"  Concrete examples — what's happening during the 1-slot pause?")
    print(f"{'='*78}")
    for cat in ["slot7+1pause", "slot7+2pause"]:
        if cat not in examples:
            continue
        print(f"\n  ── {cat} examples ──")
        for run_idx, r, evts in examples[cat][:3]:
            print(f"\n  run{run_idx}  ds_end={r['ds_end']:.0f}  gap={r['gap']:.0f}us  outcome={r['outcome']}")
            print(f"  Events from t=ds_end to t=ds_end+{r['gap']:.0f}:")
            for rel, n, ft in evts:
                role = "AP" if n == AP_NODE_ID else (
                    "OURS" if n == PEDCA_NODE_ID else f"legacy(STA{n})")
                print(f"    rel +{rel:>5.1f}us   {role:>14s}   {ft}")


if __name__ == "__main__":
    main()
