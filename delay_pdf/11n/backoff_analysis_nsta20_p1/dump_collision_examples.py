#!/usr/bin/env python3
"""Dump full ±60us context around a few RTS_COLLISION events to manually inspect."""
import csv
import bisect
from pathlib import Path

OUT_DIR = Path(__file__).parent
WINDOW = 200.0  # ±us


def load_csv(path):
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def dump(run_idx, n_examples_per_category=5):
    blog = load_csv(OUT_DIR / f"backoff_log_run{run_idx}.csv")
    txev = load_csv(OUT_DIR / f"tx_events_run{run_idx}.csv")
    tx_keys = sorted(float(t["time_us"]) for t in txev)
    tx_data = sorted(txev, key=lambda r: float(r["time_us"]))

    # Categorize collisions: slot 7 pure, slot 7+pause, slot 2-6
    cats = {"slot7_pure": [], "slot7_pause": [], "slot2-6": [], "slot7_success": []}
    for r in blog:
        outcome = r["outcome"]
        gap = float(r["gap_us"])
        slot_pure = round((gap - 34.0) / 9.0)
        if outcome == "RTS_COLLISION":
            if slot_pure == 7:
                cats["slot7_pure"].append(r)
            elif slot_pure > 7:
                cats["slot7_pause"].append(r)
            elif 2 <= slot_pure <= 6:
                cats["slot2-6"].append(r)
        elif outcome == "SUCCESS" and slot_pure == 7:
            cats["slot7_success"].append(r)

    for label, recs in cats.items():
        if not recs:
            continue
        print(f"\n{'='*78}\n  CATEGORY: {label}  (total {len(recs)} in run{run_idx})")
        print(f"{'='*78}")
        for r in recs[:n_examples_per_category]:
            sta = int(r["sta_id"])
            ds_end = float(r["ds_cts_end_us"])
            gap = float(r["gap_us"])
            rts_start = ds_end + gap
            print(f"\n  STA{sta} DS-CTS_end={ds_end:.1f}us  gap={gap:.1f}us  "
                  f"RTS_start={rts_start:.1f}us")
            print(f"  ↓ Surrounding TX events (±{WINDOW:.0f}us, all nodes):")
            lo, hi = rts_start - WINDOW, rts_start + WINDOW
            lo_i = bisect.bisect_left(tx_keys, lo)
            hi_i = bisect.bisect_right(tx_keys, hi)
            for i in range(lo_i, hi_i):
                e = tx_data[i]
                t = float(e["time_us"])
                rel = t - rts_start
                tag = "  ← OUR RTS" if (e["node_id"] == str(sta) and abs(rel) < 0.5) else ""
                role = "AP" if e["node_id"] == "20" else f"STA{e['node_id']}"
                print(f"    t={t:>10.1f} (rel {rel:+7.1f}us)  {role:>6s}  "
                      f"{e['frame_type']:14s} sz={e['size_bytes']:>4s}{tag}")


if __name__ == "__main__":
    dump(2, n_examples_per_category=4)
