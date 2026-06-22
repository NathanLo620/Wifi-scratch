#!/usr/bin/env python3
"""
Forensic trace of every RTS_COLLISION event for drawn backoff slot 5, 6, 7.
For each event, dump every PHY TX (any node, any frame) in the window
[ds_end - 80, ds_end + gap + 200] and label them against 802.11 expected behavior.

Decision rules used to label each frame:
  - "OUR DS-CTS"   : node 0 sending CTS, time ∈ [ds_end-50, ds_end]
  - "OUR RTS"      : node 0 sending RTS, time ≈ ds_end + gap_mac
  - "LEGAL AP CTS-to-our-RTS" : node 20 sends CTS at our_RTS_end+SIFS, RA = node 0
  - "LEGAL legacy frame" : starts at or before our DS-CTS (was already in flight)
  - "VIOLATION: legacy TX in NAV window" : legacy node TX inside [ds_end, ds_end+77]
  - "POSSIBLE NAV-EXPIRED RACE"          : legacy node TX in [ds_end+77, ds_end+111]
  - "POST-NAV legacy TX"                 : legacy node TX after ds_end+111 (post NAV+AIFS)
"""
import csv
import bisect
from collections import defaultdict, Counter
from pathlib import Path

OUT = Path(__file__).parent
NAV_DUR = 77.0
AIFS = 34.0
SLOT = 9.0
P_EDCA_NODE = 0
AP_NODE = 20
WARMUP_US = 1_000_000


def categorize(gap):
    if gap <= 99:
        s = round((gap - 34) / 9)
        if 0 <= s <= 7:
            return s, False
    n = round((gap - 97) / 9)
    return 7, True  # slot 7 + pause


def label_event(t_rel_to_ds_end, node, ft, our_rts_t, our_rts_dur_est=64):
    """Classify a TX event relative to the P-EDCA cycle."""
    abs_t = t_rel_to_ds_end
    if node == P_EDCA_NODE:
        if abs_t < 0 and ft == "CTS":
            return "OUR DS-CTS"
        if abs(abs_t - (our_rts_t)) < 5 and ft == "RTS":
            return "OUR Stage-2 RTS"
        if abs_t < 0 and ft == "RTS":
            return "our pre-DS-CTS RTS"
        if ft == "QOSDATA_TID6" and abs_t > our_rts_t:
            return "OUR DATA (post Stage-2)"
        return f"OUR {ft}"
    elif node == AP_NODE:
        # AP frames
        if ft == "CTS" and our_rts_t > 0 and abs_t > our_rts_t + 50 and abs_t < our_rts_t + 100:
            return "AP CTS responding to our RTS (legal)"
        if ft == "ACK":
            if 0 <= abs_t <= NAV_DUR:
                return "AP ACK to legacy DATA during NAV (legal: SIFS-bounded reply)"
            else:
                return f"AP ACK (rel +{abs_t:.0f}us)"
        if ft == "CTS":
            return f"AP CTS to some other RTS (rel +{abs_t:.0f}us)"
        return f"AP {ft}"
    else:
        # Legacy STA
        if abs_t < 0:
            return f"legacy STA{node} {ft} (started BEFORE DS-CTS)"
        if 0 <= abs_t <= NAV_DUR:
            return f"⚠ VIOLATION: legacy STA{node} {ft} during NAV [0,{NAV_DUR:.0f}us]"
        if NAV_DUR < abs_t <= NAV_DUR + AIFS:
            return f"⚠ POSSIBLE-VIOLATION: legacy STA{node} {ft} during AIFS-after-NAV [77,111us]"
        return f"legacy STA{node} {ft} post-AIFS"


def load_run(run):
    blog = OUT / f"backoff_log_run{run}.csv"
    tlog = OUT / f"tx_events_run{run}.csv"
    if not (blog.exists() and tlog.exists()):
        return None, None
    backoff = []
    with open(blog) as f:
        for r in csv.DictReader(f):
            try:
                ds = float(r["ds_cts_end_us"])
                if ds < WARMUP_US: continue
                backoff.append({"ds_end": ds, "gap": float(r["gap_us"]),
                               "outcome": r["outcome"].strip()})
            except: pass

    tx = []
    with open(tlog) as f:
        for r in csv.DictReader(f):
            try:
                tx.append((float(r["time_us"]), int(r["node_id"]),
                           r["frame_type"].strip()))
            except: pass
    tx.sort()
    return backoff, tx


def dump_event(run, rec, tx, tx_t, window_pre=80, window_post=200):
    ds_end = rec["ds_end"]
    gap = rec["gap"]
    slot, paused = categorize(gap)
    rts_t = gap   # relative to ds_end
    print(f"\n  ── run {run}  ds_end={ds_end:.0f}  slot={slot}{'+pause' if paused else ''}"
          f"  gap={gap:.0f}us  RTS at +{rts_t:.0f}us ──")

    lo, hi = ds_end - window_pre, ds_end + window_post
    lo_i = bisect.bisect_left(tx_t, lo)
    hi_i = bisect.bisect_right(tx_t, hi)

    if lo_i == hi_i:
        print("    (no PHY events captured in window)")
        return

    for i in range(lo_i, hi_i):
        t, n, ft = tx[i]
        rel = t - ds_end
        role = "OUR" if n == P_EDCA_NODE else ("AP" if n == AP_NODE else f"STA{n}")
        label = label_event(rel, n, ft, rts_t)
        marker = ""
        if "VIOLATION" in label: marker = " <<<"
        elif "OUR Stage-2 RTS" in label: marker = "  *** our RTS"
        elif "AP CTS responding" in label: marker = "  *** AP CTS"
        print(f"    rel{rel:+7.0f}us  {role:>7s}  {ft:>14s} : {label}{marker}")


def main():
    print("=" * 90)
    print("  Forensic trace of slot-5/6/7 RTS_COLLISION events")
    print("  Showing every PHY TX in [ds_end-80, ds_end+200] for each event")
    print("=" * 90)

    targets = {5: [], 6: [], 7: [], "7+pause": []}
    per_run = {}
    for run in range(1, 9):
        backoff, tx = load_run(run)
        if not backoff:
            continue
        tx_t = [e[0] for e in tx]
        per_run[run] = (backoff, tx, tx_t)
        for r in backoff:
            if r["outcome"] != "RTS_COLLISION":
                continue
            s, paused = categorize(r["gap"])
            key = "7+pause" if (s == 7 and paused) else s
            if key in targets:
                targets[key].append((run, r))

    total_collisions = sum(len(v) for v in targets.values())
    print(f"\nTotal RTS_COLLISION events in scope: {total_collisions}")
    for k, v in targets.items():
        print(f"  slot {k:>8}: {len(v):>3d} events")

    # Aggregate stats first
    print("\n" + "=" * 90)
    print("  Aggregate: what frame types appear in ±preamble window of each collision RTS?")
    print("=" * 90)

    for k, items in targets.items():
        if not items:
            continue
        violations_in_nav = 0
        violations_in_aifs = 0
        legacy_after_aifs = 0
        ap_ack_in_nav = 0
        ap_other = 0
        no_event = 0
        # also count: any "concurrent" TX in preamble overlap window (±4us of our RTS PHY-TX)
        # but here we use the MAC-level gap as our RTS time approximation
        for run, rec in items:
            _, tx, tx_t = per_run[run]
            ds_end = rec["ds_end"]
            gap = rec["gap"]
            # Look for any non-P-EDCA-node TX in [ds_end, ds_end + gap + 100]
            lo, hi = ds_end, ds_end + gap + 100
            lo_i = bisect.bisect_left(tx_t, lo)
            hi_i = bisect.bisect_right(tx_t, hi)
            saw_any = False
            for i in range(lo_i, hi_i):
                t, n, ft = tx[i]
                if n == P_EDCA_NODE:
                    continue
                saw_any = True
                rel = t - ds_end
                if n != AP_NODE:
                    if 0 <= rel <= NAV_DUR:
                        violations_in_nav += 1
                    elif NAV_DUR < rel <= NAV_DUR + AIFS:
                        violations_in_aifs += 1
                    else:
                        legacy_after_aifs += 1
                else:
                    if ft == "ACK" and 0 <= rel <= NAV_DUR:
                        ap_ack_in_nav += 1
                    else:
                        ap_other += 1
            if not saw_any:
                no_event += 1

        print(f"\n  slot {k}  (n={len(items)}):")
        print(f"    ⚠ legacy STA TX during NAV [0,77us]              : {violations_in_nav}")
        print(f"    ⚠ legacy STA TX during AIFS-after-NAV [77,111us] : {violations_in_aifs}")
        print(f"    legacy STA TX post-AIFS (>111us)                  : {legacy_after_aifs}")
        print(f"    AP ACK during NAV (legal SIFS reply)              : {ap_ack_in_nav}")
        print(f"    AP other TX                                       : {ap_other}")
        print(f"    no PHY event from others in window                : {no_event}")

    # Detailed per-event dump (limit per slot)
    print("\n" + "=" * 90)
    print("  Detailed per-event timeline (showing up to 5 examples per slot)")
    print("=" * 90)
    for k in [5, 6, 7, "7+pause"]:
        items = targets[k]
        if not items:
            continue
        print(f"\n── slot {k}: {len(items)} collisions (showing first {min(5, len(items))}) ──")
        for run, rec in items[:5]:
            _, tx, tx_t = per_run[run]
            dump_event(run, rec, tx, tx_t)


if __name__ == "__main__":
    main()
