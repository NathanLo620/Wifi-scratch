#!/usr/bin/env python3
"""
P-EDCA Backoff vs Failure Analysis (nSta=20, nPedca=1)
========================================================
Analyzes per-DS-CTS records to answer:
  1. Distribution of post-DS-CTS gap (effective backoff time)
  2. Distribution of reconstructed backoff slots
  3. Failure rate vs backoff slot bucket
  4. Failure cause breakdown
  5. Gap distribution by outcome (overlay)

Each row in the input CSV is one P-EDCA Stage 2 attempt:
  sta_id, ds_cts_end_us, gap_us, backoff_slots, outcome
where outcome ∈ {SUCCESS, RTS_CTS_TIMEOUT, RTS_COLLISION, TIMING_EXPIRED, DEFERRAL}

Usage:
  python3 analyze_backoff_failure.py
  python3 analyze_backoff_failure.py --pattern "backoff_log_run*.csv"
"""

import argparse
import csv
import math
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT_DIR = Path(__file__).parent
NAV_WINDOW_US = 77.0
DEADLINE_US = 200.0
AIFS_US = 34.0
SLOT_US = 9.0


# ─────────── Loading ───────────

def load_records(csv_paths):
    records = []
    for p in csv_paths:
        with open(p, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    records.append({
                        "sta_id": int(row["sta_id"]),
                        "ds_cts_end_us": float(row["ds_cts_end_us"]),
                        "gap_us": float(row["gap_us"]),
                        "backoff_slots": int(row["backoff_slots"]),
                        "outcome": row["outcome"].strip(),
                    })
                except (ValueError, KeyError):
                    continue
    return records


# ─────────── Plot helpers ───────────

OUTCOME_COLORS = {
    "SUCCESS":         "#2ca02c",
    "RTS_CTS_TIMEOUT": "#1f77b4",
    "RTS_COLLISION":   "#ff7f0e",
    "TIMING_EXPIRED":  "#d62728",
    "DEFERRAL":        "#9467bd",
}


def plot_gap_histogram(records, out_path):
    """Histogram of gap_us, with vertical lines at NAV window (77) and deadline (200)."""
    gaps = [r["gap_us"] for r in records if r["outcome"] != "DEFERRAL"]
    if not gaps:
        return None

    # Use 5us bins up to 250us (deadline + margin), plus a single overflow bin
    max_plot = 250.0
    bins = list(range(0, int(max_plot) + 5, 5))

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.hist([min(g, max_plot) for g in gaps], bins=bins, color="#4C72B0",
            edgecolor="white", linewidth=0.5)
    ax.axvline(NAV_WINDOW_US, color="red", linestyle="--", linewidth=1.5,
               label=f"NAV window = {NAV_WINDOW_US:.0f} us (backoff=4.8 slots)")
    ax.axvline(DEADLINE_US, color="black", linestyle=":", linewidth=1.5,
               label=f"Stage 2 deadline = {DEADLINE_US:.0f} us")
    ax.set_xlabel("Gap from DS-CTS end to TX start (us)", fontsize=11)
    ax.set_ylabel("Count", fontsize=11)
    ax.set_title(f"Distribution of Post-DS-CTS Gap (n={len(gaps)} attempts)",
                 fontsize=12, fontweight="bold")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3, linestyle="--")
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=200)
    plt.close(fig)
    return out_path


def plot_gap_by_outcome(records, out_path):
    """Stacked-style histogram: gap distribution for each outcome (overlaid)."""
    by_outcome = defaultdict(list)
    for r in records:
        if r["outcome"] != "DEFERRAL":
            by_outcome[r["outcome"]].append(r["gap_us"])

    if not by_outcome:
        return None

    max_plot = 250.0
    bins = list(range(0, int(max_plot) + 5, 5))

    fig, ax = plt.subplots(figsize=(12, 5))
    for outcome in ["SUCCESS", "RTS_COLLISION", "RTS_CTS_TIMEOUT", "TIMING_EXPIRED"]:
        gaps = by_outcome.get(outcome, [])
        if not gaps:
            continue
        ax.hist([min(g, max_plot) for g in gaps], bins=bins,
                color=OUTCOME_COLORS[outcome], alpha=0.55,
                label=f"{outcome} (n={len(gaps)})", edgecolor="white", linewidth=0.3)
    ax.axvline(NAV_WINDOW_US, color="red", linestyle="--", linewidth=1.5,
               label=f"NAV window = {NAV_WINDOW_US:.0f} us")
    ax.axvline(DEADLINE_US, color="black", linestyle=":", linewidth=1.5,
               label=f"Deadline = {DEADLINE_US:.0f} us")
    ax.set_xlabel("Gap from DS-CTS end to TX start (us)", fontsize=11)
    ax.set_ylabel("Count", fontsize=11)
    ax.set_title("Gap Distribution by Outcome", fontsize=12, fontweight="bold")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3, linestyle="--")
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=200)
    plt.close(fig)
    return out_path


CW_MAX = 7  # Stage 2 CWmin = CWmax = 7, so drawn backoff is in [0, 7]


def categorize_attempt(gap_us):
    """
    Returns (drawn_slot, is_paused).
      - Pure backoff (no medium busy during countdown): gap = 34 + slot*9.
      - If gap > 97us, drawn slot was 7 (max) and timer was paused by medium busy.
      - If gap < 34us (DEFERRAL or noise), returns (-1, False).
    """
    if gap_us < AIFS_US - 0.5:
        return -1, False
    pure_slot = round((gap_us - AIFS_US) / SLOT_US)
    if 0 <= pure_slot <= CW_MAX:
        # Within the pure-backoff range; treat as drawn-slot
        return pure_slot, False
    elif pure_slot > CW_MAX:
        # Cap at CW_MAX; the extra duration is medium-busy pause
        return CW_MAX, True
    return -1, False


def plot_failure_rate_by_backoff(records, out_path):
    """
    Failure rate vs DRAWN backoff slot (capped at CWmax=7).
    Slot 7 is split into:
      - Pure slot 7 (gap = 97us, no pause)
      - Slot 7 + busy-pause stretch (97 < gap <= 200us)
    """
    # Stage 2 attempts within deadline (no DEFERRAL, no TIMING_EXPIRED)
    pure_by_slot = defaultdict(lambda: Counter())   # slot 0..7, pure backoff
    stretched = Counter()                            # slot 7 + paused

    for r in records:
        if r["outcome"] == "DEFERRAL":
            continue
        if r["gap_us"] > DEADLINE_US:
            continue
        slot, paused = categorize_attempt(r["gap_us"])
        if slot < 0:
            continue
        if paused:
            stretched[r["outcome"]] += 1
        else:
            pure_by_slot[slot][r["outcome"]] += 1

    if not pure_by_slot:
        return None

    # Build x labels: 0, 1, ..., 7, "7+pause"
    slot_keys = sorted(pure_by_slot.keys())
    labels = [str(s) for s in slot_keys]
    if stretched:
        labels.append("7+pause")
    x_pos = list(range(len(labels)))

    success = [pure_by_slot[s].get("SUCCESS", 0) for s in slot_keys]
    collision = [pure_by_slot[s].get("RTS_COLLISION", 0) for s in slot_keys]
    cts_to = [pure_by_slot[s].get("RTS_CTS_TIMEOUT", 0) for s in slot_keys]
    if stretched:
        success.append(stretched.get("SUCCESS", 0))
        collision.append(stretched.get("RTS_COLLISION", 0))
        cts_to.append(stretched.get("RTS_CTS_TIMEOUT", 0))

    totals = [success[i] + collision[i] + cts_to[i] for i in range(len(labels))]
    fail_rate = [(collision[i] + cts_to[i]) / totals[i] * 100.0 if totals[i] > 0 else 0
                 for i in range(len(labels))]
    coll_rate = [collision[i] / totals[i] * 100.0 if totals[i] > 0 else 0
                 for i in range(len(labels))]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))

    # Top: stacked bar of counts
    ax1.bar(x_pos, success, color=OUTCOME_COLORS["SUCCESS"], label="SUCCESS")
    bot = list(success)
    ax1.bar(x_pos, collision, bottom=bot,
            color=OUTCOME_COLORS["RTS_COLLISION"], label="RTS_COLLISION")
    bot = [bot[i] + collision[i] for i in range(len(labels))]
    ax1.bar(x_pos, cts_to, bottom=bot,
            color=OUTCOME_COLORS["RTS_CTS_TIMEOUT"], label="RTS_CTS_TIMEOUT")
    ax1.set_xlabel("Drawn Backoff Slot (capped at CWmax=7)", fontsize=11)
    ax1.set_ylabel("Count", fontsize=11)
    ax1.set_title("Outcome Counts by Drawn Backoff Slot",
                  fontsize=12, fontweight="bold")
    ax1.set_xticks(x_pos)
    ax1.set_xticklabels(labels)
    ax1.legend(loc="upper right")
    ax1.grid(True, axis="y", alpha=0.3, linestyle="--")
    for i in range(len(labels)):
        ax1.text(x_pos[i], totals[i] + 1, f"n={totals[i]}", ha="center", fontsize=8, color="gray")
    # NAV-window divider: between slot 4 (gap=70<=77) and slot 5 (gap=79>77)
    ax1.axvline(4.5, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    ax1.text(4.5, ax1.get_ylim()[1] * 0.95, " NAV protected ↔ unprotected",
             color="red", fontsize=9, va="top", ha="left")

    # Bottom: failure rate (%)
    ax2.plot(x_pos, fail_rate, marker="o", color="#d62728", linewidth=2,
             label="Total failure rate")
    ax2.plot(x_pos, coll_rate, marker="s", color=OUTCOME_COLORS["RTS_COLLISION"],
             linewidth=1.5, label="RTS collision")
    ax2.axvline(4.5, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    ax2.set_xlabel("Drawn Backoff Slot (capped at CWmax=7)", fontsize=11)
    ax2.set_ylabel("Failure Rate (%)", fontsize=11)
    ax2.set_title("Failure Rate vs Drawn Backoff Slot", fontsize=12, fontweight="bold")
    ax2.set_xticks(x_pos)
    ax2.set_xticklabels(labels)
    ax2.set_ylim(bottom=0)
    ax2.legend(loc="upper left")
    ax2.grid(True, alpha=0.3, linestyle="--")

    fig.tight_layout()
    fig.savefig(str(out_path), dpi=200)
    plt.close(fig)

    # Return both pure_by_slot and stretched for the summary text
    return out_path, pure_by_slot, stretched


def plot_outcome_pie(records, out_path):
    counter = Counter(r["outcome"] for r in records)
    if not counter:
        return None

    labels, sizes, colors = [], [], []
    for outcome in ["SUCCESS", "RTS_COLLISION", "RTS_CTS_TIMEOUT", "TIMING_EXPIRED", "DEFERRAL"]:
        if counter.get(outcome, 0) > 0:
            labels.append(f"{outcome}\n(n={counter[outcome]})")
            sizes.append(counter[outcome])
            colors.append(OUTCOME_COLORS[outcome])

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.pie(sizes, labels=labels, colors=colors, autopct="%1.1f%%",
           startangle=90, textprops={"fontsize": 10})
    ax.set_title(f"P-EDCA Outcome Breakdown (Total = {sum(counter.values())} attempts)",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=200)
    plt.close(fig)
    return out_path


# ─────────── Reporting ───────────

def write_summary(records, by_slot, stretched, out_path):
    counter = Counter(r["outcome"] for r in records)
    total = sum(counter.values())

    with open(out_path, "w") as f:
        f.write("=" * 70 + "\n")
        f.write(" P-EDCA Backoff vs Failure Analysis (nSta=20, nPedca=1)\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Total P-EDCA Stage 2 attempts (incl. deferrals): {total}\n\n")

        f.write("--- Outcome Breakdown ---\n")
        for outcome in ["SUCCESS", "RTS_COLLISION", "RTS_CTS_TIMEOUT",
                        "TIMING_EXPIRED", "DEFERRAL"]:
            c = counter.get(outcome, 0)
            pct = (c / total * 100.0) if total else 0
            f.write(f"  {outcome:18s}: {c:5d}  ({pct:5.2f} %)\n")
        f.write("\n")

        # Gap stats (excluding deferral)
        gaps = [r["gap_us"] for r in records if r["outcome"] != "DEFERRAL"]
        if gaps:
            gaps_sorted = sorted(gaps)
            f.write("--- Gap (us) Statistics ---\n")
            f.write(f"  count:  {len(gaps)}\n")
            f.write(f"  min:    {gaps_sorted[0]:.1f}\n")
            f.write(f"  median: {gaps_sorted[len(gaps_sorted) // 2]:.1f}\n")
            f.write(f"  mean:   {sum(gaps)/len(gaps):.1f}\n")
            f.write(f"  P95:    {gaps_sorted[int(len(gaps_sorted)*0.95)]:.1f}\n")
            f.write(f"  max:    {gaps_sorted[-1]:.1f}\n")
            n_within_nav = sum(1 for g in gaps if g <= NAV_WINDOW_US)
            n_unprotected = sum(1 for g in gaps if NAV_WINDOW_US < g <= DEADLINE_US)
            n_stale = sum(1 for g in gaps if g > DEADLINE_US)
            f.write(f"\n")
            f.write(f"  Within NAV (<= {NAV_WINDOW_US:.0f} us):       "
                    f"{n_within_nav:5d}  ({n_within_nav/len(gaps)*100:5.2f} %)\n")
            f.write(f"  Unprotected ({NAV_WINDOW_US:.0f}-{DEADLINE_US:.0f} us): "
                    f"{n_unprotected:5d}  ({n_unprotected/len(gaps)*100:5.2f} %)\n")
            f.write(f"  Stale (> {DEADLINE_US:.0f} us):           "
                    f"{n_stale:5d}  ({n_stale/len(gaps)*100:5.2f} %)\n")
            f.write("\n")

        # Per-drawn-slot table (capped at CWmax=7)
        if by_slot:
            f.write("--- Failure Rate by Drawn Backoff Slot (CWmax=7) ---\n")
            f.write(f"  {'slot':>10s} {'gap_us':>8s} {'total':>6s} {'succ':>6s} "
                    f"{'coll':>6s} {'cts_to':>6s} {'fail_rate':>10s}\n")
            for s in sorted(by_slot.keys()):
                c = by_slot[s]
                tot = c.total()
                gap_est = AIFS_US + s * SLOT_US
                fr = ((c.get("RTS_COLLISION", 0) + c.get("RTS_CTS_TIMEOUT", 0))
                      / tot * 100.0) if tot else 0
                tag = " (within NAV)" if gap_est <= NAV_WINDOW_US else " (UNPROTECTED)"
                f.write(f"  {s:>10d} {gap_est:>8.0f} {tot:>6d} "
                        f"{c.get('SUCCESS', 0):>6d} {c.get('RTS_COLLISION', 0):>6d} "
                        f"{c.get('RTS_CTS_TIMEOUT', 0):>6d} {fr:>9.2f}%{tag}\n")
            if stretched:
                tot = stretched.total()
                fr = ((stretched.get("RTS_COLLISION", 0) + stretched.get("RTS_CTS_TIMEOUT", 0))
                      / tot * 100.0) if tot else 0
                f.write(f"  {'7+pause':>10s} {'>97':>8s} {tot:>6d} "
                        f"{stretched.get('SUCCESS', 0):>6d} {stretched.get('RTS_COLLISION', 0):>6d} "
                        f"{stretched.get('RTS_CTS_TIMEOUT', 0):>6d} {fr:>9.2f}%"
                        " (slot=7 + medium-busy stretched countdown)\n")
            f.write("\n")

        # Within-NAV vs Unprotected failure rates
        within_nav = [r for r in records if r["outcome"] != "DEFERRAL"
                      and r["gap_us"] <= NAV_WINDOW_US]
        unprot = [r for r in records if r["outcome"] != "DEFERRAL"
                  and NAV_WINDOW_US < r["gap_us"] <= DEADLINE_US]
        f.write("--- Failure Rate: NAV-protected vs Unprotected ---\n")
        for label, group in [("Within NAV (<=77us)", within_nav),
                             ("Unprotected (77-200us)", unprot)]:
            if not group:
                continue
            tot = len(group)
            fail = sum(1 for r in group
                       if r["outcome"] in ("RTS_COLLISION", "RTS_CTS_TIMEOUT"))
            f.write(f"  {label:25s}: {fail}/{tot} = {fail/tot*100:.2f} % failure\n")
        f.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default="backoff_log_run*.csv",
                        help="Glob pattern relative to script dir")
    args = parser.parse_args()

    csv_paths = sorted(OUT_DIR.glob(args.pattern))
    if not csv_paths:
        print(f"No CSVs matching {args.pattern} in {OUT_DIR}")
        return

    print(f"Loading {len(csv_paths)} files...")
    records = load_records(csv_paths)
    print(f"  {len(records)} total attempt records")

    if not records:
        print("No records to analyze.")
        return

    print(f"\nGenerating plots in {OUT_DIR}...")

    p = plot_gap_histogram(records, OUT_DIR / "01_gap_histogram.pdf")
    if p: print(f"  ✔ {p.name}")
    p = plot_gap_by_outcome(records, OUT_DIR / "02_gap_by_outcome.pdf")
    if p: print(f"  ✔ {p.name}")
    res = plot_failure_rate_by_backoff(records, OUT_DIR / "03_failure_rate_by_backoff.pdf")
    by_slot = res[1] if res else {}
    stretched = res[2] if res else Counter()
    if res: print(f"  ✔ {res[0].name}")
    p = plot_outcome_pie(records, OUT_DIR / "04_outcome_pie.pdf")
    if p: print(f"  ✔ {p.name}")

    summary_path = OUT_DIR / "summary.txt"
    write_summary(records, by_slot, stretched, summary_path)
    print(f"  ✔ {summary_path.name}")

    # Echo summary to console
    print()
    print(summary_path.read_text())


if __name__ == "__main__":
    main()
