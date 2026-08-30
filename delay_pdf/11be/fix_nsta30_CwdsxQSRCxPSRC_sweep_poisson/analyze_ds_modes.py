#!/usr/bin/env python3
"""
Mono-DS vs Dual-DS Analysis — Best Parameters, Heatmaps, and CDF Comparison
===========================================================================
Post-processes the two CWds×QSRC×PSRC sweeps produced by sweep_pedca_count.py:

    mono-DS/   (--dscts-repeat 1, single DS-CTS per Stage-1 attempt)
    dual-DS/   (--dscts-repeat 2, dual   DS-CTS per Stage-1 attempt)

For EACH mode it produces:
  - a ranked best-parameter report (txt + csv)
  - heatmaps over the QSRC x PSRC grid, one panel per (nPedca, CWds)

Then it overlays the two winning parameter sets against the EDCA-only
baseline as P-EDCA STA delay CDFs.

Scoring
-------
Delay percentiles only describe packets that were actually DELIVERED, so a
combo that drops more packets can look artificially fast. The primary score is
therefore the VoIP-style on-time delivery ratio

    R(D) = (1 - loss) * P(delay <= D | delivered)

at a deadline D (default 10 ms), aggregated over nPedca in {5,15,30}. The
P-EDCA P99 delay is reported alongside as the secondary (tail) metric.

Usage:
  python3 analyze_ds_modes.py                 # everything
  python3 analyze_ds_modes.py --deadline-ms 20
"""

import argparse
import csv
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# ══════════════════════════════════════════════════════════════════════
ROOT       = Path(__file__).resolve().parent
MODES      = ["mono-DS", "dual-DS"]
MODE_LABEL = {"mono-DS": "Mono-DS (1x DS-CTS)", "dual-DS": "Dual-DS (2x DS-CTS)"}
MODE_COLOR = {"mono-DS": "#4C72B0", "dual-DS": "#C44E52"}
BASE_COLOR = "#555555"

DATA_RATE = "1Mbps"
N_STA     = 30
NPEDCAS   = [5, 15, 30]
CWDS      = [0, 1]
QSRCS     = list(range(6))
PSRCS     = [1, 2, 3]
# ══════════════════════════════════════════════════════════════════════


def combo_tag(c, q, s):
    return f"c{c:d}_q{q:02d}_s{s:02d}"


# ─────────────────────────── Parsing ─────────────────────────────────

def parse_stats_txt(path):
    """-> {n_pedca: {metric: value}} from a per-combo statistics txt."""
    out = {}
    if not path.exists():
        return out
    cur = None
    in_ac = None
    for line in path.read_text().splitlines():
        s = line.strip()
        m = re.match(r"P-EDCA STAs\s*=\s*(\d+)/\d+", s)
        if m:
            cur = int(m.group(1))
            out[cur] = {}
            in_ac = None
            continue
        if cur is None:
            continue
        d = out[cur]
        if s.startswith("AC_") and s.endswith(":"):
            in_ac = s.rstrip(":")
            continue
        if s.startswith("---") or s.startswith("==="):
            in_ac = None
        if in_ac == "AC_VO":
            if s.startswith("Packet Loss:"):
                mm = re.search(r"([\d.]+)\s*%", s)
                if mm:
                    d["vo_loss_pct"] = float(mm.group(1))
                continue
            if s.startswith("Throughput:"):
                mm = re.search(r"([\d.eE+-]+)\s*Mbps", s)
                if mm:
                    d["vo_thpt_mbps"] = float(mm.group(1))
                continue
        for key, name in [
            ("PEDCA_STA_SUCC_COUNT",     "pedca_succ"),
            ("PEDCA_STA_FAIL_COUNT",     "pedca_fail"),
            ("LEGACY_STA_SUCC_COUNT",    "legacy_succ"),
            ("LEGACY_STA_FAIL_COUNT",    "legacy_fail"),
            ("PEDCA_STA_AVG_MAC_DELAY",  "pedca_avg_mac"),
            ("LEGACY_STA_AVG_MAC_DELAY", "legacy_avg_mac"),
            # Stage-1 / Stage-2 funnel counters
            ("DS-CTS Sent",                 "dscts_sent"),
            ("Stage 2 Entered",             "s2_enter"),
            ("Stage 2 TX Started",          "s2_tx"),
            ("P-EDCA TX Success",           "p_succ"),
            ("P-EDCA Fail RTS No CTS",      "f_nocts"),
            ("P-EDCA Fail RTS Collision",   "f_coll"),
            ("P-EDCA Fail Timing Expired",  "f_timing"),
            ("P-EDCA Fail Deferral",        "f_defer"),
        ]:
            if s.startswith(key + ":"):
                try:
                    d[name] = float(s.split(":")[1].strip())
                except ValueError:
                    pass
                break
        if s.startswith("Channel Idle Time (AP):"):
            mm = re.search(r"([\d.]+)\s*%", s)
            if mm:
                d["idle_pct"] = float(mm.group(1))
        elif s.startswith("Global P-EDCA Attempt (DS-CTS Sent):"):
            try:
                d["dscts_sent"] = float(s.split(":")[1].strip())
            except ValueError:
                pass
    return out


def load_pctls(mode):
    """-> {(c,q,s,nPedca,delay_type): {...}} from the sweep's summary CSV."""
    path = ROOT / mode / f"combo_percentile_summary_{DATA_RATE}.csv"
    out = {}
    if not path.exists():
        return out
    for r in csv.DictReader(path.open()):
        k = (int(r["CWds"]), int(r["QSRC"]), int(r["PSRC"]),
             int(r["nPedca"]), r["delay_type"])
        out[k] = {"P50": float(r["P50_us"]), "P95": float(r["P95_us"]),
                  "P99": float(r["P99_us"]),
                  "samples": int(r["samples"]) if r["samples"] else 0}
    return out


def load_cdf(path):
    """Read a delay histogram CSV -> (mids_us, cumulative_probability)."""
    if not path.exists():
        return None, None
    rows = []
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            rows.append((float(r["bin_start_us"]), float(r["bin_end_us"]),
                         float(r["probability"])))
    if not rows:
        return None, None
    rows.sort()
    mids, cum, run = [], [], 0.0
    for start, end, p in rows:
        run += p
        mids.append(0.5 * (start + end))
        cum.append(run)
    return mids, cum


def cdf_at(mids, cum, x_us):
    """P(delay <= x_us | delivered), from a cumulative curve."""
    if not mids:
        return float("nan")
    val = 0.0
    for m, c in zip(mids, cum):
        if m <= x_us:
            val = c
        else:
            break
    return val


# ───────────────────────── Collection ────────────────────────────────

def pedca_csv_path(mode, c, q, s, n):
    tag = combo_tag(c, q, s)
    return (ROOT / mode / tag /
            f"{tag}_p{n:02d}_pedca_sta_delay_pdf_nSta{N_STA}_{DATA_RATE}.csv")


def baseline_csv_path(mode):
    return (ROOT / mode / "edca_only" /
            f"edca_only_p00_legacy_sta_delay_pdf_nSta{N_STA}_{DATA_RATE}.csv")


def collect(mode, deadline_us):
    """-> (records, baseline) where records[(c,q,s,n)] = metric dict."""
    pct = load_pctls(mode)
    recs = {}
    for c in CWDS:
        for q in QSRCS:
            for s in PSRCS:
                tag = combo_tag(c, q, s)
                stats = parse_stats_txt(
                    ROOT / mode / tag /
                    f"pedca_count_sweep_statistics_{tag}_{DATA_RATE}.txt")
                for n in NPEDCAS:
                    d = dict(stats.get(n, {}))
                    for dt in ("pedca", "legacy", "vo"):
                        p = pct.get((c, q, s, n, dt))
                        if p:
                            d[f"{dt}_P50"] = p["P50"]
                            d[f"{dt}_P95"] = p["P95"]
                            d[f"{dt}_P99"] = p["P99"]
                    ps, pf = d.get("pedca_succ", 0.0), d.get("pedca_fail", 0.0)
                    ls, lf = d.get("legacy_succ", 0.0), d.get("legacy_fail", 0.0)
                    d["pedca_loss_pct"] = (100.0 * pf / (ps + pf)) if (ps + pf) else float("nan")
                    d["legacy_loss_pct"] = (100.0 * lf / (ls + lf)) if (ls + lf) else float("nan")
                    mids, cum = load_cdf(pedca_csv_path(mode, c, q, s, n))
                    d["pedca_ontime_pct"] = (
                        100.0 * (1.0 - d["pedca_loss_pct"] / 100.0) * cdf_at(mids, cum, deadline_us)
                        if mids and d["pedca_loss_pct"] == d["pedca_loss_pct"] else float("nan"))
                    recs[(c, q, s, n)] = d

    base = parse_stats_txt(ROOT / mode / "edca_only" /
                           f"pedca_count_sweep_statistics_edca_only_{DATA_RATE}.txt").get(0, {})
    bs, bf = base.get("legacy_succ", 0.0), base.get("legacy_fail", 0.0)
    base["loss_pct"] = (100.0 * bf / (bs + bf)) if (bs + bf) else float("nan")
    mids, cum = load_cdf(baseline_csv_path(mode))
    base["ontime_pct"] = (100.0 * (1.0 - base["loss_pct"] / 100.0)
                          * cdf_at(mids, cum, deadline_us)) if mids else float("nan")
    base["P50"] = _pct_from_cdf(mids, cum, 0.50)
    base["P95"] = _pct_from_cdf(mids, cum, 0.95)
    base["P99"] = _pct_from_cdf(mids, cum, 0.99)
    return recs, base


def _pct_from_cdf(mids, cum, target):
    if not mids:
        return float("nan")
    for m, c in zip(mids, cum):
        if c >= target:
            return m
    return mids[-1]


# ────────────────────────── Ranking ──────────────────────────────────

def rank_combos(recs, ns=None):
    """Score each (CWds,QSRC,PSRC) over the given nPedca values -> sorted list.

    ns=None averages over every nPedca (the single parameter set that works best
    across all loads). Passing a single-element list, e.g. ns=[5], ranks the
    combos for that load ALONE -- the best parameters legitimately differ per
    nPedca, so the per-load winner is what the comparison plots use.
    """
    ns = list(NPEDCAS) if ns is None else list(ns)
    # legacy STAs only exist while some STAs are not P-EDCA (nPedca < N_STA)
    leg_ns = [n for n in ns if n < N_STA] or None
    out = []
    for c in CWDS:
        for q in QSRCS:
            for s in PSRCS:
                per_n = {n: recs[(c, q, s, n)] for n in NPEDCAS}
                def mean(key, over=None):
                    over = ns if over is None else over
                    vals = [per_n[n].get(key, float("nan")) for n in over]
                    vals = [v for v in vals if v == v]
                    return sum(vals) / len(vals) if vals else float("nan")
                out.append({
                    "cwds": c, "qsrc": q, "psrc": s, "tag": combo_tag(c, q, s),
                    "ns": ns,
                    "ontime": mean("pedca_ontime_pct"),
                    "p99":    mean("pedca_P99"),
                    "p95":    mean("pedca_P95"),
                    "p50":    mean("pedca_P50"),
                    "loss":   mean("pedca_loss_pct"),
                    "leg_loss": mean("legacy_loss_pct", leg_ns) if leg_ns else float("nan"),
                    "leg_p99":  mean("legacy_P99", leg_ns) if leg_ns else float("nan"),
                    "per_n":  per_n,
                })
    out.sort(key=_sort_key)
    return out


# Selection criterion for "best parameters". "p99" picks the smallest P-EDCA STA
# P99 delay; "ontime" picks the highest R(D) = (1-loss) * P(delay <= D).
SELECT_BY = "p99"
SELECT_LABEL = {
    "p99":    "smallest P-EDCA STA P99 delay",
    "ontime": "highest P-EDCA STA on-time delivery R(D)",
}


def _sort_key(r):
    """Best-first ordering under the active criterion (NaN sinks to the end)."""
    if SELECT_BY == "ontime":
        v = r["ontime"]
        return (float("inf"),) if v != v else (-v,)
    v = r["p99"]
    return (float("inf"),) if v != v else (v,)


# ────────────────────────── Heatmaps ─────────────────────────────────

def metric_range(all_recs, metric):
    """Min/max of a metric across EVERY mode, so mono and dual heatmaps share
    one colour scale and can be compared cell-to-cell."""
    vals = [all_recs[m][(c, q, s, n)].get(metric, float("nan"))
            for m in all_recs
            for c in CWDS for q in QSRCS for s in PSRCS for n in NPEDCAS]
    vals = [v for v in vals if v == v]
    return (min(vals), max(vals)) if vals else (None, None)


def heatmap(recs, mode, metric, title, cbar_label, out_path,
            lower_is_better=True, fmt="{:.0f}", dpi=200,
            vmin=None, vmax=None):
    """QSRC x PSRC grid; rows = nPedca, cols = CWds.

    vmin/vmax are supplied by the caller so that the same metric uses an
    identical colour scale in every mode; without them the range is taken from
    this mode's data alone.
    """
    vals = [recs[(c, q, s, n)].get(metric, float("nan"))
            for c in CWDS for q in QSRCS for s in PSRCS for n in NPEDCAS]
    vals = [v for v in vals if v == v]
    if not vals:
        print(f"    (skip {out_path.name}: no data)")
        return
    if vmin is None or vmax is None:
        vmin, vmax = min(vals), max(vals)
    cmap = "RdYlGn_r" if lower_is_better else "RdYlGn"

    fig, axes = plt.subplots(len(NPEDCAS), len(CWDS),
                             figsize=(4.6 * len(CWDS), 3.4 * len(NPEDCAS)),
                             squeeze=False)
    im = None
    for ri, n in enumerate(NPEDCAS):
        for ci, c in enumerate(CWDS):
            ax = axes[ri][ci]
            grid = [[recs[(c, q, s, n)].get(metric, float("nan"))
                     for s in PSRCS] for q in QSRCS]
            im = ax.imshow(grid, cmap=cmap, vmin=vmin, vmax=vmax,
                           aspect="auto", origin="upper")
            flat = [(grid[qi][si], qi, si) for qi in range(len(QSRCS))
                    for si in range(len(PSRCS)) if grid[qi][si] == grid[qi][si]]
            if flat:
                best = (min if lower_is_better else max)(flat, key=lambda t: t[0])
                ax.add_patch(Rectangle((best[2] - .5, best[1] - .5), 1, 1,
                                       fill=False, edgecolor="black", lw=2.4))
            for qi in range(len(QSRCS)):
                for si in range(len(PSRCS)):
                    v = grid[qi][si]
                    if v != v:
                        continue
                    rel = (v - vmin) / (vmax - vmin) if vmax > vmin else .5
                    ax.text(si, qi, fmt.format(v), ha="center", va="center",
                            fontsize=8,
                            color="white" if (rel > .78 or rel < .22) else "black")
            ax.set_xticks(range(len(PSRCS)), [f"PSRC={s}" for s in PSRCS], fontsize=8)
            ax.set_yticks(range(len(QSRCS)), [f"QSRC={q}" for q in QSRCS], fontsize=8)
            ax.set_title(f"nPedca={n}/{N_STA}   CWds={c}", fontsize=10, pad=6)

    fig.suptitle(f"{title}\n{MODE_LABEL[mode]}   "
                 f"(nSta={N_STA}, {DATA_RATE}, black box = best in panel)\n"
                 f"colour scale {vmin:.0f}-{vmax:.0f} shared across "
                 f"{' & '.join(MODES)} — panels are directly comparable",
                 fontsize=11, y=0.995)
    fig.subplots_adjust(right=0.88, top=0.92, hspace=0.35, wspace=0.18)
    cax = fig.add_axes([0.90, 0.08, 0.022, 0.80])
    fig.colorbar(im, cax=cax).set_label(cbar_label, fontsize=9)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print(f"    -> {out_path.relative_to(ROOT)}")


# ─────────────────────────── Reports ─────────────────────────────────

def write_report(mode, ranked, base, deadline_us, out_dir, per_n_winners=None):
    csv_path = out_dir / f"best_params_{mode}_{DATA_RATE}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "CWds", "QSRC", "PSRC",
                    f"pedca_ontime_{deadline_us/1000:g}ms_pct",
                    "pedca_P50_us", "pedca_P95_us", "pedca_P99_us",
                    "pedca_loss_pct", "legacy_loss_pct", "legacy_P99_us"])
        for i, r in enumerate(ranked, 1):
            w.writerow([i, r["cwds"], r["qsrc"], r["psrc"],
                        f"{r['ontime']:.2f}", f"{r['p50']:.1f}",
                        f"{r['p95']:.1f}", f"{r['p99']:.1f}",
                        f"{r['loss']:.2f}", f"{r['leg_loss']:.2f}",
                        f"{r['leg_p99']:.1f}"])

    txt_path = out_dir / f"best_params_{mode}_{DATA_RATE}.txt"
    L = []
    L.append("=" * 96)
    L.append(f"  BEST PARAMETERS — {MODE_LABEL[mode]}")
    L.append(f"  nSta={N_STA}   dataRate={DATA_RATE}   nPedca in {NPEDCAS}   "
             f"deadline D={deadline_us/1000:g} ms")
    L.append("=" * 96)
    L.append("")
    L.append(f"  Ranked by: {SELECT_LABEL[SELECT_BY]}"
             + (" (averaged over nPedca in {5,15,30} for the overall list)."
                if True else ""))
    if SELECT_BY == "p99":
        L.append("  NOTE: P99 is conditioned on DELIVERED packets, so a combo that drops")
        L.append("  more packets loses its slowest samples and its P99 looks better than")
        L.append("  it is. Read the pLoss% column alongside; onTime% is shown for")
        L.append("  reference as the loss-aware alternative.")
    L.append("")
    L.append(f"  EDCA-only baseline (nPedca=0: all {N_STA} STAs are legacy EDCA, so")
    L.append(f"  there are NO P-EDCA STAs and PEDCA_STA_SUCC/FAIL_COUNT are both 0.")
    L.append(f"  The figures below are what an ORDINARY EDCA STA experiences, which is")
    L.append(f"  the reference a P-EDCA STA has to beat):")
    L.append(f"    on-time R(D) = {base.get('ontime_pct', float('nan')):.2f} %   "
             f"legacy-STA loss = {base.get('loss_pct', float('nan')):.2f} %   "
             f"P50/P95/P99 = {base.get('P50', 0):.0f} / {base.get('P95', 0):.0f} / "
             f"{base.get('P99', 0):.0f} us")
    L.append("")
    hdr = (f"{'#':>3}  {'CWds':>4} {'QSRC':>4} {'PSRC':>4}  {'onTime%':>8} "
           f"{'P50':>7} {'P95':>8} {'P99':>8}  {'pLoss%':>7}  {'legLoss%':>8} {'legP99':>8}")
    L.append(hdr)
    L.append("  " + "-" * (len(hdr) - 2))
    for i, r in enumerate(ranked, 1):
        mark = " <== BEST" if i == 1 else ""
        L.append(f"{i:>3}  {r['cwds']:>4} {r['qsrc']:>4} {r['psrc']:>4}  "
                 f"{r['ontime']:>8.2f} {r['p50']:>7.0f} {r['p95']:>8.0f} "
                 f"{r['p99']:>8.0f}  {r['loss']:>7.2f}  "
                 f"{r['leg_loss']:>8.2f} {r['leg_p99']:>8.0f}{mark}")

    b = ranked[0]
    L.append("")
    L.append("=" * 96)
    L.append(f"  OVERALL WINNER (single param set averaged over all loads):")
    L.append(f"  CWds={b['cwds']}  QSRC={b['qsrc']}  PSRC={b['psrc']}   ({b['tag']})")
    L.append("=" * 96)
    L.append(f"  vs EDCA-only baseline:  on-time "
             f"{base.get('ontime_pct', float('nan')):.2f}% -> {b['ontime']:.2f}%   "
             f"({b['ontime'] - base.get('ontime_pct', 0):+.2f} pts)")
    L.append(f"                          P99      {base.get('P99', 0):.0f} -> {b['p99']:.0f} us   "
             f"({b['p99'] - base.get('P99', 0):+.0f} us)")
    L.append(f"                          loss     {base.get('loss_pct', 0):.2f}% -> {b['loss']:.2f}%   "
             f"({b['loss'] - base.get('loss_pct', 0):+.2f} pts)")
    L.append("")
    L.append("  Per-nPedca breakdown of the winner:")
    L.append(f"    {'nPedca':>7}  {'onTime%':>8} {'P50':>7} {'P95':>8} {'P99':>8} "
             f"{'pLoss%':>8} {'legLoss%':>9} {'legP99':>8}")
    for n in NPEDCAS:
        d = b["per_n"][n]
        ll = d.get("legacy_loss_pct", float("nan"))
        lp = d.get("legacy_P99", float("nan"))
        L.append(f"    {n:>7}  {d.get('pedca_ontime_pct', float('nan')):>8.2f} "
                 f"{d.get('pedca_P50', float('nan')):>7.0f} "
                 f"{d.get('pedca_P95', float('nan')):>8.0f} "
                 f"{d.get('pedca_P99', float('nan')):>8.0f} "
                 f"{d.get('pedca_loss_pct', float('nan')):>8.2f} "
                 + (f"{ll:>9.2f} " if ll == ll else f"{'n/a':>9} ")
                 + (f"{lp:>8.0f}" if lp == lp else f"{'n/a':>8}"))
    if per_n_winners:
        L.append("")
        L.append("=" * 96)
        L.append("  BEST PARAMETERS CHOSEN INDEPENDENTLY FOR EACH nPedca")
        L.append("=" * 96)
        L.append("  The optimum genuinely moves with the P-EDCA population, so a single")
        L.append("  parameter set is not optimal at every load. These per-load winners are")
        L.append("  what the comparison CDFs in comparison/ plot.")
        L.append("")
        L.append(f"  {'nPedca':>7}  {'CWds':>4} {'QSRC':>4} {'PSRC':>4}  {'onTime%':>8} "
                 f"{'P50':>7} {'P95':>8} {'P99':>8}  {'pLoss%':>7}  {'legLoss%':>8}")
        L.append("  " + "-" * 80)
        for n in NPEDCAS:
            v = per_n_winners[n]
            leg = v["leg_loss"]
            L.append(f"  {n:>7}  {v['cwds']:>4} {v['qsrc']:>4} {v['psrc']:>4}  "
                     f"{v['ontime']:>8.2f} {v['p50']:>7.0f} {v['p95']:>8.0f} "
                     f"{v['p99']:>8.0f}  {v['loss']:>7.2f}  "
                     + (f"{leg:>8.2f}" if leg == leg else f"{'n/a':>8}"))

    L.append("")
    L.append("  Note: at nPedca=30 every STA is P-EDCA, so legacy columns are n/a.")
    L.append("")
    txt_path.write_text("\n".join(L) + "\n")
    print(f"    -> {txt_path.relative_to(ROOT)}")
    print(f"    -> {csv_path.relative_to(ROOT)}")
    return txt_path


# ───────────────────── Final CDF comparison ──────────────────────────

def _curves_for(winners_per_n, base_curve, base_loss, n):
    """-> list of (label, color, linestyle, mids, conditional_cum, delivery_ratio).

    winners_per_n[mode][n] is the best combo FOR THAT nPedca, so each panel shows
    the parameters actually tuned for its own load.
    """
    out = []
    bmids, bcum = base_curve
    if bmids:
        out.append((f"EDCA-only (all {N_STA} legacy STAs)", BASE_COLOR, "--",
                    bmids, bcum, 1.0 - base_loss / 100.0))
    for mode in MODES:
        w = winners_per_n[mode][n]
        mids, cum = load_cdf(pedca_csv_path(mode, w["cwds"], w["qsrc"],
                                            w["psrc"], n))
        if not mids:
            continue
        loss = w["per_n"][n].get("pedca_loss_pct", float("nan"))
        out.append((f"{MODE_LABEL[mode]}  best@nPedca={n}: CWds={w['cwds']}, "
                    f"QSRC={w['qsrc']}, PSRC={w['psrc']}",
                    MODE_COLOR[mode], "-", mids, cum,
                    1.0 - loss / 100.0 if loss == loss else 1.0))
    return out


def _cdf_x_at(mids, cum, target):
    """Smallest delay whose cumulative probability reaches `target`."""
    for m, c in zip(mids, cum):
        if c >= target:
            return m
    return mids[-1] if mids else 0.0


def _draw_cdf(ax, curves, deadline_us, unconditional, show_ceiling=True):
    for label, color, ls, mids, cum, deliv in curves:
        scale = deliv if unconditional else 1.0
        ax.plot(mids, [c * scale for c in cum], color=color, lw=2.3, ls=ls,
                zorder=3, label=(f"{label}   [delivered {deliv*100:.1f}%]"
                                 if unconditional else label))
        if unconditional and show_ceiling:
            ax.axhline(scale, color=color, lw=0.8, ls=":", alpha=0.55, zorder=1)
    ax.axvline(deadline_us, color="#888888", lw=1.0, ls=":", zorder=1)
    ax.text(deadline_us, 0.02, f" D={deadline_us/1000:g}ms", fontsize=8,
            color="#666666", rotation=90, va="bottom")
    # Scale the x-axis to the data: a fixed floor wastes most of the width at
    # light load (P99 ~ 6ms) and clips the tail at heavy load.
    tails = [_cdf_x_at(mids, cum, 0.995) for _, _, _, mids, cum, _ in curves if mids]
    xmax = max(tails) * 1.15 if tails else 15000.0
    ax.set_xlim(0, max(xmax, deadline_us * 1.1))
    ax.set_ylim(0, 1.02)
    ax.grid(alpha=0.25, lw=0.6)


def plot_comparison(winners_per_n, base_curve, base_loss, deadline_us, out_dir, dpi=200):
    """P-EDCA STA delay CDF: mono-best vs dual-best vs EDCA-only.

    Two views are written for each layout:
      unconditional — curve saturates at the delivered fraction, so packet loss
                      is visible as the gap below 1.0. This is the honest view.
      conditional   — normalised to 1.0, comparing delay SHAPE only.
    """
    ylab = {True:  "P(delivered AND delay <= x)",
            False: "CDF  P(delay <= x | delivered)"}
    note = {True:  "curves saturate at each configuration's delivered fraction "
                   "(dotted ceilings) — the gap below 1.0 is packet loss",
            False: "curves normalised to 1.0 — delay SHAPE only, loss not shown"}

    for uncond in (True, False):
        kind = "" if uncond else "_conditional"

        # ── multi-panel: one panel per nPedca ──
        fig, axes = plt.subplots(1, len(NPEDCAS),
                                 figsize=(6.0 * len(NPEDCAS), 5.4), squeeze=False)
        for ci, n in enumerate(NPEDCAS):
            ax = axes[0][ci]
            _draw_cdf(ax, _curves_for(winners_per_n, base_curve, base_loss, n),
                      deadline_us, uncond)
            ax.set_xlabel("P-EDCA STA VO delay (us)")
            if ci == 0:
                ax.set_ylabel(ylab[uncond])
            ax.set_title(f"nPedca = {n}/{N_STA}", fontsize=11)
            ax.legend(loc="lower right", fontsize=7.5, framealpha=0.92)
        fig.suptitle(
            "P-EDCA STA Delay CDF at Best Parameters — Mono-DS vs Dual-DS vs "
            f"EDCA-only\nnSta={N_STA}, {DATA_RATE}   ({note[uncond]})", fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, 0.90))
        out = out_dir / f"cdf_best_mono_vs_dual_vs_edca{kind}_{DATA_RATE}.pdf"
        fig.savefig(out, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        print(f"    -> {out.relative_to(ROOT)}")

        # ── headline single panel at the sparsest P-EDCA load ──
        n = NPEDCAS[0]
        fig, ax = plt.subplots(figsize=(9.6, 6.2))
        _draw_cdf(ax, _curves_for(winners_per_n, base_curve, base_loss, n),
                  deadline_us, uncond)
        for lvl in (0.5, 0.95, 0.99):
            ax.axhline(lvl, color="#dddddd", lw=0.7, zorder=0)
            ax.text(60, lvl + 0.008, f"P{int(lvl*100)}", fontsize=8, color="#aaaaaa")
        ax.set_xlabel("P-EDCA STA VO delay (us)")
        ax.set_ylabel(ylab[uncond])
        ax.set_title(f"P-EDCA STA Delay CDF at Best Parameters  "
                     f"(nPedca={n}/{N_STA}, nSta={N_STA}, {DATA_RATE})\n{note[uncond]}",
                     fontsize=11.5)
        ax.legend(loc="lower right", fontsize=9, framealpha=0.95)
        fig.tight_layout()
        out2 = (out_dir /
                f"cdf_best_mono_vs_dual_vs_edca_p{n:02d}{kind}_{DATA_RATE}.pdf")
        fig.savefig(out2, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        print(f"    -> {out2.relative_to(ROOT)}")


# ───────────── Delay-reduction & mechanism (funnel) report ───────────

DEFAULT_COMBO = (0, 2, 1)   # cwds/qsrc/psrc defaults in pedca_verification_nsta_11be.cc


def write_deltas_and_funnel(all_recs, winners, base, out_dir):
    """P95/P99 reduction vs default-param & vs EDCA-only, plus the Stage-1/2 funnel."""
    B95, B99 = base.get("P95", float("nan")), base.get("P99", float("nan"))
    L = ["=" * 108,
         "  P-EDCA STA DELAY REDUCTION  (negative % = improvement)",
         f"  reference A = same mode at DEFAULT param "
         f"CWds={DEFAULT_COMBO[0]}/QSRC={DEFAULT_COMBO[1]}/PSRC={DEFAULT_COMBO[2]}",
         f"  reference B = EDCA-only baseline  (P95={B95:.0f}us  P99={B99:.0f}us)",
         "=" * 108, "",
         "  CAUTION: percentiles are conditioned on DELIVERED packets. A config that",
         "  drops more packets loses its slowest samples, which flatters its P95/P99.",
         "  Always read these next to the loss column.", ""]
    hdr = (f"  {'mode':<9} {'param':<20} {'nPedca':>6} | {'P95':>7} {'vs dflt':>9} "
           f"{'vs EDCA':>9} | {'P99':>7} {'vs dflt':>9} {'vs EDCA':>9} | {'loss%':>7}")
    L += [hdr, "  " + "-" * (len(hdr) - 2)]
    L.insert(9, "  'best' is the winner FOR THAT nPedca, chosen independently per load,")
    L.insert(10, "  so the parameter set legitimately changes from row to row.")
    rows = []
    for mode in MODES:
        recs = all_recs[mode]
        for label in ("default", "best"):
            for n in NPEDCAS:
                if label == "default":
                    c, q, s = DEFAULT_COMBO
                else:
                    w = winners[mode][n]
                    c, q, s = w["cwds"], w["qsrc"], w["psrc"]
                d = recs[(c, q, s, n)]
                dd = recs[(*DEFAULT_COMBO, n)]
                p95, p99 = d.get("pedca_P95"), d.get("pedca_P99")
                d95, d99 = dd.get("pedca_P95"), dd.get("pedca_P99")
                if p95 is None or p99 is None:
                    continue
                r = dict(mode=mode, label=label, tag=combo_tag(c, q, s), n=n,
                         p95=p95, p99=p99,
                         v95=(p95 - d95) / d95 * 100, v99=(p99 - d99) / d99 * 100,
                         e95=(p95 - B95) / B95 * 100, e99=(p99 - B99) / B99 * 100,
                         loss=d.get("pedca_loss_pct", float("nan")))
                rows.append(r)
                L.append(f"  {mode:<9} {label + ' ' + r['tag']:<20} {n:>6} | "
                         f"{p95:>7.0f} {r['v95']:>8.1f}% {r['e95']:>8.1f}% | "
                         f"{p99:>7.0f} {r['v99']:>8.1f}% {r['e99']:>8.1f}% | "
                         f"{r['loss']:>7.2f}")
        L.append("")

    L += ["=" * 108,
          "  P-EDCA STAGE-1 / STAGE-2 FUNNEL   (counts per 10s sim, averaged over 10 runs)",
          "=" * 108, "",
          "  DS-CTS   = Stage-1 attempts (one per DS-CTS burst, not per frame)",
          "  S2 TX    = Stage-2 payload transmissions that actually STARTED",
          "  S2TX/DS  = fraction of Stage-1 attempts that converted into a Stage-2 TX",
          "  noCTS    = P-EDCA Fail RTS No CTS  (NAV/protection never established)",
          "", ]
    hdr = (f"  {'mode':<9} {'param':<20} {'nPedca':>6} {'DS-CTS':>8} {'S2enter':>8} "
           f"{'S2 TX':>8} {'S2TX/DS':>8} {'success':>8} {'succ/DS':>8} "
           f"{'noCTS':>7} {'noCTS/DS':>9} {'loss%':>7}")
    L += [hdr, "  " + "-" * (len(hdr) - 2)]
    for mode in MODES:
        recs = all_recs[mode]
        for label in ("default", "best"):
            for n in NPEDCAS:
                if label == "default":
                    c, q, s = DEFAULT_COMBO
                else:
                    w = winners[mode][n]
                    c, q, s = w["cwds"], w["qsrc"], w["psrc"]
                d = recs[(c, q, s, n)]
                ds = d.get("dscts_sent", 0.0) or 0.0
                L.append(f"  {mode:<9} {label + ' ' + combo_tag(c,q,s):<20} {n:>6} "
                         f"{ds:>8.0f} {d.get('s2_enter',0):>8.0f} "
                         f"{d.get('s2_tx',0):>8.0f} "
                         f"{(100*d.get('s2_tx',0)/ds if ds else 0):>7.1f}% "
                         f"{d.get('p_succ',0):>8.0f} "
                         f"{(100*d.get('p_succ',0)/ds if ds else 0):>7.1f}% "
                         f"{d.get('f_nocts',0):>7.0f} "
                         f"{(100*d.get('f_nocts',0)/ds if ds else 0):>8.1f}% "
                         f"{d.get('pedca_loss_pct', float('nan')):>7.2f}")
        L.append("")
    L += ["  NOTE: 'P-EDCA Fail Timing Expired' is 0 everywhere by construction — the",
          "  NAV-window-expired branch in qos-frame-exchange-manager.cc still counts the",
          "  attempt as a Stage-2 TX start and lets it continue, so that counter is inert.",
          "  Use the S2TX/DS conversion rate instead to see Stage-2 access failures.", ""]

    txt = out_dir / f"delay_reduction_and_funnel_{DATA_RATE}.txt"
    txt.write_text("\n".join(L) + "\n")
    print(f"    -> {txt.relative_to(ROOT)}")

    csv_path = out_dir / f"delay_reduction_{DATA_RATE}.csv"
    with csv_path.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["mode", "param_kind", "combo", "nPedca",
                     "P95_us", "P95_vs_default_pct", "P95_vs_edcaonly_pct",
                     "P99_us", "P99_vs_default_pct", "P99_vs_edcaonly_pct",
                     "pedca_loss_pct"])
        for r in rows:
            wr.writerow([r["mode"], r["label"], r["tag"], r["n"],
                         f"{r['p95']:.1f}", f"{r['v95']:.2f}", f"{r['e95']:.2f}",
                         f"{r['p99']:.1f}", f"{r['v99']:.2f}", f"{r['e99']:.2f}",
                         f"{r['loss']:.2f}"])
    print(f"    -> {csv_path.relative_to(ROOT)}")
    return "\n".join(L)


# ──────────────────────────── Main ───────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deadline-ms", type=float, default=10.0,
                    help="Deadline D for the on-time delivery score (default: 10 ms)")
    ap.add_argument("--select-by", choices=["p99", "ontime"], default="p99",
                    help="Criterion for picking the best parameters: 'p99' = smallest "
                         "P-EDCA STA P99 delay (default), 'ontime' = highest "
                         "loss-aware on-time delivery R(D).")
    ap.add_argument("--dpi", type=int, default=200)
    a = ap.parse_args()
    deadline_us = a.deadline_ms * 1000.0

    global SELECT_BY
    SELECT_BY = a.select_by
    print(f"  Selecting best parameters by: {SELECT_LABEL[SELECT_BY]}")

    # winners[mode]      -> best combo for EACH nPedca, chosen independently
    # overall[mode]       -> single best combo averaged over all nPedca
    winners, overall, bases, all_recs = {}, {}, {}, {}

    # Pass 1: load every mode first, so heatmap colour scales can span all of
    # them and mono/dual panels become directly comparable.
    for mode in MODES:
        recs, base = collect(mode, deadline_us)
        all_recs[mode] = recs
        bases[mode] = base
        ranked = rank_combos(recs)
        overall[mode] = ranked[0]
        winners[mode] = {n: rank_combos(recs, [n])[0] for n in NPEDCAS}
    clim = {m: metric_range(all_recs, m) for m in
            ("pedca_ontime_pct", "pedca_P99", "pedca_P95", "pedca_P50",
             "pedca_loss_pct", "legacy_loss_pct", "legacy_P99")}

    # Pass 2: reports and figures.
    for mode in MODES:
        print(f"\n{'='*70}\n  {MODE_LABEL[mode]}\n{'='*70}")
        out_dir = ROOT / mode / "analysis"
        out_dir.mkdir(parents=True, exist_ok=True)

        recs, base = all_recs[mode], bases[mode]
        ranked = rank_combos(recs)

        write_report(mode, ranked, base, deadline_us, out_dir,
                     per_n_winners=winners[mode])

        heatmap(recs, mode, "pedca_ontime_pct",
                f"P-EDCA STA On-Time Delivery R(D={a.deadline_ms:g}ms)",
                "on-time delivery (%)",
                out_dir / f"heatmap_pedca_ontime_{mode}_{DATA_RATE}.pdf",
                lower_is_better=False, fmt="{:.1f}", dpi=a.dpi,
                vmin=clim["pedca_ontime_pct"][0], vmax=clim["pedca_ontime_pct"][1])
        heatmap(recs, mode, "pedca_P99",
                "P-EDCA STA VO Delay — P99", "P99 delay (us)",
                out_dir / f"heatmap_pedca_P99_{mode}_{DATA_RATE}.pdf",
                lower_is_better=True, fmt="{:.0f}", dpi=a.dpi,
                vmin=clim["pedca_P99"][0], vmax=clim["pedca_P99"][1])
        heatmap(recs, mode, "pedca_P95",
                "P-EDCA STA VO Delay — P95", "P95 delay (us)",
                out_dir / f"heatmap_pedca_P95_{mode}_{DATA_RATE}.pdf",
                lower_is_better=True, fmt="{:.0f}", dpi=a.dpi,
                vmin=clim["pedca_P95"][0], vmax=clim["pedca_P95"][1])
        heatmap(recs, mode, "pedca_P50",
                "P-EDCA STA VO Delay — Median", "P50 delay (us)",
                out_dir / f"heatmap_pedca_P50_{mode}_{DATA_RATE}.pdf",
                lower_is_better=True, fmt="{:.0f}", dpi=a.dpi,
                vmin=clim["pedca_P50"][0], vmax=clim["pedca_P50"][1])
        heatmap(recs, mode, "pedca_loss_pct",
                "P-EDCA STA Packet Loss", "loss (%)",
                out_dir / f"heatmap_pedca_loss_{mode}_{DATA_RATE}.pdf",
                lower_is_better=True, fmt="{:.1f}", dpi=a.dpi,
                vmin=clim["pedca_loss_pct"][0], vmax=clim["pedca_loss_pct"][1])
        heatmap(recs, mode, "legacy_loss_pct",
                "Legacy STA Packet Loss (fairness cost)", "loss (%)",
                out_dir / f"heatmap_legacy_loss_{mode}_{DATA_RATE}.pdf",
                lower_is_better=True, fmt="{:.1f}", dpi=a.dpi,
                vmin=clim["legacy_loss_pct"][0], vmax=clim["legacy_loss_pct"][1])
        heatmap(recs, mode, "legacy_P99",
                "Legacy STA VO Delay — P99 (fairness cost)", "P99 delay (us)",
                out_dir / f"heatmap_legacy_P99_{mode}_{DATA_RATE}.pdf",
                lower_is_better=True, fmt="{:.0f}", dpi=a.dpi,
                vmin=clim["legacy_P99"][0], vmax=clim["legacy_P99"][1])

        w = ranked[0]
        print(f"    BEST overall (avg over nPedca): CWds={w['cwds']} "
              f"QSRC={w['qsrc']} PSRC={w['psrc']}   onTime={w['ontime']:.2f}%  "
              f"P99={w['p99']:.0f}us  loss={w['loss']:.2f}%")
        for n in NPEDCAS:
            v = winners[mode][n]
            print(f"    BEST @nPedca={n:<2}: CWds={v['cwds']} QSRC={v['qsrc']} "
                  f"PSRC={v['psrc']}   onTime={v['ontime']:.2f}%  "
                  f"P95={v['p95']:.0f}us  P99={v['p99']:.0f}us  loss={v['loss']:.2f}%")

    print(f"\n{'='*70}\n  Comparison: mono vs dual vs EDCA-only\n{'='*70}")
    cmp_dir = ROOT / "comparison"
    cmp_dir.mkdir(parents=True, exist_ok=True)
    plot_comparison(winners, load_cdf(baseline_csv_path(MODES[0])),
                    bases[MODES[0]].get("loss_pct", 0.0),
                    deadline_us, cmp_dir, dpi=a.dpi)

    # Side-by-side summary of the two winners against the baseline.
    lines = ["=" * 92,
             "  MONO-DS vs DUAL-DS — BEST PARAMETERS SIDE BY SIDE",
             f"  nSta={N_STA}  {DATA_RATE}  deadline D={a.deadline_ms:g} ms  "
             f"averaged over nPedca in {NPEDCAS}",
             "=" * 92, ""]
    b0 = bases[MODES[0]]
    lines.append(f"  {'':<26} {'onTime%':>9} {'P50':>8} {'P95':>9} {'P99':>9} "
                 f"{'loss%':>8} {'legLoss%':>9}")
    lines.append("  " + "-" * 88)
    lines.append(f"  {'EDCA-only baseline':<26} {b0.get('ontime_pct', 0):>9.2f} "
                 f"{b0.get('P50', 0):>8.0f} {b0.get('P95', 0):>9.0f} "
                 f"{b0.get('P99', 0):>9.0f} {b0.get('loss_pct', 0):>8.2f} "
                 f"{'n/a':>9}")
    lines.append("      ^ nPedca=0, so there are NO P-EDCA STAs: this row's loss%/delays")
    lines.append("        are those of an ORDINARY legacy EDCA STA (the reference to beat).")
    lines.append("        For the two rows below, loss% IS the P-EDCA STA loss.")
    for mode in MODES:
        w = overall[mode]
        name = f"{mode} c{w['cwds']}/q{w['qsrc']}/s{w['psrc']}"
        lines.append(f"  {name:<26} {w['ontime']:>9.2f} {w['p50']:>8.0f} "
                     f"{w['p95']:>9.0f} {w['p99']:>9.0f} {w['loss']:>8.2f} "
                     f"{w['leg_loss']:>9.2f}")
    lines += ["", "  ^ those two rows are the single best parameter set averaged over all",
              "    loads. The best parameters actually DIFFER per nPedca -- the per-load",
              "    winners below are what the comparison CDFs plot.", ""]

    lines += ["=" * 92,
              "  BEST PARAMETERS CHOSEN INDEPENDENTLY PER nPedca",
              "=" * 92, ""]
    lines.append(f"  {'nPedca':>7} {'mode':<9} {'best param':<14} {'onTime%':>9} "
                 f"{'P50':>7} {'P95':>8} {'P99':>8} {'loss%':>7} {'legLoss%':>9}")
    lines.append("  " + "-" * 88)
    for n in NPEDCAS:
        lines.append(f"  {n:>7} {'EDCA-only':<9} {'--':<14} "
                     f"{b0.get('ontime_pct', 0):>9.2f} {b0.get('P50', 0):>7.0f} "
                     f"{b0.get('P95', 0):>8.0f} {b0.get('P99', 0):>8.0f} "
                     f"{b0.get('loss_pct', 0):>7.2f} {'n/a':>9}")
        for mode in MODES:
            v = winners[mode][n]
            leg = v["leg_loss"]
            pname = f"c{v['cwds']}/q{v['qsrc']}/s{v['psrc']}"
            lines.append(f"  {'':>7} {mode:<9} {pname:<14} "
                         f"{v['ontime']:>9.2f} {v['p50']:>7.0f} {v['p95']:>8.0f} "
                         f"{v['p99']:>8.0f} {v['loss']:>7.2f} "
                         + (f"{leg:>9.2f}" if leg == leg else f"{'n/a':>9}"))
        lines.append("")
    out = cmp_dir / f"summary_mono_vs_dual_{DATA_RATE}.txt"
    out.write_text("\n".join(lines) + "\n")
    print(f"    -> {out.relative_to(ROOT)}")

    detail = write_deltas_and_funnel(all_recs, winners, bases[MODES[0]], cmp_dir)
    print("\n".join(lines))
    print()
    print(detail)


if __name__ == "__main__":
    main()
