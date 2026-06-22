#!/usr/bin/env python3
"""
Cleaner forensic trace for slot 5/6/7 RTS_COLLISION events.

Methodology:
  - Use backoff log gap_us as ground truth for "MAC intent" RTS start time.
  - Our Stage-2 RTS PHY occupies [ds_end+gap, ds_end+gap+64us]  (HtMcs0 RTS ≈ 64us).
  - AP's CTS reply would be at ds_end+gap+64+16us = ds_end+gap+80us.
  - Legacy fire boundary: ds_end + NAV(77) + AIFS_VO(34) = ds_end+111us.

Violations counted:
  V1) legacy TX inside [ds_end, ds_end+77] (= during DS-CTS NAV)
  V2) legacy TX inside [ds_end+77, ds_end+111] (during AIFS after NAV)
       Note: this is only a violation if their AIFS countdown started at NAV-end.
       If the legacy STA didn't decode DS-CTS, EIFS may apply instead. But the
       point of NAV protection is for them to honor it, so we still flag this.

Pre-DS-CTS in-flight frames (started BEFORE ds_end - 50) are excluded from
violation counts because they were legitimately in flight when DS-CTS arrived
and the sender couldn't have honored DS-CTS NAV.

We also distinguish three collision mechanisms:
  M1) Pre-existing legacy TX in flight: legacy TX started during DS-CTS period,
      didn't honor NAV (half-duplex).
  M2) Post-NAV legacy fire (>= 111us): legacy STA legally fires after NAV+AIFS,
      collides with our slot-7 RTS still on air.
  M3) Our Stage-2 RTS preamble corrupted at AP: hard to attribute from trace,
      inferred when AP never sends CTS reply.
"""
import csv
import bisect
from collections import Counter
from pathlib import Path

OUT = Path(__file__).parent
NAV_DUR = 77.0
AIFS = 34.0
SLOT = 9.0
P_EDCA_NODE = 0
AP_NODE = 20
WARMUP_US = 1_000_000
RTS_PHY_DUR = 64    # HtMcs0 RTS ≈ 36us preamble + 28us payload
CTS_PHY_DUR = 32    # HtMcs0 CTS shorter than RTS

# DS-CTS theoretical airtime: 20us long preamble + 24us payload (6Mbps) = 44us
DSCTS_DUR = 44


def categorize(gap):
    if gap <= 99:
        s = round((gap - 34) / 9)
        if 0 <= s <= 7:
            return s, False
    n = round((gap - 97) / 9)
    return 7, True


def load_run(run):
    blog = OUT / f"backoff_log_run{run}.csv"
    tlog = OUT / f"tx_events_run{run}.csv"
    if not (blog.exists() and tlog.exists()):
        return None, None, None
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
    tx_t = [e[0] for e in tx]
    return backoff, tx, tx_t


def classify_legacy_tx(rel_to_ds_end, ds_cts_period_us=DSCTS_DUR):
    """Return classification for a legacy STA TX seen at rel_to_ds_end."""
    # When did their TX start? Note: rel is the START time (PhyTxBegin).
    if rel_to_ds_end < -ds_cts_period_us - 30:
        return "pre-DSCTS-far"
    if -ds_cts_period_us - 30 <= rel_to_ds_end < 0:
        return "pre-DSCTS-inflight (was already TXing, didn't see DS-CTS)"
    if 0 <= rel_to_ds_end <= NAV_DUR:
        return "NAV-VIOLATION (legacy ignored DS-CTS NAV)"
    if NAV_DUR < rel_to_ds_end <= NAV_DUR + AIFS:
        return "AIFS-window (could be legit if EIFS applies)"
    return "post-AIFS legal (>=111us)"


def trace_all_collisions():
    # categorize counters
    by_slot = {5: Counter(), 6: Counter(), 7: Counter(), "7+pause": Counter()}
    counts = {5: 0, 6: 0, 7: 0, "7+pause": 0}
    # also: for each collision, mark whether AP CTS reply was seen
    ap_cts_seen = {5: 0, 6: 0, 7: 0, "7+pause": 0}

    per_run_data = {}
    for run in range(1, 9):
        backoff, tx, tx_t = load_run(run)
        if backoff is None: continue
        per_run_data[run] = (backoff, tx, tx_t)

        for r in backoff:
            if r["outcome"] != "RTS_COLLISION":
                continue
            s, paused = categorize(r["gap"])
            key = "7+pause" if (s == 7 and paused) else s
            if key not in by_slot:
                continue
            counts[key] += 1

            ds_end = r["ds_end"]
            gap = r["gap"]
            our_rts_start = ds_end + gap

            # Check if AP CTS reply was seen in [our_rts_start + RTS_DUR, our_rts_start + RTS_DUR + 100]
            lo, hi = our_rts_start + RTS_PHY_DUR - 5, our_rts_start + RTS_PHY_DUR + 50
            lo_i = bisect.bisect_left(tx_t, lo)
            hi_i = bisect.bisect_right(tx_t, hi)
            for i in range(lo_i, hi_i):
                t, n, ft = tx[i]
                if n == AP_NODE and ft == "CTS":
                    ap_cts_seen[key] += 1
                    break

            # Check legacy TXes in [ds_end - DSCTS_DUR - 30, ds_end + gap + RTS_DUR + 50]
            lo = ds_end - DSCTS_DUR - 30
            hi = ds_end + gap + RTS_PHY_DUR + 50
            lo_i = bisect.bisect_left(tx_t, lo)
            hi_i = bisect.bisect_right(tx_t, hi)
            seen_legacy_tx = False
            for i in range(lo_i, hi_i):
                t, n, ft = tx[i]
                if n in (P_EDCA_NODE, AP_NODE):
                    continue
                rel = t - ds_end
                cls = classify_legacy_tx(rel)
                by_slot[key][cls] += 1
                seen_legacy_tx = True
            if not seen_legacy_tx:
                by_slot[key]["no-legacy-TX-in-window"] += 1

    return counts, by_slot, ap_cts_seen, per_run_data


def main():
    print("=" * 90)
    print("  Slot-5/6/7 RTS_COLLISION breakdown (v2 — cleaner classification)")
    print("=" * 90)

    counts, by_slot, ap_cts_seen, per_run = trace_all_collisions()

    for key in [5, 6, 7, "7+pause"]:
        n = counts[key]
        if n == 0: continue
        ap_cts = ap_cts_seen[key]
        print(f"\n  ── slot {key}: {n} collisions ──")
        print(f"     AP CTS reply observed (= AP did receive our RTS): {ap_cts} / {n} ({ap_cts*100/n:.1f}%)")
        print(f"     Legacy TX categorization (counts may exceed n because multiple TXes per event):")
        total = sum(by_slot[key].values())
        for cls, c in by_slot[key].most_common():
            print(f"        {cls:>55s}: {c:>4d}  ({c*100/n:.1f}% of events)")

    # ── Specific trace examples ──
    print("\n" + "=" * 90)
    print("  Concrete examples of slot-5 collisions (n=2)")
    print("=" * 90)
    show_specific_examples(per_run, [5], max_examples=2)

    print("\n" + "=" * 90)
    print("  Concrete examples of slot-6 collisions (n=2)")
    print("=" * 90)
    show_specific_examples(per_run, [6], max_examples=2)

    print("\n" + "=" * 90)
    print("  Concrete examples of slot-7 collisions (showing 5)")
    print("=" * 90)
    show_specific_examples(per_run, [7], max_examples=5)


def show_specific_examples(per_run, slots, max_examples=3):
    shown = 0
    for run in sorted(per_run.keys()):
        backoff, tx, tx_t = per_run[run]
        for r in backoff:
            if r["outcome"] != "RTS_COLLISION": continue
            s, paused = categorize(r["gap"])
            if paused: continue
            if s not in slots: continue
            if shown >= max_examples: return
            shown += 1

            ds_end = r["ds_end"]
            gap = r["gap"]
            our_rts = ds_end + gap
            print(f"\n  ▸ run{run} ds_end={ds_end:.0f}us  slot={s}  gap={gap:.0f}us  RTS_start_rel=+{gap:.0f}us")
            print(f"    Our RTS PHY:  [+{gap:.0f}, +{gap+RTS_PHY_DUR:.0f}]us  (HtMcs0, 64us)")
            print(f"    Expected AP CTS reply at +{gap+RTS_PHY_DUR+16:.0f}us (if AP got RTS)")
            print(f"    NAV-from-DSCTS expires at +77us; Legacy fire boundary at +111us")
            print(f"    Events from [ds_end-80, ds_end+gap+150]:")

            lo = ds_end - 80
            hi = ds_end + gap + 150
            lo_i = bisect.bisect_left(tx_t, lo)
            hi_i = bisect.bisect_right(tx_t, hi)
            for i in range(lo_i, hi_i):
                t, n, ft = tx[i]
                rel = t - ds_end
                role = "OURS" if n == P_EDCA_NODE else ("AP" if n == AP_NODE else f"STA{n}")
                # Verdict
                if n == P_EDCA_NODE and ft == "CTS":
                    verdict = "= OUR DS-CTS (PHY START)"
                elif n == P_EDCA_NODE and ft == "RTS" and abs(rel - gap) < 5:
                    verdict = "= OUR Stage-2 RTS"
                elif n == P_EDCA_NODE and ft == "RTS":
                    verdict = f"= OUR RTS (unrelated, off by {rel-gap:+.0f}us)"
                elif n == AP_NODE and ft == "CTS" and abs(rel - (gap + RTS_PHY_DUR + 16)) < 30:
                    verdict = "= AP CTS reply to our RTS (LEGAL)"
                elif n == AP_NODE and ft == "ACK":
                    if 0 <= rel <= NAV_DUR:
                        verdict = "= AP ACK during NAV (SIFS-bounded reply to legacy DATA → standard allows)"
                    else:
                        verdict = "= AP ACK (outside NAV)"
                elif n == AP_NODE:
                    verdict = f"= AP {ft}"
                else:
                    cls = classify_legacy_tx(rel)
                    verdict = f"legacy {ft}: {cls}"
                print(f"      t={t:>10.0f}  rel{rel:+5.0f}us  {role:>7s}  {ft:<14s} {verdict}")


if __name__ == "__main__":
    main()
