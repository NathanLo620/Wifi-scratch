#!/usr/bin/env python3
"""
Best-P99 vs Default vs EDCA-only CDF comparison.

For each STA-type view (all / pedca / legacy) and each nPedca in {5,15,30}:
  - Find the parameter combo with the MINIMUM P99 delay
  - Overlay its CDF against the DEFAULT combo (CWds=0, QSRC=2, PSRC=1)
    and the EDCA-only baseline (nPedca=0)
  - Report P99 of each + gain of best-min vs default and vs EDCA-only

Outputs (in this directory):
  best_vs_default_cdf_<sta_type>_<data_rate>.pdf   (one multi-panel figure per type)
  best_vs_default_p99_gain_<data_rate>.csv         (numeric summary)
  best_vs_default_p99_gain_<data_rate>.txt         (human-readable table)
"""

import csv
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE      = Path(__file__).resolve().parent
N_STA     = 30
DATA_RATE = "1Mbps"
import sys as _sys  # optional CLI override: python3 gen_best_vs_default_cdf.py --data-rate 0.5Mbps
if "--data-rate" in _sys.argv:
    DATA_RATE = _sys.argv[_sys.argv.index("--data-rate") + 1]
N_PEDCA_LIST = [5, 15, 30]
CWDS_VALUES  = [0, 1]
QSRC_VALUES  = list(range(0, 6))
PSRC_VALUES  = [1, 2, 3]

# Default parameter combo
DEF_CWDS, DEF_QSRC, DEF_PSRC = 0, 2, 1
DEF_TAG = f"c{DEF_CWDS}_q{DEF_QSRC:02d}_s{DEF_PSRC:02d}"

STA_TYPES = [
    ("all",    "All STAs",     "vo_delay_pdf"),
    ("pedca",  "P-EDCA STAs",  "pedca_sta_delay_pdf"),
    ("legacy", "Legacy STAs",  "legacy_sta_delay_pdf"),
]


# ── Histogram + percentile helpers ─────────────────────────────────────

def load_histogram(csv_path: Path):
    rows = []
    with csv_path.open("r", newline="") as f:
        for row in csv.DictReader(f):
            rows.append((float(row["bin_start_us"]),
                         float(row["bin_end_us"]),
                         float(row["probability"])))
    if not rows:
        return None
    widths = sorted(e - s for s, e, _ in rows if e > s)
    bw = widths[len(widths) // 2]
    min_s = min(s for s, _, _ in rows)
    max_e = max(e for _, e, _ in rows)
    lu = {round(s / bw) * bw: p for s, e, p in rows}
    mids, probs = [], []
    cur, eps = min_s, bw * 1e-6
    while cur < max_e - eps:
        mids.append(cur + 0.5 * bw)
        probs.append(lu.get(round(cur / bw) * bw, 0.0))
        cur += bw
    return mids, probs


def percentiles(mids, probs, pcts=(0.5, 0.95, 0.99)):
    total = sum(probs)
    if total <= 0:
        return {p: None for p in pcts}
    out, run, ti = {}, 0.0, 0
    tgt = sorted(pcts)
    for m, p in zip(mids, probs):
        run += p
        while ti < len(tgt) and run / total >= tgt[ti]:
            out[tgt[ti]] = m
            ti += 1
        if ti >= len(tgt):
            break
    for pc in pcts:
        out.setdefault(pc, mids[-1] if mids else None)
    return out


def combo_csv(cwds, qsrc, psrc, n_pedca, suffix):
    tag = f"c{cwds}_q{qsrc:02d}_s{psrc:02d}"
    return BASE / tag / f"{tag}_p{n_pedca:02d}_{suffix}_nSta{N_STA}_{DATA_RATE}.csv"


def cdf_from_hist(mids, probs, xmax=None):
    tot = sum(probs)
    cx, cy, run = [], [], 0.0
    if tot <= 0:
        return cx, cy
    for m, p in zip(mids, probs):
        run += p
        if xmax is None or m <= xmax:
            cx.append(m)
            cy.append(run / tot)
    return cx, cy


def build_ticks(xmin, xmax, n=10):
    span = max(xmax - xmin, 1.0)
    raw = span / n
    exp = math.floor(math.log10(raw))
    frac = raw / 10**exp
    nice = 1 if frac <= 1 else (2 if frac <= 2 else (5 if frac <= 5 else 10))
    step = nice * 10**exp
    first = math.floor(xmin / step) * step
    t, ticks = first, []
    while t <= xmax + step * 0.01:
        ticks.append(round(t, 3))
        t += step
    return ticks


# ── Load EDCA-only baseline (nPedca=0, all-VO) ──────────────────────────

edca_csv = BASE / "edca_only" / f"edca_only_p00_vo_delay_pdf_nSta{N_STA}_{DATA_RATE}.csv"
edca_hist = load_histogram(edca_csv) if edca_csv.exists() else None
edca_pcts = percentiles(*edca_hist) if edca_hist else {}
EDCA_P99 = edca_pcts.get(0.99)


# ── Find min-P99 combo for each (sta_type, nPedca) ──────────────────────

summary_rows = []   # for CSV / txt

for sta_key, sta_label, suffix in STA_TYPES:
    # collect available panels for this STA type
    panels = []   # (n_pedca, best_combo_dict, best_hist, default_hist)
    for n_pedca in N_PEDCA_LIST:
        # legacy has no STAs when nPedca == N_STA
        if sta_key == "legacy" and n_pedca == N_STA:
            continue

        # scan all combos, compute P99, keep min
        best = None
        for cwds in CWDS_VALUES:
            for qsrc in QSRC_VALUES:
                for psrc in PSRC_VALUES:
                    p = combo_csv(cwds, qsrc, psrc, n_pedca, suffix)
                    if not p.exists():
                        continue
                    h = load_histogram(p)
                    if h is None:
                        continue
                    pc = percentiles(*h)
                    if pc[0.99] is None:
                        continue
                    cand = {"cwds": cwds, "qsrc": qsrc, "psrc": psrc,
                            "p50": pc[0.5], "p95": pc[0.95], "p99": pc[0.99],
                            "hist": h}
                    if best is None or cand["p99"] < best["p99"]:
                        best = cand

        if best is None:
            continue

        # default combo hist
        def_path = combo_csv(DEF_CWDS, DEF_QSRC, DEF_PSRC, n_pedca, suffix)
        def_hist = load_histogram(def_path) if def_path.exists() else None
        def_pcts = percentiles(*def_hist) if def_hist else {}

        panels.append({
            "n_pedca": n_pedca,
            "best": best,
            "def_hist": def_hist,
            "def_pcts": def_pcts,
        })

        # record summary row
        def_p99 = def_pcts.get(0.99)
        best_p99 = best["p99"]
        gain_def = ((def_p99 - best_p99) / def_p99 * 100) if def_p99 else None
        gain_edca = ((EDCA_P99 - best_p99) / EDCA_P99 * 100) if EDCA_P99 else None
        summary_rows.append({
            "sta_type": sta_key, "sta_label": sta_label, "n_pedca": n_pedca,
            "best_cwds": best["cwds"], "best_qsrc": best["qsrc"], "best_psrc": best["psrc"],
            "best_p99": best_p99, "best_p50": best["p50"], "best_p95": best["p95"],
            "def_p99": def_p99,
            "edca_p99": EDCA_P99,
            "gain_vs_def_pct": gain_def,
            "gain_vs_edca_pct": gain_edca,
        })

    # ── Plot a multi-panel figure for this STA type ──
    if not panels:
        continue
    n_panels = len(panels)
    fig, axes = plt.subplots(1, n_panels, figsize=(6.5 * n_panels, 6.0),
                             squeeze=False)
    axes = axes[0]

    for ax, panel in zip(axes, panels):
        n_pedca = panel["n_pedca"]
        best = panel["best"]
        def_hist = panel["def_hist"]

        # shared x-zoom to ~P99.5 of the widest curve
        series = [best["hist"]]
        if def_hist:
            series.append(def_hist)
        if edca_hist:
            series.append(edca_hist)
        xmax = 0.0
        for h in series:
            pc = percentiles(*h, pcts=(0.99,))
            if pc[0.99]:
                xmax = max(xmax, pc[0.99])
        xmax *= 1.15
        xmin = min(h[0][0] for h in series)

        # EDCA-only (grey dashed)
        if edca_hist:
            cx, cy = cdf_from_hist(*edca_hist, xmax=xmax)
            if cx:
                ax.plot(cx, cy, lw=1.5, color="#888888", ls="--",
                        label=f"EDCA only  (P99={EDCA_P99:.0f}µs)")

        # Default combo (orange)
        if def_hist:
            cx, cy = cdf_from_hist(*def_hist, xmax=xmax)
            dp99 = panel["def_pcts"].get(0.99)
            if cx:
                ax.plot(cx, cy, lw=1.8, color="#DD8452", ls="-",
                        label=f"Default c0·q2·s1  (P99={dp99:.0f}µs)")

        # Best-min-P99 combo (red, thick)
        cx, cy = cdf_from_hist(*best["hist"], xmax=xmax)
        ax.plot(cx, cy, lw=2.2, color="#C44E52", ls="-",
                label=f"Min-P99 c{best['cwds']}·q{best['qsrc']}·s{best['psrc']}  "
                      f"(P99={best['p99']:.0f}µs)")

        # P99 vertical markers
        for h, col in [(best["hist"], "#C44E52"),
                       (def_hist, "#DD8452"),
                       (edca_hist, "#888888")]:
            if h is None:
                continue
            xv = percentiles(*h, pcts=(0.99,))[0.99]
            if xv and xv <= xmax:
                ax.axvline(xv, color=col, lw=0.8, ls=":", alpha=0.6)

        ax.set_xlim(xmin, xmax)
        ax.set_xticks(build_ticks(xmin, xmax))
        ax.set_ylim(0, 1.02)
        ax.axhline(0.99, color="#444", lw=0.6, ls=":", alpha=0.5)
        ax.tick_params(axis="x", labelsize=8, rotation=45)
        ax.set_xlabel("Delay (µs)", fontsize=10)
        ax.set_ylabel("Cumulative Probability", fontsize=10)
        ax.set_title(f"nPedca={n_pedca}/{N_STA}", fontsize=12, fontweight="bold")
        ax.grid(True, alpha=0.25, ls="--")
        ax.legend(loc="lower right", fontsize=8)

    fig.suptitle(
        f"{sta_label} — Min-P99 vs Default (c0·q2·s1) vs EDCA-only   |   "
        f"nSta={N_STA}, {DATA_RATE}, 10 runs avg",
        fontsize=13, fontweight="bold"
    )
    fig.tight_layout()
    out = BASE / f"best_vs_default_cdf_{sta_key}_{DATA_RATE}.pdf"
    fig.savefig(str(out), dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  ✔ {out.name}")


# ── Write numeric summary (CSV + txt) ──────────────────────────────────

csv_out = BASE / f"best_vs_default_p99_gain_{DATA_RATE}.csv"
with open(csv_out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["sta_type", "nPedca",
                "min_p99_CWds", "min_p99_QSRC", "min_p99_PSRC",
                "min_P99_us", "default_P99_us", "EDCA_only_P99_us",
                "gain_vs_default_%", "gain_vs_EDCA_%",
                "min_P50_us", "min_P95_us"])
    for r in summary_rows:
        w.writerow([
            r["sta_label"], r["n_pedca"],
            r["best_cwds"], r["best_qsrc"], r["best_psrc"],
            f"{r['best_p99']:.1f}",
            f"{r['def_p99']:.1f}" if r["def_p99"] else "N/A",
            f"{r['edca_p99']:.1f}" if r["edca_p99"] else "N/A",
            f"{r['gain_vs_def_pct']:.1f}" if r["gain_vs_def_pct"] is not None else "N/A",
            f"{r['gain_vs_edca_pct']:.1f}" if r["gain_vs_edca_pct"] is not None else "N/A",
            f"{r['best_p50']:.1f}", f"{r['best_p95']:.1f}",
        ])
print(f"  ✔ {csv_out.name}")

# Human-readable
txt_out = BASE / f"best_vs_default_p99_gain_{DATA_RATE}.txt"
lines = []
lines.append("=" * 100)
lines.append(f"  P99 Delay: Min-P99 combo vs Default (CWds=0,QSRC=2,PSRC=1) vs EDCA-only")
lines.append(f"  nSta={N_STA}  dataRate={DATA_RATE}  10 runs averaged")
lines.append(f"  EDCA-only baseline P99 = {EDCA_P99:.0f} µs")
lines.append("=" * 100)
lines.append("")
hdr = (f"  {'STA Type':12}{'nPedca':>7}  {'Best Params':14}"
       f"{'Min P99':>10}{'Default P99':>12}{'EDCA P99':>10}"
       f"{'Gain vs Def':>13}{'Gain vs EDCA':>14}")
lines.append(hdr)
lines.append("  " + "-" * (len(hdr) - 2))
cur_type = None
for r in summary_rows:
    if r["sta_label"] != cur_type:
        cur_type = r["sta_label"]
    params = f"c{r['best_cwds']}·q{r['best_qsrc']}·s{r['best_psrc']}"
    dp = f"{r['def_p99']:.0f}" if r["def_p99"] else "N/A"
    gd = f"{r['gain_vs_def_pct']:+.1f}%" if r["gain_vs_def_pct"] is not None else "N/A"
    ge = f"{r['gain_vs_edca_pct']:+.1f}%" if r["gain_vs_edca_pct"] is not None else "N/A"
    lines.append(
        f"  {r['sta_label']:12}{r['n_pedca']:>7}  {params:14}"
        f"{r['best_p99']:>10.0f}{dp:>12}{EDCA_P99:>10.0f}"
        f"{gd:>13}{ge:>14}"
    )
lines.append("")
lines.append("  Gain = (reference_P99 - min_P99) / reference_P99 × 100  (positive = min combo is better)")
lines.append("  Note: 'gain vs Def' positive means the min-P99 combo beats the default parameter set.")
txt = "\n".join(lines) + "\n"
txt_out.write_text(txt)
print(f"  ✔ {txt_out.name}")
print()
print(txt)
