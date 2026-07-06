#!/usr/bin/env python3
"""
Cwds Comparison — Fixed nSta=30, QSRC=2, PSRC=1, sweep nPedca × Cwds ∈ {0,1}
===============================================================================
For each nPedca value in N_PEDCA, runs simulations with cwds=0 (default) and
cwds=1 (both with QSRC=2, PSRC=1 fixed), then for each nPedca:
  - Overlays the two CDFs
  - Prints / saves a percentile comparison table (Median / P95 / P99)

Usage:
  python3 compare_cwds.py                      # Full sim + plot
  python3 compare_cwds.py --plot-only          # Re-plot from existing CSVs
  python3 compare_cwds.py --workers 4          # Parallel workers
  python3 compare_cwds.py --runs 10            # Runs per scenario
  python3 compare_cwds.py --nPedca 0 15 30    # Override nPedca list
"""

import argparse
import csv
import math
import os
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════
#  USER-CONFIGURABLE PARAMETERS
# ══════════════════════════════════════════════════════════════════════
N_STA       = 30
N_PEDCA     = [0, 5, 10, 15, 20, 25, 30]   # sweep these n_pedca values
QSRC        = 2
PSRC        = 1
CWDS_VALUES = [0, 1]                        # 0 = default baseline, 1 = test
DATA_RATE   = "1Mbps"
SIM_TIME    = 10.0
BIN_WIDTH   = 5                             # VO delay PDF bin width (µs)
N_RUNS      = 10
MAX_WORKERS = max(1, int((os.cpu_count() or 4) // 1.2))
SIM_BINARY  = "scratch/pedca_verification_nsta.cc"
# ══════════════════════════════════════════════════════════════════════

NS3_DIR = Path("/home/wmnlab/Desktop/ns-3.45")
OUT_DIR = Path("/home/wmnlab/Desktop/ns-3.45/scratch/delay_pdf/fix_nsta30_cwds1")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm


# ─────────────────────── Filename Helpers ────────────────────────────

def csv_name(n_pedca: int, cwds: int, data_rate: str, run_idx: int = None) -> str:
    base = (f"cwds{cwds}_nPedca{n_pedca:02d}_vo_delay_pdf"
            f"_nSta{N_STA}_qsrc{QSRC}_psrc{PSRC}_{data_rate}")
    return f"{base}_run{run_idx}.csv" if run_idx is not None else f"{base}.csv"

def pedca_sta_csv_name(n_pedca: int, cwds: int, data_rate: str,
                       run_idx: int = None) -> str:
    base = (f"cwds{cwds}_nPedca{n_pedca:02d}_pedca_sta_delay_pdf"
            f"_nSta{N_STA}_qsrc{QSRC}_psrc{PSRC}_{data_rate}")
    return f"{base}_run{run_idx}.csv" if run_idx is not None else f"{base}.csv"

def legacy_sta_csv_name(n_pedca: int, cwds: int, data_rate: str,
                        run_idx: int = None) -> str:
    base = (f"cwds{cwds}_nPedca{n_pedca:02d}_legacy_sta_delay_pdf"
            f"_nSta{N_STA}_qsrc{QSRC}_psrc{PSRC}_{data_rate}")
    return f"{base}_run{run_idx}.csv" if run_idx is not None else f"{base}.csv"


# ─────────────────────── Single Simulation Task ──────────────────────

def run_single_sim(n_pedca: int, cwds: int, data_rate: str,
                   sim_time: float, bin_us: int, run_idx: int = 0) -> dict:
    ratio = n_pedca / N_STA

    csv_path        = OUT_DIR / csv_name(n_pedca, cwds, data_rate, run_idx)
    pedca_csv_path  = OUT_DIR / pedca_sta_csv_name(n_pedca, cwds, data_rate, run_idx)
    legacy_csv_path = OUT_DIR / legacy_sta_csv_name(n_pedca, cwds, data_rate, run_idx)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Note: QSRC=2 (PEDCA_RETRY_THRESHOLD) and PSRC=1 (PEDCA_CONSECUTIVE_ATTEMPT)
    # are already hardcoded defaults in the C++ binary — no need to pass them.
    sim_args = (
        f"--nSta={N_STA} "
        f"--simTime={sim_time} "
        f"--dataRate={data_rate} "
        f"--pedcaRatio={ratio:.6f} "
        f"--cwds={cwds} "
        f"--voicePdfBinUs={bin_us} "
        f"--voicePdfOutput={csv_path.relative_to(NS3_DIR)} "
        f"--pedcaStaDelayOutput={pedca_csv_path.relative_to(NS3_DIR)} "
        f"--legacyStaDelayOutput={legacy_csv_path.relative_to(NS3_DIR)} "
        f"--clogFile=/dev/null "
        f"--RngRun={run_idx + 1}"
    )
    cmd = ["./ns3", "run", f"{SIM_BINARY} {sim_args}"]

    t0   = time.time()
    info = {
        "n_pedca":  n_pedca,
        "cwds":     cwds,
        "run_idx":  run_idx,
        "csv_path": None,
        "success":  False,
        "stdout":   "",
        "elapsed":  0.0,
    }

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, cwd=str(NS3_DIR)
        )
        info["stdout"] = result.stdout
        if csv_path.exists() and csv_path.stat().st_size > 10:
            info["csv_path"] = csv_path
            info["success"]  = True
    except subprocess.CalledProcessError as e:
        info["stdout"] = ((e.stdout or "") + "\nSTDERR:\n" + (e.stderr or ""))[-5000:]

    info["elapsed"] = time.time() - t0
    return info


# ─────────────────────── Histogram Averaging ─────────────────────────

def average_histograms(csv_paths: list, out_path: Path, n_runs: int):
    all_bins = defaultdict(list)
    for cp in csv_paths:
        try:
            with open(cp, "r", newline="") as f:
                for row in csv.DictReader(f):
                    key = (float(row["bin_start_us"]), float(row["bin_end_us"]))
                    all_bins[key].append(float(row["probability"]))
        except Exception:
            continue
    if not all_bins:
        return
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["bin_start_us", "bin_end_us", "bin_mid_us",
                          "pdf_per_us", "probability", "count"])
        for (start, end) in sorted(all_bins.keys()):
            probs = all_bins[(start, end)]
            while len(probs) < n_runs:
                probs.append(0.0)
            avg_prob = sum(probs) / n_runs
            mid      = (start + end) / 2
            width    = end - start
            pdf      = avg_prob / width if width > 0 else 0
            writer.writerow([start, end, mid, f"{pdf:.8g}", f"{avg_prob:.8g}", 0])


def aggregate_runs(run_results: list, n_pedca: int, cwds: int,
                   data_rate: str, n_runs: int) -> dict:
    csv_paths = [r["csv_path"] for r in run_results if r["csv_path"]]
    avg_csv   = OUT_DIR / csv_name(n_pedca, cwds, data_rate)
    if csv_paths:
        average_histograms(csv_paths, avg_csv, n_runs)
        for cp in csv_paths:
            try:
                cp.unlink()
            except OSError:
                pass
    n_success = sum(1 for r in run_results if r["success"])
    return {
        "n_pedca":   n_pedca,
        "cwds":      cwds,
        "csv_path":  avg_csv if csv_paths else None,
        "n_success": n_success,
        "n_runs":    n_runs,
        "elapsed":   sum(r["elapsed"] for r in run_results),
        "success":   n_success > 0,
    }


# ─────────────────────── Histogram Loading ───────────────────────────

def load_histogram(csv_path: Path):
    rows = []
    with csv_path.open("r", newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "start": float(row["bin_start_us"]),
                "end":   float(row["bin_end_us"]),
                "prob":  float(row["probability"]),
            })
    if not rows:
        raise ValueError("CSV has no data rows")

    widths    = sorted(r["end"] - r["start"] for r in rows if r["end"] > r["start"])
    bin_width = widths[len(widths) // 2]
    min_start = min(r["start"] for r in rows)
    max_end   = max(r["end"]   for r in rows)

    prob_lookup = {round(r["start"] / bin_width) * bin_width: r["prob"] for r in rows}

    full_starts, full_probs = [], []
    cur = min_start
    eps = bin_width * 1e-6
    while cur < max_end - eps:
        full_starts.append(cur)
        full_probs.append(prob_lookup.get(round(cur / bin_width) * bin_width, 0.0))
        cur += bin_width

    mids = [s + 0.5 * bin_width for s in full_starts]
    return mids, full_probs, min_start, max_end, bin_width


# ─────────────────────── Percentile Computation ──────────────────────

PCT_LEVELS = [0.50, 0.95, 0.99]
PCT_LABELS = {0.50: "Median (P50)", 0.95: "P95", 0.99: "P99"}


def compute_percentiles(mids: list, probs: list, percentiles: list) -> dict:
    total = sum(probs)
    if total <= 0:
        return {p: 0.0 for p in percentiles}
    result, running, t_idx = {}, 0.0, 0
    targets = sorted(percentiles)
    for m, p in zip(mids, probs):
        running += p
        while t_idx < len(targets) and running / total >= targets[t_idx]:
            result[targets[t_idx]] = m
            t_idx += 1
        if t_idx >= len(targets):
            break
    for pct in targets:
        if pct not in result:
            result[pct] = mids[-1] if mids else 0.0
    return result


# ─────────────────────── Scale / Tick Helpers ────────────────────────

def nice_step(value: float) -> float:
    if value <= 0:
        return 1.0
    exp  = math.floor(math.log10(value))
    frac = value / (10 ** exp)
    nice = 1 if frac <= 1 else (2 if frac <= 2 else (5 if frac <= 5 else 10))
    return nice * (10 ** exp)


def build_ticks(xmin: float, xmax: float, fig_width: float):
    span  = max(xmax - xmin, 1.0)
    step  = nice_step(span / max(6, int(fig_width * 2.0)))
    first = math.floor(xmin / step) * step
    ticks, t = [], first
    while t <= xmax + step * 0.01:
        ticks.append(round(t, 6))
        t += step
    return ticks


def zoom_xmax_from_series(all_series: list, percentile: float = 0.95) -> float:
    combined = defaultdict(float)
    for mids, probs in all_series:
        for m, p in zip(mids, probs):
            combined[m] += p
    if not combined:
        return float("inf")
    n     = len(all_series)
    total = sum(combined.values()) / n
    if total <= 0:
        return float("inf")
    running = 0.0
    for m in sorted(combined.keys()):
        running += combined[m] / n
        if running >= percentile * total:
            return m
    return max(combined.keys())


# ─────────────────────── Color / Style Helpers ───────────────────────

CWDS_COLORS = {0: "#4C72B0", 1: "#C44E52"}      # blue=default, red=cwds1
CWDS_STYLES = {0: "--",      1: "-"}             # dashed=cwds0 so it shows through cwds1
CWDS_WIDTHS = {0: 2.2,       1: 1.8}
CWDS_LABELS = {
    0: f"cwds=0  (default, QSRC={QSRC}, PSRC={PSRC})",
    1: f"cwds=1  (QSRC={QSRC}, PSRC={PSRC})",
}


def _npedca_colors(n_pedca_list: list) -> dict:
    """Return distinct colors for each n_pedca value (viridis palette)."""
    cmap = cm.get_cmap("viridis", max(len(n_pedca_list), 2))
    return {n: cmap(i) for i, n in enumerate(n_pedca_list)}


# ─────────────────────── Per-nPedca CDF Comparison ───────────────────

def plot_cdf_per_npedca(n_pedca: int, data_rate: str, n_runs: int,
                        fig_width: float = 12.0, fig_height: float = 7.0,
                        dpi: int = 200):
    """
    One CDF figure per n_pedca: cwds=0 vs cwds=1.
    Returns (out_path, pct_results_dict) or (None, {}).
    """
    loaded     = {}
    all_series = []

    for cwds in CWDS_VALUES:
        path = OUT_DIR / csv_name(n_pedca, cwds, data_rate)
        if not path.exists():
            continue
        try:
            mids, probs, xmin, xmax, bw = load_histogram(path)
            loaded[cwds] = (mids, probs, xmin)
            all_series.append((mids, probs))
        except Exception as e:
            print(f"    ⚠  load error {path.name}: {e}")

    if not loaded:
        return None, {}

    zoom_max    = zoom_xmax_from_series(all_series, 0.99) * 1.10
    global_xmin = min(v[2] for v in loaded.values())
    runs_label  = f", avg of {n_runs} runs" if n_runs > 1 else ""

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    pct_results = {}

    # Draw cwds=1 first (solid red), then cwds=0 (dashed blue) on top
    for cwds in sorted(CWDS_VALUES, reverse=True):
        if cwds not in loaded:
            continue
        mids, probs, _ = loaded[cwds]
        total = sum(probs)
        if total <= 0:
            continue

        cdf_x, cdf_y, running = [], [], 0.0
        for m, p in zip(mids, probs):
            running += p
            if m <= zoom_max:
                cdf_x.append(m)
                cdf_y.append(running / total)

        color  = CWDS_COLORS[cwds]
        lstyle = CWDS_STYLES[cwds]
        lwidth = CWDS_WIDTHS[cwds]
        ax.plot(cdf_x, cdf_y, linewidth=lwidth, color=color,
                linestyle=lstyle, label=CWDS_LABELS[cwds])

        pcts = compute_percentiles(mids, probs, PCT_LEVELS)
        pct_results[cwds] = pcts
        for i, p_val in enumerate(PCT_LEVELS):
            x_val = pcts[p_val]
            if x_val <= zoom_max:
                ax.axvline(x=x_val, color=color, linewidth=0.8,
                           linestyle=lstyle, alpha=0.6)
                ax.text(x_val + zoom_max * 0.004,
                        0.02 + i * 0.07,
                        f"{PCT_LABELS[p_val]}\n{x_val:.0f} µs",
                        fontsize=7, color=color, va="bottom")

    ax.set_xlim(global_xmin, zoom_max)
    ax.set_xticks(build_ticks(global_xmin, zoom_max, fig_width))
    ax.tick_params(axis="x", labelsize=8, rotation=45)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Delay (µs)", fontsize=11)
    ax.set_ylabel("Cumulative Probability", fontsize=11)
    ax.set_title(
        f"VO Delay CDF: cwds=0 vs cwds=1  —  "
        f"nSta={N_STA}, nPedca={n_pedca}, QSRC={QSRC}, PSRC={PSRC}, "
        f"{data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.legend(loc="lower right", fontsize=10)

    out_path = OUT_DIR / f"cdf_nPedca{n_pedca:02d}_cwds0_vs_cwds1_{data_rate}.pdf"
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path, pct_results


# ─────────────────────── Multi-Panel CDF Grid ────────────────────────

def plot_cdf_grid(n_pedca_list: list, data_rate: str, n_runs: int,
                  fig_width: float = 14.0, dpi: int = 200):
    """
    One figure with one subplot per n_pedca value.
    Each subplot shows cwds=0 vs cwds=1 CDF for that n_pedca.
    """
    n = len(n_pedca_list)
    if n == 0:
        return None

    ncols = min(3, n)
    nrows = math.ceil(n / ncols)
    fig_height = nrows * 5.0

    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(fig_width, fig_height),
                             squeeze=False)
    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""

    for idx, n_pedca in enumerate(n_pedca_list):
        row, col = divmod(idx, ncols)
        ax = axes[row][col]

        loaded     = {}
        all_series = []
        for cwds in CWDS_VALUES:
            path = OUT_DIR / csv_name(n_pedca, cwds, data_rate)
            if not path.exists():
                continue
            try:
                mids, probs, xmin, xmax, bw = load_histogram(path)
                loaded[cwds] = (mids, probs, xmin)
                all_series.append((mids, probs))
            except Exception:
                continue

        if not loaded:
            ax.set_visible(False)
            continue

        zoom_max    = zoom_xmax_from_series(all_series, 0.99) * 1.10
        global_xmin = min(v[2] for v in loaded.values())

        # Draw cwds=1 first, then cwds=0 (dashed) on top
        for cwds in sorted(CWDS_VALUES, reverse=True):
            if cwds not in loaded:
                continue
            mids, probs, _ = loaded[cwds]
            total = sum(probs)
            if total <= 0:
                continue
            cdf_x, cdf_y, running = [], [], 0.0
            for m, p in zip(mids, probs):
                running += p
                if m <= zoom_max:
                    cdf_x.append(m)
                    cdf_y.append(running / total)
            ax.plot(cdf_x, cdf_y,
                    linewidth=CWDS_WIDTHS[cwds],
                    color=CWDS_COLORS[cwds],
                    linestyle=CWDS_STYLES[cwds],
                    label=f"cwds={cwds}")

            pcts = compute_percentiles(mids, probs, PCT_LEVELS)
            for p_val in PCT_LEVELS:
                x_val = pcts[p_val]
                if x_val <= zoom_max:
                    ax.axvline(x=x_val, color=CWDS_COLORS[cwds],
                               linewidth=0.7, linestyle=CWDS_STYLES[cwds],
                               alpha=0.55)

        ax.set_xlim(global_xmin, zoom_max)
        ax.set_ylim(0, 1.02)
        ax.set_xlabel("Delay (µs)", fontsize=8)
        ax.set_ylabel("CDF", fontsize=8)
        ax.set_title(f"nPedca={n_pedca}", fontsize=10, fontweight="bold")
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.25, linestyle="--")
        ax.legend(loc="lower right", fontsize=7)

    # Hide unused subplots
    for idx in range(n, nrows * ncols):
        row, col = divmod(idx, ncols)
        axes[row][col].set_visible(False)

    fig.suptitle(
        f"VO Delay CDF: cwds=0 vs cwds=1  |  QSRC={QSRC}, PSRC={PSRC}, "
        f"nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=13, fontweight="bold"
    )
    fig.tight_layout()

    out_path = OUT_DIR / f"cdf_grid_cwds0_vs_cwds1_{data_rate}.pdf"
    fig.savefig(str(out_path), dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ─────────── Percentile vs nPedca Lines (one figure per metric) ──────

def plot_percentile_vs_npedca(all_pcts: dict, data_rate: str, n_runs: int,
                               fig_width: float = 12.0, fig_height: float = 6.0,
                               dpi: int = 200):
    """
    For each percentile level (P50, P95, P99), plot delay vs n_pedca
    with two lines: cwds=0 and cwds=1.
    all_pcts: {n_pedca: {cwds: {pct_level: delay_us}}}
    """
    n_pedca_list = sorted(all_pcts.keys())
    if not n_pedca_list:
        return None

    fig, axes = plt.subplots(1, len(PCT_LEVELS),
                             figsize=(fig_width, fig_height))
    if len(PCT_LEVELS) == 1:
        axes = [axes]

    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""

    for ax, p_val in zip(axes, PCT_LEVELS):
        for cwds in CWDS_VALUES:
            xs, ys = [], []
            for n_pedca in n_pedca_list:
                v = all_pcts.get(n_pedca, {}).get(cwds, {}).get(p_val)
                if v is not None:
                    xs.append(n_pedca)
                    ys.append(v)
            if xs:
                ax.plot(xs, ys, marker="o", markersize=5, linewidth=1.5,
                        color=CWDS_COLORS[cwds],
                        label=f"cwds={cwds}")

        ax.set_xlabel("nPedca", fontsize=10)
        ax.set_ylabel("Delay (µs)", fontsize=10)
        ax.set_title(f"{PCT_LABELS[p_val]}", fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.3, linestyle="--")
        ax.set_xticks(n_pedca_list)
        ax.tick_params(axis="x", labelsize=8, rotation=45)
        ax.legend(fontsize=9)

    fig.suptitle(
        f"Delay Percentiles vs nPedca: cwds=0 vs cwds=1  |  "
        f"QSRC={QSRC}, PSRC={PSRC}, nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    fig.tight_layout()

    out_path = OUT_DIR / f"percentiles_vs_npedca_cwds0_vs_cwds1_{data_rate}.pdf"
    fig.savefig(str(out_path), dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ─────────────────────── Percentile Stats Text ───────────────────────

def write_percentile_stats(all_pcts: dict, data_rate: str, n_runs: int,
                           n_pedca_list: list):
    out_path = OUT_DIR / f"percentile_comparison_cwds0_vs_cwds1_{data_rate}.txt"

    col_w = 16
    header = (f"  {'nPedca':<8}{'Metric':<18}"
              f"{'cwds=0 (µs)':>{col_w}}"
              f"{'cwds=1 (µs)':>{col_w}}"
              f"{'Δ (cwds1−0)':>{col_w}}"
              f"{'Δ %':>{col_w}}")
    sep = "  " + "-" * (len(header) - 2)

    lines = [
        "=" * 80,
        f"  Delay Percentile Comparison: cwds=0 vs cwds=1",
        f"  nSta={N_STA}  QSRC={QSRC}  PSRC={PSRC}",
        f"  dataRate={data_rate}  simTime={SIM_TIME}s  runs={n_runs} (averaged)",
        f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 80, "",
        header, sep,
    ]

    for n_pedca in n_pedca_list:
        for p_val in PCT_LEVELS:
            v0 = all_pcts.get(n_pedca, {}).get(0, {}).get(p_val)
            v1 = all_pcts.get(n_pedca, {}).get(1, {}).get(p_val)
            label = PCT_LABELS[p_val]
            n_str = str(n_pedca) if p_val == PCT_LEVELS[0] else ""
            if v0 is not None and v1 is not None:
                delta     = v1 - v0
                delta_pct = (delta / v0 * 100.0) if v0 != 0 else float("nan")
                sign = "+" if delta >= 0 else ""
                lines.append(
                    f"  {n_str:<8}{label:<18}"
                    f"{v0:>{col_w}.1f}"
                    f"{v1:>{col_w}.1f}"
                    f"{sign}{delta:>{col_w-1}.1f}"
                    f"{sign}{delta_pct:>{col_w-1}.2f}%"
                )
            else:
                v0s = f"{v0:.1f}" if v0 is not None else "N/A"
                v1s = f"{v1:.1f}" if v1 is not None else "N/A"
                lines.append(
                    f"  {n_str:<8}{label:<18}"
                    f"{v0s:>{col_w}}{v1s:>{col_w}}"
                    f"{'N/A':>{col_w}}{'N/A':>{col_w}}"
                )
        lines.append(sep)

    lines += [
        "",
        "  Note: Δ < 0 means cwds=1 reduces delay (improvement).",
        "        Δ% = (cwds1 − cwds0) / cwds0 × 100",
        "",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


def print_percentile_table(all_pcts: dict, n_pedca_list: list):
    col_w = 14
    header = (f"  {'nPedca':<8}{'Metric':<18}"
              f"{'cwds=0 (µs)':>{col_w}}"
              f"{'cwds=1 (µs)':>{col_w}}"
              f"{'Δ (µs)':>{col_w}}"
              f"{'Δ %':>{col_w}}")
    sep = "  " + "─" * (len(header) - 2)

    print(f"\n{'─'*80}")
    print(f"  Percentile Comparison: cwds=0 vs cwds=1")
    print(f"  (nSta={N_STA}, QSRC={QSRC}, PSRC={PSRC})")
    print(f"{'─'*80}")
    print(header)
    print(sep)

    for n_pedca in n_pedca_list:
        for p_val in PCT_LEVELS:
            v0    = all_pcts.get(n_pedca, {}).get(0, {}).get(p_val)
            v1    = all_pcts.get(n_pedca, {}).get(1, {}).get(p_val)
            label = PCT_LABELS[p_val]
            n_str = str(n_pedca) if p_val == PCT_LEVELS[0] else ""
            if v0 is not None and v1 is not None:
                delta     = v1 - v0
                delta_pct = (delta / v0 * 100.0) if v0 != 0 else float("nan")
                sign = "+" if delta >= 0 else ""
                print(
                    f"  {n_str:<8}{label:<18}"
                    f"{v0:>{col_w}.1f}"
                    f"{v1:>{col_w}.1f}"
                    f"{sign}{delta:>{col_w-1}.1f}"
                    f"{sign}{delta_pct:>{col_w-1}.2f}%"
                )
            else:
                v0s = f"{v0:.1f}" if v0 is not None else "N/A"
                v1s = f"{v1:.1f}" if v1 is not None else "N/A"
                print(f"  {n_str:<8}{label:<18}"
                      f"{v0s:>{col_w}}{v1s:>{col_w}}"
                      f"{'N/A':>{col_w}}{'N/A':>{col_w}}")
        print(sep)

    print("  Δ < 0  means cwds=1 reduces delay (improvement)")
    print(f"{'─'*80}\n")


# ──────────────────────────── Main ───────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compare cwds=0 vs cwds=1 with fixed QSRC=2, PSRC=1, "
                    "sweeping nPedca values."
    )
    parser.add_argument("--plot-only", action="store_true",
                        help="Skip simulations, re-plot from existing CSVs")
    parser.add_argument("--workers",  type=int,   default=MAX_WORKERS)
    parser.add_argument("--runs",     type=int,   default=N_RUNS)
    parser.add_argument("--nPedca",   type=int,   nargs="+", default=N_PEDCA,
                        help="nPedca values to sweep (default: all in N_PEDCA list)")
    parser.add_argument("--fig-width",  type=float, default=12.0)
    parser.add_argument("--fig-height", type=float, default=7.0)
    parser.add_argument("--dpi",        type=int,   default=200)
    args = parser.parse_args()

    n_pedca_list = sorted(args.nPedca)
    data_rate    = DATA_RATE
    n_runs       = args.runs
    workers      = args.workers

    total_configs = len(n_pedca_list) * len(CWDS_VALUES)
    total_sims    = total_configs * n_runs

    print(f"\n╔══════════════════════════════════════════════════════════╗")
    print(f"║  cwds Comparison  (QSRC={QSRC}, PSRC={PSRC} fixed)               ║")
    print(f"║  nSta={N_STA}")
    print(f"║  nPedca sweep: {n_pedca_list}")
    print(f"║  cwds values:  {CWDS_VALUES}")
    print(f"║  dataRate={data_rate}  simTime={SIM_TIME}s")
    print(f"║  runs={n_runs}  workers={workers}")
    print(f"╚══════════════════════════════════════════════════════════╝\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t_total = time.time()

    if not args.plot_only:
        print(f"  Launching {total_configs} configs × {n_runs} runs = {total_sims} sims")
        print(f"  ({workers} concurrent)\n")

        raw_results = defaultdict(list)   # key = (n_pedca, cwds)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for n_pedca in n_pedca_list:
                for cwds in CWDS_VALUES:
                    for run_idx in range(n_runs):
                        fut = executor.submit(
                            run_single_sim,
                            n_pedca, cwds, data_rate, SIM_TIME, BIN_WIDTH, run_idx
                        )
                        futures[fut] = (n_pedca, cwds, run_idx)

            done = 0
            for future in as_completed(futures):
                n_pedca, cwds, run_idx = futures[future]
                done += 1
                try:
                    r = future.result()
                    raw_results[(n_pedca, cwds)].append(r)
                    status = "✔" if r["success"] else "✗"
                    if done % max(1, total_sims // 40) == 0 or not r["success"]:
                        print(f"  [{done:>4}/{total_sims}] {status}  "
                              f"nPedca={n_pedca:>2}  cwds={cwds}  "
                              f"run={run_idx}  {r['elapsed']:.1f}s")
                except Exception as e:
                    print(f"  ✗ nPedca={n_pedca} cwds={cwds} run={run_idx} "
                          f"EXCEPTION: {e}")

        print(f"\n{'─'*60}")
        print(f"  Aggregating...")
        for (n_pedca, cwds) in sorted(raw_results.keys()):
            agg    = aggregate_runs(raw_results[(n_pedca, cwds)],
                                    n_pedca, cwds, data_rate, n_runs)
            status = "✔" if agg["success"] else "✗"
            print(f"  {status} nPedca={n_pedca:>2}  cwds={cwds}  "
                  f"{agg['n_success']}/{n_runs} runs OK  "
                  f"elapsed={agg['elapsed']:.1f}s")
    else:
        print("  [plot-only mode]")
        for n_pedca in n_pedca_list:
            for cwds in CWDS_VALUES:
                p = OUT_DIR / csv_name(n_pedca, cwds, data_rate)
                print(f"  {'✔' if p.exists() else '✗ Missing'}: {p.name}")

    # ── Generate plots ──
    print(f"\n{'─'*60}")
    print(f"  Generating plots...")

    all_pcts = {}   # {n_pedca: {cwds: {pct_level: delay}}}

    # Per-nPedca individual CDF plots
    for n_pedca in n_pedca_list:
        out_path, pct_results = plot_cdf_per_npedca(
            n_pedca, data_rate, n_runs,
            args.fig_width, args.fig_height, args.dpi
        )
        if out_path:
            print(f"    ✔ {out_path.name}")
            all_pcts[n_pedca] = pct_results
        else:
            print(f"    ⚠  No data for nPedca={n_pedca}")

    # Multi-panel CDF grid
    grid_path = plot_cdf_grid(
        n_pedca_list, data_rate, n_runs, args.fig_width, args.dpi
    )
    if grid_path:
        print(f"    ✔ {grid_path.name}")
    else:
        print(f"    ⚠  No data for CDF grid")

    # Percentile-vs-nPedca line plots
    if all_pcts:
        pct_line_path = plot_percentile_vs_npedca(
            all_pcts, data_rate, n_runs,
            args.fig_width * 1.4, args.fig_height, args.dpi
        )
        if pct_line_path:
            print(f"    ✔ {pct_line_path.name}")

    # Percentile comparison table
    if all_pcts:
        print_percentile_table(all_pcts, n_pedca_list)
        stats_path = write_percentile_stats(all_pcts, data_rate, n_runs, n_pedca_list)
        print(f"    ✔ {stats_path.name}  ({stats_path.stat().st_size:,} bytes)")
    else:
        print("  ⚠  No percentile data (missing CSVs?)")

    elapsed = time.time() - t_total
    print(f"\n{'═'*60}")
    print(f"  Done!  Total time: {elapsed:.1f}s")
    print(f"  Output directory: {OUT_DIR}")
    print(f"\n  Output files:")
    for f in sorted(OUT_DIR.glob("cwds*")):
        if f.is_file():
            print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    for f in sorted(OUT_DIR.glob("cdf_*")):
        if f.is_file():
            print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    for f in sorted(OUT_DIR.glob("percentile*")):
        if f.is_file():
            print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
