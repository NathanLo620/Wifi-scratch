#!/usr/bin/env python3
"""
Collision Source Attribution Analysis
======================================
For each P-EDCA RTS_COLLISION event, identify WHO collided with the P-EDCA STA
and WHEN they started transmitting (relative to our RTS start time).

Inputs (per run):
  backoff_log_run{i}.csv : per-DS-CTS records
  tx_events_run{i}.csv   : per-PHY TX events (every node, every frame)

Node ID mapping:
  - STA index 0..19 → node_id 0..19
  - AP             → node_id 20
  - P-EDCA STA(s) are first nPedcaSta STAs (here: only node_id=0)

Method:
  - For each backoff record where outcome ∈ {RTS_COLLISION, RTS_CTS_TIMEOUT},
    compute rts_start = ds_cts_end_us + gap_us
  - Find TX events from OTHER nodes within [rts_start - WINDOW, rts_start + WINDOW]
  - Categorize: frame type, node, time offset from rts_start
"""

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT_DIR = Path(__file__).parent
PEDCA_NODE_ID = 0       # only one P-EDCA STA
AP_NODE_ID = 20
LEGACY_NODES = set(range(1, 20))

# A TX is considered "concurrent" with our RTS if it starts within this window.
# Preamble + L-SIG ≈ 20us; if the colliding TX starts within ±4us they truly collide
# at PHY (preamble overlap). We use a wider window to also see what was already
# in-flight at the time.
COLLISION_WINDOW_US = 20.0  # ± window
PREAMBLE_OVERLAP_US = 4.0   # tight preamble-collision window


def load_backoff(path):
    rows = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                rows.append({
                    "sta_id": int(row["sta_id"]),
                    "ds_cts_end_us": float(row["ds_cts_end_us"]),
                    "gap_us": float(row["gap_us"]),
                    "backoff_slots": int(row["backoff_slots"]),
                    "outcome": row["outcome"].strip(),
                })
            except (ValueError, KeyError):
                continue
    return rows


def load_tx_events(path):
    """Returns sorted list of (time_us, node_id, frame_type, size)."""
    events = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                events.append((float(row["time_us"]),
                               int(row["node_id"]),
                               row["frame_type"].strip(),
                               int(row["size_bytes"])))
            except (ValueError, KeyError):
                continue
    events.sort()
    return events


def find_concurrent(events, t_center, t_window, exclude_node):
    """Linear scan for events with time in [t_center-t_window, t_center+t_window]
    and node_id != exclude_node. Returns list of (offset_us, node, frame, size)."""
    lo = t_center - t_window
    hi = t_center + t_window
    res = []
    # NOTE: events is sorted; for one record this is O(n) but with 8 runs * ~118k
    # events and ~200 collision records per run, total is ~190M ops in pure
    # Python. Use binary search to keep it manageable.
    import bisect
    keys = [e[0] for e in events]
    lo_i = bisect.bisect_left(keys, lo)
    hi_i = bisect.bisect_right(keys, hi)
    for i in range(lo_i, hi_i):
        t, n, ft, sz = events[i]
        if n == exclude_node:
            continue
        res.append((t - t_center, n, ft, sz))
    return res


def categorize_drawn_slot(gap_us, AIFS=34.0, SLOT=9.0, CWMAX=7):
    if gap_us < AIFS - 0.5:
        return -1, False
    pure = round((gap_us - AIFS) / SLOT)
    if pure <= CWMAX:
        return max(0, pure), False
    return CWMAX, True  # paused


def analyze_run(run_idx):
    blog = OUT_DIR / f"backoff_log_run{run_idx}.csv"
    tlog = OUT_DIR / f"tx_events_run{run_idx}.csv"
    if not blog.exists() or not tlog.exists():
        return None

    backoff = load_backoff(blog)
    tx_events = load_tx_events(tlog)

    # For each RTS-collision attempt, find concurrent TXs
    findings = []
    for r in backoff:
        if r["outcome"] != "RTS_COLLISION":
            continue
        rts_start = r["ds_cts_end_us"] + r["gap_us"]
        slot, paused = categorize_drawn_slot(r["gap_us"])
        concurrent = find_concurrent(tx_events, rts_start,
                                     COLLISION_WINDOW_US,
                                     exclude_node=r["sta_id"])
        findings.append({
            "rts_start_us": rts_start,
            "ds_cts_end_us": r["ds_cts_end_us"],
            "gap_us": r["gap_us"],
            "drawn_slot": slot,
            "paused": paused,
            "concurrent_txs": concurrent,
        })

    # Also analyze SUCCESS events for slot 7 to compare (control group)
    successes_slot7 = []
    for r in backoff:
        if r["outcome"] != "SUCCESS":
            continue
        slot, paused = categorize_drawn_slot(r["gap_us"])
        if slot != 7 or paused:
            continue
        rts_start = r["ds_cts_end_us"] + r["gap_us"]
        concurrent = find_concurrent(tx_events, rts_start,
                                     COLLISION_WINDOW_US,
                                     exclude_node=r["sta_id"])
        successes_slot7.append(concurrent)

    return findings, successes_slot7


def summarize(all_findings, all_succ_s7):
    by_slot = defaultdict(list)
    for f in all_findings:
        key = "7+pause" if (f["drawn_slot"] == 7 and f["paused"]) else str(f["drawn_slot"])
        by_slot[key].append(f)

    print(f"\n{'='*78}")
    print(f"  Collision-source attribution — {len(all_findings)} RTS_COLLISION events")
    print(f"  COLLISION_WINDOW = ±{COLLISION_WINDOW_US:.0f} us, "
          f"PREAMBLE_OVERLAP = ±{PREAMBLE_OVERLAP_US:.0f} us")
    print(f"{'='*78}")

    for slot_label in sorted(by_slot.keys(),
                             key=lambda x: (1 if "+" in x else 0, x)):
        findings = by_slot[slot_label]
        if not findings:
            continue
        print(f"\n── Drawn slot = {slot_label}  (n={len(findings)} collisions) ──")

        # Closest concurrent TX per collision (= the actual collider)
        closest_offsets = []
        closest_frame = Counter()
        closest_node_role = Counter()
        all_concurrent_frames = Counter()

        for f in findings:
            if not f["concurrent_txs"]:
                closest_frame["(no concurrent TX in window)"] += 1
                continue
            # Pick the TX closest in time to our RTS start
            closest = min(f["concurrent_txs"], key=lambda x: abs(x[0]))
            offset, node, frame, size = closest
            closest_offsets.append(offset)
            closest_frame[frame] += 1
            if node == AP_NODE_ID:
                closest_node_role["AP"] += 1
            elif node in LEGACY_NODES:
                closest_node_role[f"legacy STA"] += 1
            else:
                closest_node_role[f"other (node {node})"] += 1
            for off, n, ft, sz in f["concurrent_txs"]:
                all_concurrent_frames[ft] += 1

        print(f"  Closest colliding TX:")
        print(f"    Frame type breakdown:")
        for ft, c in closest_frame.most_common():
            print(f"      {ft:18s}: {c:4d}  ({c/len(findings)*100:5.1f}%)")
        print(f"    Node role breakdown:")
        for role, c in closest_node_role.most_common():
            print(f"      {role:18s}: {c:4d}  ({c/len(findings)*100:5.1f}%)")
        if closest_offsets:
            sortd = sorted(closest_offsets)
            n = len(sortd)
            print(f"  Time offset (us, our_RTS_start - their_TX_start; negative=their TX started first):")
            print(f"      min:    {-sortd[-1]:.2f}")
            print(f"      median: {-sortd[n//2]:.2f}")
            print(f"      max:    {-sortd[0]:.2f}")
            n_before = sum(1 for o in closest_offsets if o < -PREAMBLE_OVERLAP_US)
            n_overlap = sum(1 for o in closest_offsets if -PREAMBLE_OVERLAP_US <= o <= PREAMBLE_OVERLAP_US)
            n_after = sum(1 for o in closest_offsets if o > PREAMBLE_OVERLAP_US)
            print(f"      Their TX started >4us BEFORE ours (we walked into their TX): {n_before}")
            print(f"      Within ±4us preamble overlap (true simultaneous):           {n_overlap}")
            print(f"      Their TX started >4us AFTER ours (they walked into ours):    {n_after}")

    # Compare slot 7 SUCCESS vs slot 7 COLLISION (within preamble window)
    print(f"\n{'='*78}")
    print(f"  Control comparison: slot 7 SUCCESS vs COLLISION — within ±{PREAMBLE_OVERLAP_US:.0f}us")
    print(f"{'='*78}")
    succ_overlap = 0
    for concurrent in all_succ_s7:
        for off, n, ft, sz in concurrent:
            if abs(off) <= PREAMBLE_OVERLAP_US:
                succ_overlap += 1
                break
    print(f"  Slot 7 SUCCESS attempts: {len(all_succ_s7)}")
    print(f"    With another TX in ±4us preamble overlap: {succ_overlap}  "
          f"({succ_overlap/max(1,len(all_succ_s7))*100:.1f}%)")
    coll7 = by_slot.get("7", [])
    coll7_overlap = 0
    for f in coll7:
        for off, n, ft, sz in f["concurrent_txs"]:
            if abs(off) <= PREAMBLE_OVERLAP_US:
                coll7_overlap += 1
                break
    print(f"  Slot 7 COLLISION attempts: {len(coll7)}")
    print(f"    With another TX in ±4us preamble overlap: {coll7_overlap}  "
          f"({coll7_overlap/max(1,len(coll7))*100:.1f}%)")


def plot_offset_histogram(all_findings, out_path):
    """Histogram of time offset (their_start - our_RTS_start) for all collisions,
    split by drawn slot."""
    by_slot = defaultdict(list)
    for f in all_findings:
        if not f["concurrent_txs"]:
            continue
        closest = min(f["concurrent_txs"], key=lambda x: abs(x[0]))
        offset = -closest[0]  # their_start - our_start = -(our_start - their_start)
        key = "7+pause" if (f["drawn_slot"] == 7 and f["paused"]) else str(f["drawn_slot"])
        by_slot[key].append(offset)

    if not by_slot:
        return None

    fig, ax = plt.subplots(figsize=(12, 6))
    bins = list(range(-int(COLLISION_WINDOW_US), int(COLLISION_WINDOW_US) + 1, 1))
    colors = {"2": "#9467bd", "3": "#9467bd", "4": "#9467bd",
              "5": "#1f77b4", "6": "#2ca02c", "7": "#ff7f0e", "7+pause": "#d62728"}
    for slot in sorted(by_slot.keys(), key=lambda x: (1 if "+" in x else 0, x)):
        offsets = by_slot[slot]
        c = colors.get(slot, "#888888")
        ax.hist(offsets, bins=bins, alpha=0.55,
                label=f"slot {slot} (n={len(offsets)})",
                color=c, edgecolor="white", linewidth=0.3)
    ax.axvspan(-PREAMBLE_OVERLAP_US, PREAMBLE_OVERLAP_US, alpha=0.15, color="red",
               label=f"±{PREAMBLE_OVERLAP_US:.0f}us preamble overlap")
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Time offset: their_TX_start − our_RTS_start (us)\n"
                  "negative = they were already TXing when we started",
                  fontsize=11)
    ax.set_ylabel("Count", fontsize=11)
    ax.set_title("When did the colliding TX start, relative to our RTS?",
                 fontsize=12, fontweight="bold")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3, linestyle="--")
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=200)
    plt.close(fig)
    return out_path


def main():
    runs = sorted(OUT_DIR.glob("tx_events_run*.csv"))
    run_indices = []
    for p in runs:
        try:
            i = int(p.stem.replace("tx_events_run", ""))
            run_indices.append(i)
        except ValueError:
            pass
    run_indices.sort()

    print(f"Analyzing runs: {run_indices}")
    all_findings = []
    all_succ_s7 = []
    for i in run_indices:
        res = analyze_run(i)
        if res is None:
            continue
        findings, succ_s7 = res
        all_findings.extend(findings)
        all_succ_s7.extend(succ_s7)
        print(f"  run {i}: {len(findings)} collisions, {len(succ_s7)} slot-7 successes")

    summarize(all_findings, all_succ_s7)
    plot_offset_histogram(all_findings, OUT_DIR / "05_collision_offset_histogram.pdf")
    print(f"\nPlot saved: 05_collision_offset_histogram.pdf")


if __name__ == "__main__":
    main()
