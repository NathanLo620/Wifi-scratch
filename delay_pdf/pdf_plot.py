#!/usr/bin/env python3
"""
P-EDCA Ratio Sweep — Delay PDF Pipeline (Parallel, Multi-Run Averaging)
=========================================================================
Sweeps over P-EDCA enable ratios (e.g. 0%, 20%, 40%, 60%, 80%, 100%),
running pedca_verification_nsta.cc with --pedcaRatio=X for each.

For each (nSta, pedcaRatio) pair:
  - Runs N_RUNS simulations (different RngRun seeds)
  - Averages histogram probabilities across runs
  - Collects averaged statistics

Output per nSta:
  - ratio{R}_vo_delay_pdf_nSta{N}_{rate}.csv   (averaged histogram per ratio)
  - vo_delay_probability_nSta{N}_{rate}.pdf     (overlay plot, one line per ratio)
  - sim_log_nSta{N}_{rate}.txt                  (combined simulation log)
  - ratio_sweep_statistics_{rate}.txt            (all ratios × all nSta values)

Usage:
  python3 pdf_plot.py                            # Full sweep + plot
  python3 pdf_plot.py --plot-only                # Re-plot from existing CSVs
  python3 pdf_plot.py --workers 8                # Parallel workers
  python3 pdf_plot.py --runs 3                   # Override runs per scenario
  python3 pdf_plot.py --ratios 0.0 0.5 1.0       # Custom ratio list
  python3 pdf_plot.py --nsta 10 20 30            # Custom nSta list
"""

import argparse
import csv
import math
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from multiprocessing import cpu_count as mp_cpu_count

# ══════════════════════════════════════════════════════════════════════
#  USER-CONFIGURABLE PARAMETERS
# ══════════════════════════════════════════════════════════════════════
N_STA_LIST      = list(range(2, 51, 2))               # 2, 4, 6, ..., 50
PEDCA_RATIOS    = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]     # P-EDCA enable ratios
DATA_RATE       = "1Mbps"                            # Fixed data rate
SIM_TIME        = 10.0                                 # Simulation duration (s)
BIN_WIDTH       = 5                                    # VO delay PDF bin width (µs)
MAX_WORKERS     = 4 if not os.cpu_count() else max(1, int(os.cpu_count() // 1.5))  # Keep CPU near saturation
N_RUNS          = 10                                    # Runs to average
SIM_BINARY      = "scratch/pedca_verification_nsta.cc" # Single unified binary
# ══════════════════════════════════════════════════════════════════════

# Paths
NS3_DIR = Path("/home/wmnlab/Desktop/ns-3.45")
OUT_DIR = Path("/home/wmnlab/Desktop/ns-3.45/scratch/delay_pdf/delay_result_ratio_sweep_1Mbps_rts_on")

# ── Force non-interactive backend ──
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm


# ─────────────────────── Filename Helpers ────────────────────────────

def ratio_tag(ratio: float) -> str:
    """Return a filesystem-safe ratio tag, e.g. '0.0' -> 'r000', '0.2' -> 'r020'."""
    return f"r{int(ratio * 100):03d}"

def csv_name(ratio: float, n_sta: int, data_rate: str, run_idx: int = None) -> str:
    tag = ratio_tag(ratio)
    if run_idx is not None:
        return f"{tag}_vo_delay_pdf_nSta{n_sta}_{data_rate}_run{run_idx}.csv"
    return f"{tag}_vo_delay_pdf_nSta{n_sta}_{data_rate}.csv"

def combined_plot_name(n_sta: int, data_rate: str) -> str:
    return f"vo_delay_probability_nSta{n_sta}_{data_rate}.pdf"

def log_name(n_sta: int, data_rate: str) -> str:
    return f"sim_log_nSta{n_sta}_{data_rate}.txt"


# ─────────────────────── Single Simulation Task ──────────────────────

def run_single_sim(ratio: float, n_sta: int, data_rate: str,
                   sim_time: float, bin_us: int, run_idx: int = 0) -> dict:
    """
    Run ONE ns-3 simulation with a specific pedcaRatio and RngRun seed.
    Returns a dict with results. Thread-safe.
    """
    csv_file = csv_name(ratio, n_sta, data_rate, run_idx)
    csv_path = OUT_DIR / csv_file
    relative_csv = str(csv_path.relative_to(NS3_DIR))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    args = (
        f"--nSta={n_sta} "
        f"--simTime={sim_time} "
        f"--dataRate={data_rate} "
        f"--pedcaRatio={ratio} "
        f"--voicePdfBinUs={bin_us} "
        f"--voicePdfOutput={relative_csv} "
        f"--RngRun={run_idx + 1}"
    )
    cmd = ["./ns3", "run", f"{SIM_BINARY} {args}"]

    t0 = time.time()
    result_info = {
        "ratio":    ratio,
        "n_sta":    n_sta,
        "run_idx":  run_idx,
        "cmd":      " ".join(cmd),
        "csv_path": None,
        "stdout":   "",
        "stderr":   "",
        "success":  False,
        "elapsed":  0.0,
    }

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True,
            cwd=str(NS3_DIR)
        )
        # Extract stats immediately and discard full stdout to prevent huge RAM usage
        result_info["stdout"] = extract_stats_block(result.stdout)
        result_info["stderr"] = "" # discard stderr to save RAM

        if csv_path.exists() and csv_path.stat().st_size > 10:
            result_info["csv_path"] = csv_path
            result_info["success"] = True

    except subprocess.CalledProcessError as e:
        err_out = e.stdout or ""
        result_info["stdout"] = err_out[-5000:] if len(err_out) > 5000 else err_out
        result_info["stderr"] = "" # discard stderr on error to save RAM

    result_info["elapsed"] = time.time() - t0
    return result_info


# ─────────────────────── Histogram Averaging ─────────────────────────

def average_histograms(csv_paths: list, out_path: Path, n_runs: int):
    """
    Read multiple per-run histogram CSVs, average the probability values,
    and write a single averaged CSV.
    """
    all_bins = defaultdict(list)

    for cp in csv_paths:
        try:
            with open(cp, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
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
            mid = (start + end) / 2
            width = end - start
            pdf = avg_prob / width if width > 0 else 0
            writer.writerow([start, end, mid, f"{pdf:.8g}", f"{avg_prob:.8g}", 0])


# ─────────────────── Statistics Parsing & Averaging ──────────────────

def extract_stats_block(stdout: str) -> str:
    lines = stdout.splitlines()
    start_idx = None
    end_idx = len(lines)
    for i, line in enumerate(lines):
        if "WifiTxStatsHelper" in line and start_idx is None:
            start_idx = i
        if start_idx is not None and "VO Delay PDF" in line:
            end_idx = i
            break
    if start_idx is None:
        return "  (no WifiTxStatsHelper output found)\n"
    block = lines[start_idx:end_idx]
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block) + "\n"


def parse_stats(stdout: str) -> dict:
    """Parse WifiTxStatsHelper output into a structured dict."""
    block = extract_stats_block(stdout)
    result = {
        "pedca_ratio": 0.0,
        "total_successes": 0,
        "total_failures": 0,
        "total_retransmissions": 0,
        "per_ac": {},
        "failure_ac": {},
        "failure_reasons": {},
    }

    lines = block.splitlines()
    section = None
    current_ac = None

    for line in lines:
        s = line.strip()
        if s.startswith("P-EDCA Ratio:"):
            try: result["pedca_ratio"] = float(s.split(":")[1].strip())
            except: pass
        elif s.startswith("Total Successes:"):
            try: result["total_successes"] = float(s.split(":")[1].strip())
            except: pass
        elif s.startswith("Total Failures:"):
            try: result["total_failures"] = float(s.split(":")[1].strip())
            except: pass
        elif s.startswith("Total Retransmissions:"):
            try: result["total_retransmissions"] = float(s.split(":")[1].strip())
            except: pass
        elif "Per-AC Success" in s:
            section = "success"
            current_ac = None
        elif "Per-AC Failure Statistics" in s:
            section = "failure"
            current_ac = None
        elif "Failure Reasons" in s:
            section = "reasons"
        elif section == "success" and s.startswith("AC_") and s.endswith(":"):
            current_ac = s.rstrip(":")
            result["per_ac"][current_ac] = {}
        elif section == "success" and current_ac and ":" in s:
            key, _, val_part = s.partition(":")
            key = key.strip()
            val_token = val_part.strip().split()[0] if val_part.strip() else ""
            try:
                result["per_ac"][current_ac][key] = float(val_token)
            except ValueError:
                pass
        elif section == "failure" and "Failures:" in s:
            parts = s.split()
            if len(parts) >= 3:
                ac = parts[0]
                try:
                    result["failure_ac"][ac] = float(parts[-1])
                except: pass
        elif section == "reasons" and ":" in s:
            key, _, val = s.rpartition(":")
            key = key.strip()
            try:
                result["failure_reasons"][key] = float(val.strip())
            except: pass

    return result


def average_stats(stats_list: list) -> dict:
    """Average parsed stats dicts across multiple runs."""
    n = len(stats_list)
    if n == 0:
        return None

    avg = {
        "pedca_ratio": stats_list[0].get("pedca_ratio", 0.0),
        "total_successes": sum(s["total_successes"] for s in stats_list) / n,
        "total_failures": sum(s["total_failures"] for s in stats_list) / n,
        "total_retransmissions": sum(s["total_retransmissions"] for s in stats_list) / n,
        "per_ac": {},
        "failure_ac": {},
        "failure_reasons": {},
    }

    all_acs = set()
    for s in stats_list:
        all_acs.update(s["per_ac"].keys())

    for ac in sorted(all_acs):
        avg["per_ac"][ac] = {}
        all_keys = set()
        for s in stats_list:
            if ac in s["per_ac"]:
                all_keys.update(s["per_ac"][ac].keys())
        for key in sorted(all_keys):
            vals = [s["per_ac"][ac][key] for s in stats_list
                    if ac in s["per_ac"] and key in s["per_ac"][ac]]
            avg["per_ac"][ac][key] = sum(vals) / len(vals) if vals else 0

    all_facs = set()
    for s in stats_list:
        all_facs.update(s["failure_ac"].keys())
    for ac in sorted(all_facs):
        vals = [s["failure_ac"].get(ac, 0) for s in stats_list]
        avg["failure_ac"][ac] = sum(vals) / n

    all_reasons = set()
    for s in stats_list:
        all_reasons.update(s["failure_reasons"].keys())
    for reason in sorted(all_reasons):
        vals = [s["failure_reasons"].get(reason, 0) for s in stats_list]
        avg["failure_reasons"][reason] = sum(vals) / n

    return avg


def format_stats_text(avg: dict, n_runs: int) -> str:
    """Format averaged stats dict into text."""
    lines = []
    lines.append(f"=== WifiTxStatsHelper (MAC-layer) [Averaged over {n_runs} runs] ===")
    lines.append(f"P-EDCA Ratio: {avg['pedca_ratio']}")
    lines.append(f"Total Successes:       {avg['total_successes']:.1f}")
    lines.append(f"Total Failures:        {avg['total_failures']:.1f}")
    lines.append(f"Total Retransmissions: {avg['total_retransmissions']:.1f}")
    lines.append("")
    lines.append("--- Per-AC Success Statistics ---")

    for ac in sorted(avg["per_ac"].keys()):
        m = avg["per_ac"][ac]
        lines.append(f"{ac}:")
        if "Successes" in m:
            lines.append(f"  Successes:         {m['Successes']:.1f}")
        if "Throughput" in m:
            lines.append(f"  Throughput:        {m['Throughput']:.6g} Mbps")
        if "Packet Loss" in m:
            lines.append(f"  Packet Loss:       {m['Packet Loss']:.4f} %")
        if "Avg Retx/MPDU" in m:
            lines.append(f"  Avg Retx/MPDU:     {m['Avg Retx/MPDU']:.6g}")
        if "Avg Queue Delay" in m:
            lines.append(f"  Avg Queue Delay:   {m['Avg Queue Delay']:.3f} us (Enqueue->TxStart)")
        if "Avg Access Delay" in m:
            lines.append(f"  Avg Access Delay:  {m['Avg Access Delay']:.3f} us (TxStart->Ack)")
        if "Avg MAC Delay" in m:
            lines.append(f"  Avg MAC Delay:     {m['Avg MAC Delay']:.3f} us (Total: Enqueue->Ack)")
        lines.append("")

    lines.append("--- Per-AC Failure Statistics ---")
    for ac, count in sorted(avg["failure_ac"].items()):
        lines.append(f"{ac} Failures: {count:.1f}")

    if avg["failure_reasons"]:
        lines.append("")
        lines.append("--- Failure Reasons by AC ---")
        for reason, count in sorted(avg["failure_reasons"].items()):
            lines.append(f"  {reason}: {count:.1f}")

    return "\n".join(lines) + "\n"


# ─────────────────────── Multi-Run Group Execution ───────────────────

def aggregate_runs(run_results: list, ratio: float, n_sta: int,
                   data_rate: str, n_runs: int) -> dict:
    """
    Given a list of per-run result dicts for one (ratio, nSta) pair,
    average histograms and statistics, clean up per-run CSVs, and
    return the aggregated result dict.
    """
    csv_paths = [r["csv_path"] for r in run_results if r["csv_path"]]

    avg_csv = OUT_DIR / csv_name(ratio, n_sta, data_rate)
    if csv_paths:
        average_histograms(csv_paths, avg_csv, n_runs)
        for cp in csv_paths:
            try:
                cp.unlink()
            except OSError:
                pass

    stdouts = [r["stdout"] for r in run_results if r["success"]]
    parsed_list = [parse_stats(s) for s in stdouts]
    avg_stats = average_stats(parsed_list)
    avg_stdout = format_stats_text(avg_stats, n_runs) if avg_stats else ""

    total_elapsed = sum(r["elapsed"] for r in run_results)
    n_success = sum(1 for r in run_results if r["success"])

    return {
        "success":   n_success > 0,
        "ratio":     ratio,
        "n_sta":     n_sta,
        "csv_path":  avg_csv if csv_paths else None,
        "stdout":    avg_stdout,
        "elapsed":   total_elapsed,
        "n_success": n_success,
        "n_runs":    n_runs,
    }


# ─────────────────────── Log Writer ──────────────────────────────────

def write_log(n_sta: int, data_rate: str, results: dict, n_runs: int, ratios: list):
    log_path = OUT_DIR / log_name(n_sta, data_rate)
    with open(log_path, "w") as f:
        f.write(f"{'='*70}\n")
        f.write(f"  Simulation Log — nSta={n_sta}  dataRate={data_rate}\n")
        f.write(f"  Ratios: {ratios}\n")
        f.write(f"  Runs per ratio: {n_runs}\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*70}\n\n")

        for ratio in ratios:
            if ratio not in results:
                continue
            r = results[ratio]
            f.write(f"{'─'*70}\n")
            f.write(f"  P-EDCA Ratio = {ratio:.0%}  (nSta={n_sta})\n")
            f.write(f"  Successful runs: {r.get('n_success', '?')}/{r.get('n_runs', '?')}\n")
            f.write(f"  Total elapsed: {r['elapsed']:.1f}s\n")
            if r["csv_path"]:
                f.write(f"  Averaged CSV: {r['csv_path']}\n")
            f.write(f"{'─'*70}\n")
            f.write(f"\n--- Averaged Statistics ---\n{r['stdout']}\n")
            f.write(f"\n")


# ─────────────────────── Data Loading ────────────────────────────────

def load_histogram(csv_path: Path):
    rows = []
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        required = {"bin_start_us", "bin_end_us", "probability"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(
                f"CSV header must contain {sorted(required)}, got {reader.fieldnames}"
            )
        for row in reader:
            rows.append({
                "start": float(row["bin_start_us"]),
                "end":   float(row["bin_end_us"]),
                "prob":  float(row["probability"]),
            })

    if not rows:
        raise ValueError("CSV has no data rows")

    widths = [r["end"] - r["start"] for r in rows if r["end"] > r["start"]]
    if not widths:
        raise ValueError("Invalid bins")

    widths_sorted = sorted(widths)
    bin_width = widths_sorted[len(widths_sorted) // 2]

    min_start = min(r["start"] for r in rows)
    max_end   = max(r["end"]   for r in rows)

    # Build a fast lookup: round bin_start to nearest bin edge
    # to avoid floating-point matching issues
    prob_lookup = {}
    for r in rows:
        # Round to nearest bin_width multiple for reliable lookup
        key = round(r["start"] / bin_width) * bin_width
        prob_lookup[key] = r["prob"]

    full_starts, full_probs = [], []
    cur = min_start
    eps = bin_width * 1e-6
    while cur < max_end - eps:
        full_starts.append(cur)
        key = round(cur / bin_width) * bin_width
        full_probs.append(prob_lookup.get(key, 0.0))
        cur += bin_width

    mids = [s + 0.5 * bin_width for s in full_starts]
    return mids, full_probs, min_start, max_end, bin_width


# ─────────────────────── Tick Computation ────────────────────────────

def nice_step(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    frac = value / (10 ** exp)
    if frac <= 1:   nice = 1
    elif frac <= 2: nice = 2
    elif frac <= 5: nice = 5
    else:           nice = 10
    return nice * (10 ** exp)


def build_ticks(xmin: float, xmax: float, fig_width: float):
    span = max(xmax - xmin, 1.0)
    target_ticks = max(6, int(fig_width * 2.0))
    step = nice_step(span / target_ticks)
    first = math.floor(xmin / step) * step
    ticks = []
    t = first
    while t <= xmax + step * 0.01:
        ticks.append(round(t, 6))
        t += step
    return ticks


# ─────────────────────── Color Palette ───────────────────────────────

def get_ratio_colors(ratios: list) -> dict:
    """Assign distinct colors to each ratio using a colormap."""
    n = len(ratios)
    if n <= 6:
        # Hand-picked distinguishable colors
        palette = ["#4C72B0", "#55A868", "#C4A000", "#DD8452", "#C44E52", "#8172B3"]
        return {r: palette[i % len(palette)] for i, r in enumerate(ratios)}
    else:
        cmap = cm.get_cmap("viridis", n)
        return {r: cmap(i) for i, r in enumerate(ratios)}


# ─────────────────────── Combined Overlay Plot ───────────────────────

def _compute_percentile_xlim(all_series: list, percentile: float = 0.95) -> float:
    """
    Compute the x value at which the combined CDF across all series
    reaches `percentile` (e.g. 0.95 = 95th percentile).
    Returns that x value as the upper x-axis bound for the zoomed PDF.
    """
    # Merge all (mid, prob) pairs from every series, keyed by mid
    combined = defaultdict(float)
    for mids, probs in all_series:
        for m, p in zip(mids, probs):
            combined[m] += p

    if not combined:
        return float("inf")

    # Normalize so total = 1 per series, then average
    n_series = len(all_series)
    sorted_mids = sorted(combined.keys())
    total = sum(combined.values()) / n_series  # average total prob

    if total <= 0:
        return float("inf")

    cumulative = 0.0
    target = percentile * total
    for m in sorted_mids:
        cumulative += combined[m] / n_series
        if cumulative >= target:
            return m

    return sorted_mids[-1] if sorted_mids else float("inf")


def plot_combined(csv_dict: dict, out_path: Path, n_sta: int, data_rate: str,
                  ratios: list, n_runs: int = 1,
                  fig_width: float = 14.0, fig_height: float = 10.0, dpi: int = 200):
    """
    Generate a 2-subplot figure:
      Top:    Zoomed PDF (clipped at 95th percentile to focus on the peak)
      Bottom: CDF overlay (integral of the displayed PDF)
    """
    fig, (ax_pdf, ax_cdf) = plt.subplots(2, 1, figsize=(fig_width, fig_height))

    colors = get_ratio_colors(ratios)

    # Load each ratio's own histogram data directly (no grid remapping)
    loaded_data = {}   # ratio -> (mids, probs, bw)
    all_series = []
    global_xmin = float("inf")

    for ratio in ratios:
        if ratio not in csv_dict:
            continue
        csv_path = csv_dict[ratio]
        if not csv_path.exists():
            continue
        try:
            mids, probs, xmin, xmax, bw = load_histogram(csv_path)
        except Exception:
            continue
        loaded_data[ratio] = (mids, probs, bw)
        all_series.append((mids, probs))
        global_xmin = min(global_xmin, xmin)

    if not loaded_data:
        plt.close(fig)
        return

    # Compute 95th percentile x-limit for zoomed view
    x_95 = _compute_percentile_xlim(all_series, 0.95)
    zoom_xmax = x_95 * 1.10  # 10% padding

    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""

    # ── Top subplot: Zoomed PDF ──
    for ratio in ratios:
        if ratio not in loaded_data:
            continue
        mids, probs, bw = loaded_data[ratio]

        # Clip to zoomed range
        z_mids = [m for m, p in zip(mids, probs) if m <= zoom_xmax]
        z_probs = [p for m, p in zip(mids, probs) if m <= zoom_xmax]

        if not z_mids:
            continue

        label = f"P-EDCA {ratio:.0%}"
        color = colors[ratio]

        ax_pdf.plot(z_mids, z_probs, linewidth=0.8, color=color, label=label)
        ax_pdf.fill_between(z_mids, z_probs, alpha=0.08, color=color)

    ax_pdf.set_xlim(global_xmin, zoom_xmax)
    ticks = build_ticks(global_xmin, zoom_xmax, fig_width)
    ax_pdf.set_xticks(ticks)

    ax_pdf.tick_params(axis="x", labelsize=8, rotation=45)
    ax_pdf.set_xlabel("Delay (µs)", fontsize=10)
    ax_pdf.set_ylabel("Probability", fontsize=11)
    ax_pdf.set_title(
        f"VO Delay PDF (zoomed to 95th pctl)  —  nSta={n_sta}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax_pdf.grid(True, alpha=0.25, linestyle="--")
    ax_pdf.legend(loc="upper right", fontsize=8)

    # ── Bottom subplot: CDF ──
    # CDF(x) = P(delay ≤ x), normalized by each ratio's FULL total probability
    for ratio in ratios:
        if ratio not in loaded_data:
            continue
        mids, probs, bw = loaded_data[ratio]

        # Use the FULL data's total for normalization (not just zoomed portion)
        full_total = sum(probs)
        if full_total <= 0:
            continue

        # Compute CDF over the full data
        cdf_mids = []
        cdf_vals = []
        running = 0.0
        for m, p in zip(mids, probs):
            running += p
            if m <= zoom_xmax:
                cdf_mids.append(m)
                cdf_vals.append(running / full_total)

        if not cdf_mids:
            continue

        label = f"P-EDCA {ratio:.0%}"
        color = colors[ratio]
        ax_cdf.plot(cdf_mids, cdf_vals, linewidth=1.0, color=color, label=label)

    ax_cdf.set_xlim(global_xmin, zoom_xmax)
    ticks = build_ticks(global_xmin, zoom_xmax, fig_width)
    ax_cdf.set_xticks(ticks)

    ax_cdf.set_ylim(0, 1.02)
    ax_cdf.tick_params(axis="x", labelsize=8, rotation=45)
    ax_cdf.set_xlabel("Delay (µs)", fontsize=10)
    ax_cdf.set_ylabel("Cumulative Probability", fontsize=11)
    ax_cdf.set_title(
        f"VO Delay CDF  —  nSta={n_sta}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax_cdf.grid(True, alpha=0.25, linestyle="--")
    ax_cdf.legend(loc="lower right", fontsize=8)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)


# ─────────────────── Statistics Comparison File ──────────────────────

def write_comparison_stats(all_results: dict, data_rate: str,
                           n_runs: int, ratios: list):
    stats_path = OUT_DIR / f"ratio_sweep_statistics_{data_rate}.txt"
    with open(stats_path, "w") as f:
        f.write(f"{'='*100}\n")
        f.write(f"  P-EDCA Ratio Sweep Statistics\n")
        f.write(f"  dataRate = {data_rate}    simTime = {SIM_TIME}s    "
                f"runs = {n_runs} (averaged)\n")
        f.write(f"  Ratios: {ratios}\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*100}\n\n")

        for n_sta in sorted(all_results.keys()):
            results = all_results[n_sta]
            f.write(f"{'='*100}\n")
            f.write(f"  nSta = {n_sta}\n")
            f.write(f"{'='*100}\n\n")

            for ratio in ratios:
                if ratio in results and results[ratio]["success"]:
                    r = results[ratio]
                    f.write(f"P-EDCA Ratio = {ratio:.0%}\n")
                    f.write(r["stdout"])
                    f.write(f"\n")
                else:
                    f.write(f"P-EDCA Ratio = {ratio:.0%}\n")
                    f.write(f"  (simulation failed or not run)\n\n")

            f.write(f"{'='*100}\n\n\n")

    return stats_path


# ─────────── Parse stats file for Packet Loss vs nSta plot ───────────

def parse_stats_file_for_packet_loss(stats_path: Path, ratios: list) -> dict:
    """
    Parse ratio_sweep_statistics_*.txt to extract AC_VO Packet Loss
    for each (nSta, ratio) pair.
    Returns: {ratio: [(nSta, packet_loss_pct), ...]}
    """
    result = {r: [] for r in ratios}
    if not stats_path.exists():
        return result

    text = stats_path.read_text()
    current_nsta = None
    current_ratio = None
    in_ac_vo = False

    for line in text.splitlines():
        s = line.strip()
        # Match "nSta = 10"
        m = re.match(r"nSta\s*=\s*(\d+)", s)
        if m:
            current_nsta = int(m.group(1))
            current_ratio = None
            in_ac_vo = False
            continue
        # Match "P-EDCA Ratio = 60%"
        m = re.match(r"P-EDCA Ratio\s*=\s*(\d+)%", s)
        if m:
            pct = int(m.group(1))
            current_ratio = pct / 100.0
            in_ac_vo = False
            continue
        # Match "P-EDCA Ratio: 0.6" (inside stats block)
        if s.startswith("P-EDCA Ratio:"):
            continue
        # Detect AC_VO section
        if s == "AC_VO:":
            in_ac_vo = True
            continue
        if s.startswith("AC_") and s.endswith(":") and s != "AC_VO:":
            in_ac_vo = False
            continue
        if s.startswith("---"):
            in_ac_vo = False
            continue
        # Extract Packet Loss inside AC_VO
        if in_ac_vo and s.startswith("Packet Loss:"):
            m2 = re.search(r"([\d.]+)\s*%", s)
            if m2 and current_nsta is not None and current_ratio is not None:
                pkt_loss = float(m2.group(1))
                if current_ratio in result:
                    result[current_ratio].append((current_nsta, pkt_loss))
            in_ac_vo = False

    # Sort by nSta
    for r in result:
        result[r].sort(key=lambda x: x[0])
    return result


def plot_packet_loss_vs_nsta(stats_path: Path, out_path: Path,
                             data_rate: str, ratios: list,
                             n_runs: int = 1,
                             fig_width: float = 12.0,
                             fig_height: float = 6.0,
                             dpi: int = 200):
    """
    Generate a Packet Loss (%) vs nSta plot with one line per P-EDCA ratio.
    """
    data = parse_stats_file_for_packet_loss(stats_path, ratios)
    colors = get_ratio_colors(ratios)

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    has_data = False
    for ratio in ratios:
        pts = data.get(ratio, [])
        if not pts:
            continue
        nsta_vals = [p[0] for p in pts]
        loss_vals = [p[1] for p in pts]
        label = f"P-EDCA {ratio:.0%}"
        color = colors[ratio]
        ax.plot(nsta_vals, loss_vals, marker="o", markersize=4,
                linewidth=1.2, color=color, label=label)
        has_data = True

    if not has_data:
        plt.close(fig)
        return None

    runs_label = f" (avg of {n_runs} runs)" if n_runs > 1 else ""
    ax.set_xlabel("Number of Stations (nSta)", fontsize=11)
    ax.set_ylabel("VO Packet Loss (%)", fontsize=11)
    ax.set_title(
        f"VO Packet Loss vs nSta  —  {data_rate}{runs_label}",
        fontsize=13, fontweight="bold"
    )
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.legend(loc="upper left", fontsize=9)

    # Get all nSta values for x-ticks
    all_nsta = sorted(set(n for r in ratios for n, _ in data.get(r, [])))
    if len(all_nsta) > 15:
        # Show every other tick if too many
        ax.set_xticks(all_nsta[::2])
    else:
        ax.set_xticks(all_nsta)
    ax.tick_params(axis="x", labelsize=8, rotation=45)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


# ────────────────── Parallel-safe single-plot wrapper ─────────────────

def _plot_single_nsta(args_tuple):
    """
    Wrapper for plot_combined that can be used with ProcessPoolExecutor.
    Receives all arguments as a tuple.
    """
    csv_paths, out_pdf, n_sta, data_rate, ratios, n_runs, fw, fh, dpi = args_tuple
    try:
        plot_combined(csv_paths, out_pdf, n_sta, data_rate, ratios, n_runs, fw, fh, dpi)
        return n_sta, True, out_pdf.name
    except Exception as e:
        return n_sta, False, str(e)


# ──────────────────────────── Main ───────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="P-EDCA Ratio Sweep: run simulations and plot delay PDFs"
    )
    parser.add_argument("--plot-only", action="store_true",
                        help="Skip simulations, only re-plot existing CSVs")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help=f"Max parallel nSta groups (default: {MAX_WORKERS})")
    parser.add_argument("--runs", type=int, default=N_RUNS,
                        help=f"Runs per scenario to average (default: {N_RUNS})")
    parser.add_argument("--ratios", nargs="+", type=float, default=None,
                        help=f"P-EDCA ratios to sweep (default: {PEDCA_RATIOS})")
    parser.add_argument("--nsta", nargs="+", type=int, default=None,
                        help=f"nSta values to sweep (default: {N_STA_LIST})")
    parser.add_argument("--fig-width",  type=float, default=14.0)
    parser.add_argument("--fig-height", type=float, default=5.5)
    parser.add_argument("--dpi",        type=int,   default=200)
    args = parser.parse_args()

    data_rate = DATA_RATE
    sim_time  = SIM_TIME
    bin_us    = BIN_WIDTH
    workers   = args.workers
    n_runs    = args.runs
    ratios    = sorted(args.ratios) if args.ratios else PEDCA_RATIOS
    nsta_list = args.nsta if args.nsta else N_STA_LIST

    print(f"\n╔══════════════════════════════════════════════════════════╗")
    print(f"║  P-EDCA Ratio Sweep (Parallel, Multi-Run Averaging)    ║")
    print(f"║  nSta = {nsta_list}")
    print(f"║  ratios = {ratios}")
    print(f"║  dataRate = {data_rate}    simTime = {sim_time}s")
    print(f"║  runs = {n_runs}    workers = {workers}")
    print(f"║  binary = {SIM_BINARY}")
    print(f"╚══════════════════════════════════════════════════════════╝\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t_total = time.time()

    # ── Helper: parallel PDF/CDF plot generation ──
    plot_workers = max(1, mp_cpu_count() or 4)

    def generate_plots_parallel(nsta_list_plot, csv_source=None):
        """
        Generate per-nSta PDF/CDF plots in parallel using ThreadPoolExecutor.
        (ProcessPoolExecutor deadlocks with matplotlib's Agg backend on Linux fork.)
        csv_source: if None, look up files on disk; otherwise a dict {nSta: {ratio: path}}.
        """
        plot_tasks = []
        for n_sta in nsta_list_plot:
            csv_paths = {}
            for ratio in ratios:
                p = OUT_DIR / csv_name(ratio, n_sta, data_rate)
                if p.exists() and p.stat().st_size > 10:
                    csv_paths[ratio] = p
            if csv_paths:
                out_pdf = OUT_DIR / combined_plot_name(n_sta, data_rate)
                plot_tasks.append(
                    (csv_paths, out_pdf, n_sta, data_rate, ratios, n_runs,
                     args.fig_width, args.fig_height, args.dpi)
                )
            else:
                print(f"    ⚠ nSta={n_sta}: no CSVs, skipping plot")

        if not plot_tasks:
            return

        print(f"  Plotting {len(plot_tasks)} charts in parallel "
              f"({plot_workers} workers)...")

        with ThreadPoolExecutor(max_workers=plot_workers) as pexec:
            futs = {pexec.submit(_plot_single_nsta, t): t[2] for t in plot_tasks}
            for fut in as_completed(futs):
                n_sta_done = futs[fut]
                try:
                    ns, ok, msg = fut.result()
                    if ok:
                        print(f"    ✔ {msg}")
                    else:
                        print(f"    ✗ nSta={ns}: {msg}")
                except Exception as e:
                    print(f"    ✗ nSta={n_sta_done}: {e}")

    if args.plot_only:
        # ── Plot-only mode ──
        for n_sta in nsta_list:
            for ratio in ratios:
                p = OUT_DIR / csv_name(ratio, n_sta, data_rate)
                if p.exists():
                    print(f"  [plot-only] Found: {p.name}")
                else:
                    print(f"  [plot-only] ✗ Missing: {p.name}")

        print(f"\n{'─'*60}")
        print(f"  Generating PDF/CDF plots (parallel)...")
        print(f"{'─'*60}")
        generate_plots_parallel(nsta_list)

        # ── Packet loss vs nSta plot ──
        stats_path = OUT_DIR / f"ratio_sweep_statistics_{data_rate}.txt"
        if stats_path.exists():
            print(f"\n{'─'*60}")
            print(f"  Generating Packet Loss vs nSta plot...")
            print(f"{'─'*60}")
            loss_pdf = OUT_DIR / f"vo_packet_loss_vs_nSta_{data_rate}.pdf"
            result = plot_packet_loss_vs_nsta(
                stats_path, loss_pdf, data_rate, ratios, n_runs,
                args.fig_width, args.fig_height, args.dpi
            )
            if result:
                print(f"    ✔ {loss_pdf.name}")
            else:
                print(f"    ⚠ No packet loss data found in {stats_path.name}")
    else:
        # ── Parallel simulation mode (flat task pool) ──
        all_results = {}  # {nSta: {ratio: aggregated_result}}
        total_sims = len(nsta_list) * len(ratios) * n_runs

        print(f"  Launching {len(nsta_list)} nSta × {len(ratios)} ratios "
              f"× {n_runs} runs = {total_sims} simulations")
        print(f"  ({workers} concurrent simulations)\n")

        # Flatten all individual simulations into the thread pool
        raw_results = defaultdict(lambda: defaultdict(list))
        #  raw_results[nSta][ratio] = [result_dict, ...]

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for n_sta in nsta_list:
                for ratio in ratios:
                    for run_idx in range(n_runs):
                        fut = executor.submit(
                            run_single_sim, ratio, n_sta, data_rate,
                            sim_time, bin_us, run_idx
                        )
                        futures[fut] = (n_sta, ratio, run_idx)

            done_count = 0
            for future in as_completed(futures):
                n_sta, ratio, run_idx = futures[future]
                done_count += 1
                try:
                    r = future.result()
                    raw_results[n_sta][ratio].append(r)
                    status = "✔" if r["success"] else "✗"
                    if done_count % max(1, total_sims // 40) == 0 or not r["success"]:
                        print(f"  [{done_count}/{total_sims}] {status} "
                              f"nSta={n_sta:>2} ratio={ratio:.0%} "
                              f"run={run_idx} {r['elapsed']:.1f}s")
                except Exception as e:
                    print(f"  ✗ nSta={n_sta} ratio={ratio:.0%} "
                          f"run={run_idx} EXCEPTION: {e}")

        # ── Aggregate per (nSta, ratio) ──
        print(f"\n{'─'*60}")
        print(f"  Aggregating results...")
        print(f"{'─'*60}")

        for n_sta in sorted(raw_results.keys()):
            all_results[n_sta] = {}
            for ratio in ratios:
                run_list = raw_results[n_sta].get(ratio, [])
                if run_list:
                    agg = aggregate_runs(run_list, ratio, n_sta, data_rate, n_runs)
                    all_results[n_sta][ratio] = agg
                    status = "✔" if agg["success"] else "✗"
                    print(f"  {status} nSta={n_sta:>2}  ratio={ratio:.0%}  "
                          f"{agg['n_success']}/{n_runs} runs OK  "
                          f"elapsed={agg['elapsed']:.1f}s")

            write_log(n_sta, data_rate, all_results[n_sta], n_runs, ratios)

        # ── Generate per-nSta PDF/CDF plots (parallel) ──
        print(f"\n{'─'*60}")
        print(f"  Generating PDF/CDF plots (parallel)...")
        print(f"{'─'*60}")
        generate_plots_parallel(nsta_list)

        # ── Statistics comparison ──
        print(f"\n{'─'*60}")
        print(f"  Generating statistics comparison...")
        print(f"{'─'*60}")
        stats_path = write_comparison_stats(all_results, data_rate, n_runs, ratios)
        print(f"    ✔ {stats_path.name}  ({stats_path.stat().st_size:,} bytes)")

        # ── Packet loss vs nSta plot ──
        print(f"\n{'─'*60}")
        print(f"  Generating Packet Loss vs nSta plot...")
        print(f"{'─'*60}")
        loss_pdf = OUT_DIR / f"vo_packet_loss_vs_nSta_{data_rate}.pdf"
        result = plot_packet_loss_vs_nsta(
            stats_path, loss_pdf, data_rate, ratios, n_runs,
            args.fig_width, args.fig_height, args.dpi
        )
        if result:
            print(f"    ✔ {loss_pdf.name}")
        else:
            print(f"    ⚠ No packet loss data found")

    elapsed_total = time.time() - t_total

    # ── Summary ──
    print(f"\n{'═'*60}")
    print(f"  Sweep complete!  Total time: {elapsed_total:.1f}s")
    print(f"  Output directory: {OUT_DIR}")
    print(f"\n  Averaged CSV files:")
    for f in sorted(OUT_DIR.glob(f"*_vo_delay_pdf_*_{data_rate}.csv")):
        if "_run" not in f.name:
            print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    print(f"\n  Plot files:")
    for f in sorted(OUT_DIR.glob(f"vo_delay_probability_*_{data_rate}.pdf")):
        print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    plot_loss = OUT_DIR / f"vo_packet_loss_vs_nSta_{data_rate}.pdf"
    if plot_loss.exists():
        print(f"    {plot_loss.name}  ({plot_loss.stat().st_size:,} bytes)")
    print(f"\n  Log files:")
    for f in sorted(OUT_DIR.glob(f"sim_log_*_{data_rate}.txt")):
        print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    print(f"\n  Statistics comparison:")
    for f in sorted(OUT_DIR.glob(f"ratio_sweep_statistics_*.txt")):
        print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
