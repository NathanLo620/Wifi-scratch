#!/usr/bin/env python3
"""
Collision Source Attribution v2 — uses ACTUAL PHY TX time of P-EDCA STA's RTS
==============================================================================
The MAC-level "gap" recorded in backoff log is the time of the timing-check
inside QosFrameExchangeManager, NOT the actual PHY TX start time. Between the
two there can be a 0-50us deferral if medium becomes busy.

This script:
  1. For each backoff_log record, find the *actual* STA0 RTS PHY TX time (from
     tx_events) — the first STA0 RTS event whose time is >= ds_end + gap_us.
  2. Compute actual_gap_us = actual_rts_time - ds_end_us.
  3. Find concurrent TXs from OTHER nodes within ±preamble_window of actual RTS.
  4. Report breakdown by drawn slot.
"""

import csv
import bisect
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT_DIR = Path(__file__).parent
PEDCA_NODE_ID = 0
AP_NODE_ID = 20
PREAMBLE_WIN = 4.5   # ±us; events within this window of our RTS overlap at PHY


def load_backoff(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "sta_id": int(r["sta_id"]),
                    "ds_end": float(r["ds_cts_end_us"]),
                    "gap_mac": float(r["gap_us"]),
                    "outcome": r["outcome"].strip(),
                })
            except (ValueError, KeyError):
                continue
    return rows


def load_tx(path):
    """List of (time_us, node_id, frame_type)."""
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                out.append((float(r["time_us"]), int(r["node_id"]),
                           r["frame_type"].strip()))
            except (ValueError, KeyError):
                continue
    out.sort()
    return out


def categorize_drawn_slot(gap_us, AIFS=34.0, SLOT=9.0, CWMAX=7):
    if gap_us < AIFS - 0.5:
        return -1, False
    pure = round((gap_us - AIFS) / SLOT)
    if pure <= CWMAX:
        return max(0, pure), False
    return CWMAX, True


def analyze_run(run_idx):
    blog = OUT_DIR / f"backoff_log_run{run_idx}.csv"
    tlog = OUT_DIR / f"tx_events_run{run_idx}.csv"
    if not blog.exists() or not tlog.exists():
        return [], []

    backoff = load_backoff(blog)
    tx_events = load_tx(tlog)
    tx_times = [e[0] for e in tx_events]

    # Pre-compute STA0 RTS event times (sorted)
    sta0_rts_times = [e[0] for e in tx_events
                      if e[1] == PEDCA_NODE_ID and e[2] == "RTS"]
    sta0_rts_times.sort()

    findings_collision = []
    findings_success = []

    for r in backoff:
        if r["outcome"] not in ("RTS_COLLISION", "SUCCESS"):
            continue
        if r["sta_id"] != PEDCA_NODE_ID:
            continue
        # Find first STA0 RTS time >= ds_end + gap_mac (with small tolerance)
        target = r["ds_end"] + r["gap_mac"] - 1.0
        idx = bisect.bisect_left(sta0_rts_times, target)
        if idx >= len(sta0_rts_times):
            continue
        actual_rts = sta0_rts_times[idx]
        # Skip if too far (e.g., >300us deferred — likely wrong match)
        if actual_rts - r["ds_end"] > 300:
            continue
        actual_gap = actual_rts - r["ds_end"]
        deferral = actual_rts - (r["ds_end"] + r["gap_mac"])

        # Concurrent TXs in ±preamble window from OTHER nodes
        lo_i = bisect.bisect_left(tx_times, actual_rts - PREAMBLE_WIN)
        hi_i = bisect.bisect_right(tx_times, actual_rts + PREAMBLE_WIN)
        concurrent = []
        for i in range(lo_i, hi_i):
            t, n, ft = tx_events[i]
            if n == PEDCA_NODE_ID:
                continue
            concurrent.append((t - actual_rts, n, ft))

        slot, paused = categorize_drawn_slot(r["gap_mac"])
        rec = {
            "ds_end": r["ds_end"],
            "gap_mac": r["gap_mac"],
            "actual_gap": actual_gap,
            "deferral": deferral,
            "drawn_slot": slot,
            "paused": paused,
            "concurrent": concurrent,
            "outcome": r["outcome"],
        }
        if r["outcome"] == "RTS_COLLISION":
            findings_collision.append(rec)
        else:
            findings_success.append(rec)

    return findings_collision, findings_success


def summarize(coll, succ):
    print(f"\n{'='*78}")
    print(f"  RTS_COLLISION analysis using ACTUAL PHY TX time")
    print(f"  Total: {len(coll)} collisions, {len(succ)} successes")
    print(f"  ±preamble window = {PREAMBLE_WIN}us")
    print(f"{'='*78}")

    # Group by drawn slot
    by_slot = defaultdict(lambda: {"coll": [], "succ": []})
    for r in coll:
        key = "7+pause" if (r["drawn_slot"] == 7 and r["paused"]) else str(r["drawn_slot"])
        by_slot[key]["coll"].append(r)
    for r in succ:
        key = "7+pause" if (r["drawn_slot"] == 7 and r["paused"]) else str(r["drawn_slot"])
        by_slot[key]["succ"].append(r)

    print(f"\n{'slot':>10s} {'n_coll':>8s} {'n_succ':>8s} {'mac_gap':>8s} "
          f"{'phy_gap_coll':>14s} {'defer_coll':>12s} {'defer_succ':>12s}")
    for key in sorted(by_slot.keys(), key=lambda x: (1 if "+" in x else 0, x)):
        cs = by_slot[key]["coll"]
        ss = by_slot[key]["succ"]
        avg_mac = sum(r["gap_mac"] for r in cs+ss) / len(cs+ss) if (cs or ss) else 0
        avg_phy_c = sum(r["actual_gap"] for r in cs) / len(cs) if cs else 0
        avg_def_c = sum(r["deferral"] for r in cs) / len(cs) if cs else 0
        avg_def_s = sum(r["deferral"] for r in ss) / len(ss) if ss else 0
        print(f"{key:>10s} {len(cs):>8d} {len(ss):>8d} {avg_mac:>8.1f} "
              f"{avg_phy_c:>14.1f} {avg_def_c:>12.1f} {avg_def_s:>12.1f}")

    # Concurrent-TX (preamble overlap) analysis per drawn slot
    print(f"\n{'='*78}")
    print(f"  Concurrent TX within ±{PREAMBLE_WIN}us of our RTS PHY TX:")
    print(f"{'='*78}")
    for key in sorted(by_slot.keys(), key=lambda x: (1 if "+" in x else 0, x)):
        cs = by_slot[key]["coll"]
        ss = by_slot[key]["succ"]
        if not cs and not ss:
            continue
        coll_overlap = sum(1 for r in cs if r["concurrent"])
        succ_overlap = sum(1 for r in ss if r["concurrent"])
        print(f"\n  Slot {key}:")
        print(f"    Collisions with concurrent TX in ±{PREAMBLE_WIN}us: "
              f"{coll_overlap}/{len(cs)} = {coll_overlap/max(1,len(cs))*100:.1f}%")
        print(f"    Successes  with concurrent TX in ±{PREAMBLE_WIN}us: "
              f"{succ_overlap}/{len(ss)} = {succ_overlap/max(1,len(ss))*100:.1f}%")
        # Frame-type breakdown of colliders
        if cs:
            ft = Counter()
            for r in cs:
                for off, n, fr in r["concurrent"]:
                    role = "AP" if n == AP_NODE_ID else "legacy"
                    ft[(role, fr)] += 1
            if ft:
                print(f"    Colliding frame types from others:")
                for (role, fr), c in ft.most_common(8):
                    print(f"      {role:6s} {fr:18s}: {c}")


def plot_actual_gap_distribution(coll, succ, out_path):
    """Histogram of actual PHY-level gap, split by outcome."""
    fig, ax = plt.subplots(figsize=(12, 6))
    bins = list(range(30, 251, 3))
    coll_gaps = [r["actual_gap"] for r in coll if r["actual_gap"] <= 250]
    succ_gaps = [r["actual_gap"] for r in succ if r["actual_gap"] <= 250]
    ax.hist(succ_gaps, bins=bins, alpha=0.6, color="#2ca02c",
            label=f"SUCCESS (n={len(succ_gaps)})")
    ax.hist(coll_gaps, bins=bins, alpha=0.6, color="#d62728",
            label=f"RTS_COLLISION (n={len(coll_gaps)})")
    ax.axvline(77, color="red", linestyle="--", linewidth=1.2,
               label="NAV window = 77us")
    ax.axvline(111, color="black", linestyle=":", linewidth=1.2,
               label="NAV+AIFS = 111us (legacy can fire)")
    ax.set_xlabel("Actual PHY-level gap from DS-CTS end to RTS TX start (us)",
                  fontsize=11)
    ax.set_ylabel("Count", fontsize=11)
    ax.set_title("Actual RTS TX time distribution: SUCCESS vs RTS_COLLISION",
                 fontsize=12, fontweight="bold")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3, linestyle="--")
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=200)
    plt.close(fig)


def plot_deferral_vs_outcome(coll, succ, out_path):
    """Scatter: MAC-gap vs PHY-gap, color by outcome."""
    fig, ax = plt.subplots(figsize=(10, 8))
    s = [r for r in succ if r["actual_gap"] <= 250]
    c = [r for r in coll if r["actual_gap"] <= 250]
    ax.scatter([r["gap_mac"] for r in s], [r["actual_gap"] for r in s],
               c="#2ca02c", alpha=0.4, s=15, label=f"SUCCESS (n={len(s)})")
    ax.scatter([r["gap_mac"] for r in c], [r["actual_gap"] for r in c],
               c="#d62728", alpha=0.6, s=18, label=f"RTS_COLLISION (n={len(c)})",
               edgecolor="black", linewidth=0.3)
    # y=x line (no deferral)
    lo, hi = 30, 250
    ax.plot([lo, hi], [lo, hi], color="gray", linestyle="--", linewidth=1,
            label="No deferral (y=x)")
    ax.axhline(111, color="black", linestyle=":", linewidth=1.2,
               label="Legacy fire boundary (111us)")
    ax.axvline(97, color="orange", linestyle="--", linewidth=1.2,
               label="Slot 7 MAC gap (97us)")
    ax.set_xlabel("MAC-level gap (recorded in backoff_log) [us]", fontsize=11)
    ax.set_ylabel("Actual PHY TX gap (from PHY trace) [us]", fontsize=11)
    ax.set_title("MAC gap vs PHY gap, colored by outcome\n"
                 "Vertical distance from y=x = how much the RTS was deferred",
                 fontsize=12, fontweight="bold")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3, linestyle="--")
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=200)
    plt.close(fig)


def main():
    runs = sorted(int(p.stem.replace("tx_events_run", ""))
                  for p in OUT_DIR.glob("tx_events_run*.csv"))
    print(f"Analyzing runs: {runs}")
    all_coll = []
    all_succ = []
    for i in runs:
        c, s = analyze_run(i)
        all_coll.extend(c)
        all_succ.extend(s)
        print(f"  run {i}: {len(c)} coll, {len(s)} succ")

    summarize(all_coll, all_succ)
    plot_actual_gap_distribution(all_coll, all_succ,
                                 OUT_DIR / "06_actual_phy_gap_distribution.pdf")
    plot_deferral_vs_outcome(all_coll, all_succ,
                             OUT_DIR / "07_mac_gap_vs_phy_gap.pdf")
    print("\n  ✔ 06_actual_phy_gap_distribution.pdf")
    print("  ✔ 07_mac_gap_vs_phy_gap.pdf")


if __name__ == "__main__":
    main()
