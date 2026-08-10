#!/usr/bin/env python3
"""
Cross-Traffic-Model Comparison — lightload / MMPP / onoff / poisson
===================================================================
For every traffic model, compares five configurations of the P-EDCA STA
VO delay distribution:

    EDCA-only      nPedca=0, all 30 STAs legacy (the reference)
    mono default   1x DS-CTS at CWds=0/QSRC=2/PSRC=1 (the .cc defaults)
    mono best      1x DS-CTS at the combo with the smallest P-EDCA STA P99
    dual default   2x DS-CTS at the .cc defaults
    dual best      2x DS-CTS at the combo with the smallest P-EDCA STA P99

and reports P50 / P95 / P99, then the reduction of "dual best" against
EDCA-only. Best parameters are chosen independently for each nPedca.

Percentiles are conditioned on DELIVERED packets, so the packet-loss column
is printed next to them: a config that drops more packets loses its slowest
samples and its percentiles look better than they are.

Usage:
  python3 compare_traffic_models.py
  python3 compare_traffic_models.py --select-by ontime --deadline-ms 10
"""

import argparse
import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "traffic_model_comparison"

# model key -> (sweep directory name, data rate, pretty label)
ALL_MODELS = [
    ("cbr1M",     "fix_nsta30_CwdsxQSRCxPSRC_sweep",           "1Mbps",   "CBR 1Mbps (full load)"),
    ("lightload", "fix_nsta30_CwdsxQSRCxPSRC_sweep_lightload", "0.5Mbps", "CBR 0.5Mbps (light load)"),
    ("MMPP",      "fix_nsta30_CwdsxQSRCxPSRC_sweep_MMPP",      "1Mbps",   "MMPP (bursty)"),
    ("onoff",     "fix_nsta30_CwdsxQSRCxPSRC_sweep_onoff",     "1Mbps",   "ON/OFF"),
    ("poisson",   "fix_nsta30_CwdsxQSRCxPSRC_sweep_poisson",   "1Mbps",   "Poisson"),
]
MODEL_DIRS = {k: d for k, d, _, _ in ALL_MODELS}
MODELS = [(k, r, l) for k, _, r, l in ALL_MODELS]   # replaced in main() by --models
MODES   = ["mono-DS", "dual-DS"]
NPEDCAS = [5, 15, 30]
CWDS, QSRCS, PSRCS = [0, 1], list(range(6)), [1, 2, 3]
DEFAULT_COMBO = (0, 2, 1)
N_STA = 30
PCTS = [("P50", 0.50), ("P95", 0.95), ("P99", 0.99)]


def tag(c, q, s):
    return f"c{c:d}_q{q:02d}_s{s:02d}"


def model_dir(key):
    return ROOT / MODEL_DIRS[key]


# ─────────────────────────── loading ─────────────────────────────────

def load_pctls(key, rate, mode):
    p = model_dir(key) / mode / f"combo_percentile_summary_{rate}.csv"
    out = {}
    if not p.exists():
        return out
    for r in csv.DictReader(p.open()):
        out[(int(r["CWds"]), int(r["QSRC"]), int(r["PSRC"]),
             int(r["nPedca"]), r["delay_type"])] = {
            "P50": float(r["P50_us"]), "P95": float(r["P95_us"]),
            "P99": float(r["P99_us"])}
    return out


def load_cdf(path):
    if not path.exists():
        return None, None
    rows = sorted((float(r["bin_start_us"]), float(r["bin_end_us"]),
                   float(r["probability"]))
                  for r in csv.DictReader(path.open()))
    if not rows:
        return None, None
    mids, cum, run = [], [], 0.0
    for a, b, p in rows:
        run += p
        mids.append(0.5 * (a + b))
        cum.append(run)
    return mids, cum


def pct_from_cdf(mids, cum, t):
    if not mids:
        return float("nan")
    for m, c in zip(mids, cum):
        if c >= t:
            return m
    return mids[-1]


def cdf_at(mids, cum, x):
    if not mids:
        return float("nan")
    v = 0.0
    for m, c in zip(mids, cum):
        if m <= x:
            v = c
        else:
            break
    return v


def parse_losses(path):
    """-> {n_pedca: {'pedca_loss': %, 'legacy_loss': %}} from a stats txt."""
    import re
    out, cur = {}, None
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        s = line.strip()
        m = re.match(r"P-EDCA STAs\s*=\s*(\d+)/\d+", s)
        if m:
            cur = int(m.group(1))
            out[cur] = {}
            continue
        if cur is None:
            continue
        for k, name in [("PEDCA_STA_SUCC_COUNT", "ps"), ("PEDCA_STA_FAIL_COUNT", "pf"),
                        ("LEGACY_STA_SUCC_COUNT", "ls"), ("LEGACY_STA_FAIL_COUNT", "lf")]:
            if s.startswith(k + ":"):
                try:
                    out[cur][name] = float(s.split(":")[1].strip())
                except ValueError:
                    pass
                break
    for n, d in out.items():
        ps, pf = d.get("ps", 0.0), d.get("pf", 0.0)
        ls, lf = d.get("ls", 0.0), d.get("lf", 0.0)
        d["pedca_loss"] = 100.0 * pf / (ps + pf) if (ps + pf) else float("nan")
        d["legacy_loss"] = 100.0 * lf / (ls + lf) if (ls + lf) else float("nan")
    return out


def collect_mode(key, rate, mode, deadline_us):
    """-> recs[(c,q,s,n)] = {P50,P95,P99,loss,ontime}"""
    pct = load_pctls(key, rate, mode)
    recs = {}
    for c in CWDS:
        for q in QSRCS:
            for s in PSRCS:
                t = tag(c, q, s)
                losses = parse_losses(model_dir(key) / mode / t /
                                      f"pedca_count_sweep_statistics_{t}_{rate}.txt")
                for n in NPEDCAS:
                    d = {}
                    p = pct.get((c, q, s, n, "pedca"))
                    if p:
                        d.update(p)
                    d["loss"] = losses.get(n, {}).get("pedca_loss", float("nan"))
                    mids, cum = load_cdf(
                        model_dir(key) / mode / t /
                        f"{t}_p{n:02d}_pedca_sta_delay_pdf_nSta{N_STA}_{rate}.csv")
                    d["ontime"] = (100.0 * (1 - d["loss"] / 100.0)
                                   * cdf_at(mids, cum, deadline_us)
                                   if mids and d["loss"] == d["loss"] else float("nan"))
                    recs[(c, q, s, n)] = d
    return recs


def baseline(key, rate):
    """EDCA-only reference: percentiles from the legacy-STA CDF + its loss."""
    d = {}
    for mode in MODES:
        base_dir = model_dir(key) / mode / "edca_only"
        mids, cum = load_cdf(base_dir /
                             f"edca_only_p00_legacy_sta_delay_pdf_nSta{N_STA}_{rate}.csv")
        if not mids:
            continue
        loss = parse_losses(base_dir /
                            f"pedca_count_sweep_statistics_edca_only_{rate}.txt")
        cand = {name: pct_from_cdf(mids, cum, t) for name, t in PCTS}
        cand["loss"] = loss.get(0, {}).get("legacy_loss", float("nan"))
        cand["_mode"] = mode
        if not d:
            d = cand
        else:
            # mono and dual run nPedca=0 identically; flag it if they diverge.
            if any(abs(d[k] - cand[k]) > 1e-6 for k, _ in PCTS):
                d["_mismatch"] = True
    return d


def pick_best(recs, n, select_by):
    """Best combo for one nPedca under the chosen criterion."""
    cands = []
    for c in CWDS:
        for q in QSRCS:
            for s in PSRCS:
                d = recs[(c, q, s, n)]
                v = d.get("P99") if select_by == "p99" else d.get("ontime")
                if v is None or v != v:
                    continue
                cands.append(((v if select_by == "p99" else -v), (c, q, s), d))
    if not cands:
        return None, None
    cands.sort(key=lambda t: t[0])
    return cands[0][1], cands[0][2]


# ──────────────────────────── main ───────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--select-by", choices=["p99", "ontime"], default="p99",
                    help="How 'best' is chosen (default: p99 = smallest P-EDCA P99)")
    ap.add_argument("--deadline-ms", type=float, default=10.0)
    ap.add_argument("--models", nargs="+", default=None,
                    choices=[k for k, _, _, _ in ALL_MODELS],
                    help="Which sweeps to compare (default: all that have data)")
    ap.add_argument("--dpi", type=int, default=200)
    a = ap.parse_args()
    deadline_us = a.deadline_ms * 1000.0
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    global MODELS
    sel = a.models or [k for k, _, _, _ in ALL_MODELS]
    MODELS = [(k, r, l) for k, _, r, l in ALL_MODELS if k in sel]
    # Keep one output set per model selection so runs do not overwrite each other.
    out_tag = a.select_by if len(MODELS) == len(ALL_MODELS) else \
        f"{a.select_by}_{'-'.join(k for k, _, _ in MODELS)}"

    crit = ("smallest P-EDCA STA P99 delay" if a.select_by == "p99"
            else f"highest on-time delivery R({a.deadline_ms:g}ms)")
    L = ["=" * 104,
         "  CROSS-TRAFFIC-MODEL COMPARISON — P-EDCA STA VO DELAY",
         f"  nSta={N_STA}   best parameters chosen per nPedca by: {crit}",
         f"  default param = CWds={DEFAULT_COMBO[0]}/QSRC={DEFAULT_COMBO[1]}"
         f"/PSRC={DEFAULT_COMBO[2]}",
         "=" * 104, "",
         "  Percentiles cover DELIVERED packets only -- read them next to loss%.",
         ""]
    rows_for_plot = {}
    summary_rows = []

    for key, rate, label in MODELS:
        base = baseline(key, rate)
        if not base:
            L.append(f"  !! {key}: no EDCA-only baseline found, skipped\n")
            continue
        recs = {m: collect_mode(key, rate, m, deadline_us) for m in MODES}

        L.append("=" * 104)
        L.append(f"  {label}    ({key}, {rate})")
        if base.get("_mismatch"):
            L.append("  WARNING: mono-DS and dual-DS EDCA-only baselines differ "
                     "(they should be identical at nPedca=0)")
        L.append("=" * 104)

        for n in NPEDCAS:
            L.append(f"\n  --- nPedca = {n}/{N_STA} ---")
            L.append(f"  {'configuration':<26} {'param':<12} {'P50':>8} {'P95':>8} "
                     f"{'P99':>8} {'loss%':>7}")
            L.append("  " + "-" * 74)
            L.append(f"  {'EDCA-only (reference)':<26} {'--':<12} "
                     f"{base['P50']:>8.0f} {base['P95']:>8.0f} {base['P99']:>8.0f} "
                     f"{base['loss']:>7.2f}")

            entry = {}
            for mode in MODES:
                dd = recs[mode][(*DEFAULT_COMBO, n)]
                L.append(f"  {mode + ' default':<26} {tag(*DEFAULT_COMBO):<12} "
                         f"{dd.get('P50', float('nan')):>8.0f} "
                         f"{dd.get('P95', float('nan')):>8.0f} "
                         f"{dd.get('P99', float('nan')):>8.0f} "
                         f"{dd.get('loss', float('nan')):>7.2f}")
                combo, bd = pick_best(recs[mode], n, a.select_by)
                L.append(f"  {mode + ' best':<26} {tag(*combo):<12} "
                         f"{bd['P50']:>8.0f} {bd['P95']:>8.0f} {bd['P99']:>8.0f} "
                         f"{bd['loss']:>7.2f}")
                entry[mode] = (combo, bd)

            combo, bd = entry["dual-DS"]
            L.append("")
            L.append(f"  >> dual-DS best ({tag(*combo)}) vs EDCA-only:")
            red = {}
            for name, _ in PCTS:
                r = (bd[name] - base[name]) / base[name] * 100.0
                red[name] = r
                L.append(f"       {name}: {base[name]:>8.0f} -> {bd[name]:>8.0f} us   "
                         f"{r:+7.1f} %   ({'improvement' if r < 0 else 'worse'})")
            rows_for_plot.setdefault(key, {})[n] = red
            summary_rows.append(dict(model=key, label=label, n=n, combo=tag(*combo),
                                     base=base, best=bd, red=red))
        L.append("")

    # ── compact cross-model summary of the dual-best gain ──
    L.append("=" * 104)
    L.append("  SUMMARY — dual-DS best vs EDCA-only  (negative % = delay reduction)")
    L.append("=" * 104)
    L.append("")
    L.append(f"  {'traffic model':<26} {'nPedca':>6} {'param':<12} "
             f"{'dP50':>9} {'dP95':>9} {'dP99':>9}  {'loss%':>7}")
    L.append("  " + "-" * 84)
    for r in summary_rows:
        L.append(f"  {r['label']:<26} {r['n']:>6} {r['combo']:<12} "
                 f"{r['red']['P50']:>8.1f}% {r['red']['P95']:>8.1f}% "
                 f"{r['red']['P99']:>8.1f}%  {r['best']['loss']:>7.2f}")
    L.append("")

    txt = OUT_DIR / f"traffic_model_comparison_{out_tag}.txt"
    txt.write_text("\n".join(L) + "\n")

    csv_path = OUT_DIR / f"traffic_model_comparison_{out_tag}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "nPedca", "config", "param", "P50_us", "P95_us",
                    "P99_us", "loss_pct"])
        for key, rate, label in MODELS:
            base = baseline(key, rate)
            if not base:
                continue
            recs = {m: collect_mode(key, rate, m, deadline_us) for m in MODES}
            for n in NPEDCAS:
                w.writerow([key, n, "EDCA-only", "--", f"{base['P50']:.1f}",
                            f"{base['P95']:.1f}", f"{base['P99']:.1f}",
                            f"{base['loss']:.2f}"])
                for mode in MODES:
                    dd = recs[mode][(*DEFAULT_COMBO, n)]
                    w.writerow([key, n, f"{mode} default", tag(*DEFAULT_COMBO),
                                f"{dd.get('P50', float('nan')):.1f}",
                                f"{dd.get('P95', float('nan')):.1f}",
                                f"{dd.get('P99', float('nan')):.1f}",
                                f"{dd.get('loss', float('nan')):.2f}"])
                    combo, bd = pick_best(recs[mode], n, a.select_by)
                    w.writerow([key, n, f"{mode} best", tag(*combo),
                                f"{bd['P50']:.1f}", f"{bd['P95']:.1f}",
                                f"{bd['P99']:.1f}", f"{bd['loss']:.2f}"])

    # ── grouped bar chart of the dual-best reduction ──
    fig, axes = plt.subplots(1, len(NPEDCAS), figsize=(5.4 * len(NPEDCAS), 5.0),
                             squeeze=False, sharey=True)
    colors = {"P50": "#4C72B0", "P95": "#DD8452", "P99": "#C44E52"}
    keys = [k for k, _, _ in MODELS if k in rows_for_plot]
    for ci, n in enumerate(NPEDCAS):
        ax = axes[0][ci]
        x = range(len(keys))
        width = 0.26
        for i, (name, _) in enumerate(PCTS):
            vals = [rows_for_plot[k][n][name] for k in keys]
            ax.bar([xx + (i - 1) * width for xx in x], vals, width,
                   color=colors[name], label=name)
            for xx, v in zip(x, vals):
                ax.text(xx + (i - 1) * width, v + (-1.6 if v < 0 else 0.6),
                        f"{v:.0f}", ha="center",
                        va="top" if v < 0 else "bottom", fontsize=7.5)
        ax.axhline(0, color="black", lw=1.0)
        ax.set_xticks(list(x), keys, fontsize=9)
        ax.set_title(f"nPedca = {n}/{N_STA}", fontsize=11)
        if ci == 0:
            ax.set_ylabel("delay change vs EDCA-only (%)\nnegative = improvement")
        ax.grid(axis="y", alpha=0.25, lw=0.6)
        ax.legend(fontsize=8.5)
    fig.suptitle("Dual-DS at best parameters vs EDCA-only — delay reduction by "
                 f"traffic model\n(best chosen per nPedca by {crit}; "
                 "percentiles cover delivered packets only)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    pdf = OUT_DIR / f"dual_best_vs_edca_reduction_{out_tag}.pdf"
    fig.savefig(pdf, dpi=a.dpi, bbox_inches="tight")
    plt.close(fig)

    print("\n".join(L))
    for p in (txt, csv_path, pdf):
        print(f"  -> {p.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
