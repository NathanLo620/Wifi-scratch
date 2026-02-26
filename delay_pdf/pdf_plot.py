#!/usr/bin/env python3
"""
Unified P-EDCA vs EDCA Delay PDF Pipeline (Parallel Sweep, Multi-Run Averaging)
=================================================================================
1. Sweeps over multiple STA counts IN PARALLEL
2. For each nSta, runs both P-EDCA and EDCA ns-3 simulations N_RUNS times
3. Averages histogram probabilities across runs
4. Outputs per-scenario averaged CSVs and one combined overlay plot per nSta

Output files per nSta:
  - edca_vo_delay_pdf_nSta{N}_{rate}.csv        (averaged histogram)
  - pedca_vo_delay_pdf_nSta{N}_{rate}.csv        (averaged histogram)
  - *_run{R}.csv                                  (per-run raw histograms)
  - vo_delay_probability_nSta{N}_{rate}.pdf       (EDCA blue + P-EDCA red overlay)
  - sim_log_nSta{N}_{rate}.txt                    (combined log)
  - PEDCA_vs_EDCA_statistics_{rate}.txt            (side-by-side averaged stats)

Usage:
  python3 pdf_plot.py              # Run full parallel sweep + plot
  python3 pdf_plot.py --plot-only  # Skip simulations, only re-plot existing CSVs
  python3 pdf_plot.py --workers 8  # Use 8 parallel workers
  python3 pdf_plot.py --runs 3     # Override number of runs per scenario
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════
#  USER-CONFIGURABLE PARAMETERS
# ══════════════════════════════════════════════════════════════════════
N_STA_LIST  = list(range(2, 51, 2))   # 2, 4, 6, ..., 50
DATA_RATE   = "0.5Mbps"               # Fixed data rate
SIM_TIME    = 10.0                    # Simulation duration (seconds)
BIN_WIDTH   = 5                       # VO delay PDF bin width (µs)
MAX_WORKERS = 20                      # Max parallel simulations
N_RUNS      = 5                       # Number of runs to average
# ══════════════════════════════════════════════════════════════════════

# Paths
NS3_DIR = Path("/home/wmnlab/Desktop/ns-3.45")
OUT_DIR = Path("/home/wmnlab/Desktop/ns-3.45/scratch/delay_pdf/delay_result_0.5Mbps[2-50]_rts_on")

# ── Force non-interactive backend ──
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ─────────────────────── Filename Helpers ────────────────────────────

def csv_name(scenario: str, n_sta: int, data_rate: str, run_idx: int = None) -> str:
    """Return CSV filename. If run_idx is None, returns the averaged filename."""
    if run_idx is not None:
        return f"{scenario}_vo_delay_pdf_nSta{n_sta}_{data_rate}_run{run_idx}.csv"
    return f"{scenario}_vo_delay_pdf_nSta{n_sta}_{data_rate}.csv"

def combined_plot_name(n_sta: int, data_rate: str) -> str:
    return f"vo_delay_probability_nSta{n_sta}_{data_rate}.pdf"

def log_name(n_sta: int, data_rate: str) -> str:
    return f"sim_log_nSta{n_sta}_{data_rate}.txt"


# ─────────────────────── Scenario Definitions ────────────────────────

SCENARIOS = [
    {
        "key":   "pedca",
        "cc":    "scratch/pedca_verification_nsta.cc",
        "label": "P-EDCA",
        "color": "#C44E52",   # red
    },
    {
        "key":   "edca",
        "cc":    "scratch/wifi_backoff80211n.cc",
        "label": "EDCA",
        "color": "#4C72B0",   # blue
    },
]


# ─────────────────────── Single Simulation Task ──────────────────────

def run_single_sim(scenario: dict, n_sta: int, data_rate: str,
                   sim_time: float, bin_us: int, run_idx: int = 0) -> dict:
    """
    Run ONE ns-3 simulation with a specific RngRun seed.
    Returns a dict with results. Thread-safe.
    """
    csv_file = csv_name(scenario["key"], n_sta, data_rate, run_idx)
    csv_path = OUT_DIR / csv_file
    relative_csv = str(csv_path.relative_to(NS3_DIR))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    args = (
        f"--nSta={n_sta} "
        f"--simTime={sim_time} "
        f"--dataRate={data_rate} "
        f"--voicePdfBinUs={bin_us} "
        f"--voicePdfOutput={relative_csv} "
        f"--RngRun={run_idx + 1}"
    )
    cmd = ["./ns3", "run", f"{scenario['cc']} {args}"]

    t0 = time.time()
    result_info = {
        "scenario": scenario["key"],
        "label":    scenario["label"],
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
        result_info["stdout"] = result.stdout
        result_info["stderr"] = result.stderr

        if csv_path.exists() and csv_path.stat().st_size > 10:
            result_info["csv_path"] = csv_path
            result_info["success"] = True

    except subprocess.CalledProcessError as e:
        result_info["stdout"] = e.stdout or ""
        result_info["stderr"] = e.stderr or ""

    result_info["elapsed"] = time.time() - t0
    return result_info


# ─────────────────────── Histogram Averaging ─────────────────────────

def average_histograms(csv_paths: list, out_path: Path, n_runs: int):
    """
    Read multiple per-run histogram CSVs, average the probability values,
    and write a single averaged CSV.
    """
    all_bins = defaultdict(list)  # (start, end) -> [prob1, prob2, ...]

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
            # Pad missing runs with 0
            while len(probs) < n_runs:
                probs.append(0.0)
            avg_prob = sum(probs) / n_runs
            mid = (start + end) / 2
            width = end - start
            pdf = avg_prob / width if width > 0 else 0
            writer.writerow([start, end, mid, f"{pdf:.8g}", f"{avg_prob:.8g}", 0])


# ─────────────────── Statistics Parsing & Averaging ──────────────────

def extract_stats_block(stdout: str) -> str:
    """
    Extract the WifiTxStatsHelper block from simulation stdout.
    Returns lines from '=== WifiTxStatsHelper' up to (but not including)
    '--- VO Delay PDF' or end of text.
    """
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
        "total_successes": 0,
        "total_failures": 0,
        "total_retransmissions": 0,
        "per_ac": {},       # ac_name -> {metric: value}
        "failure_ac": {},   # ac_name -> count
        "failure_reasons": {},  # "AC_XX REASON": count
    }

    lines = block.splitlines()
    section = None
    current_ac = None

    for line in lines:
        s = line.strip()
        if s.startswith("Total Successes:"):
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
        "total_successes": sum(s["total_successes"] for s in stats_list) / n,
        "total_failures": sum(s["total_failures"] for s in stats_list) / n,
        "total_retransmissions": sum(s["total_retransmissions"] for s in stats_list) / n,
        "per_ac": {},
        "failure_ac": {},
        "failure_reasons": {},
    }

    # Collect all AC names
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


def format_stats_text(avg: dict) -> str:
    """Format averaged stats dict into WifiTxStatsHelper-style text."""
    lines = []
    lines.append(f"=== WifiTxStatsHelper (MAC-layer) [Averaged over {N_RUNS} runs] ===")
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


# ─────────────────────── Multi-Run Pair Execution ────────────────────

def run_nsta_pair(n_sta: int, data_rate: str, sim_time: float,
                  bin_us: int, n_runs: int) -> tuple:
    """
    Run N_RUNS iterations of both P-EDCA and EDCA for one nSta value.
    All runs are sequential within a single thread (avoids ns-3 build locks).
    After all runs, averages histograms and statistics.
    Returns (n_sta, averaged_results).
    """
    all_run_results = []  # list of {sc_key: result_info}

    for run_idx in range(n_runs):
        run_results = {}
        for sc in SCENARIOS:
            r = run_single_sim(sc, n_sta, data_rate, sim_time, bin_us, run_idx)
            run_results[sc["key"]] = r
        all_run_results.append(run_results)

    # ── Build averaged results ──
    averaged = {}
    for sc in SCENARIOS:
        # Collect per-run CSV paths
        csv_paths = [rr[sc["key"]]["csv_path"] for rr in all_run_results
                     if rr[sc["key"]]["csv_path"]]

        # Average histograms → single CSV (same name as old single-run)
        avg_csv = OUT_DIR / csv_name(sc["key"], n_sta, data_rate)
        if csv_paths:
            average_histograms(csv_paths, avg_csv, n_runs)
            # Delete per-run CSVs — only keep the averaged one
            for cp in csv_paths:
                try:
                    cp.unlink()
                except OSError:
                    pass

        # Average statistics
        stdouts = [rr[sc["key"]]["stdout"] for rr in all_run_results
                   if rr[sc["key"]]["success"]]
        parsed_list = [parse_stats(s) for s in stdouts]
        avg_stats = average_stats(parsed_list)
        avg_stdout = format_stats_text(avg_stats) if avg_stats else ""

        total_elapsed = sum(rr[sc["key"]]["elapsed"] for rr in all_run_results)
        n_success = sum(1 for rr in all_run_results if rr[sc["key"]]["success"])

        averaged[sc["key"]] = {
            "success":  n_success > 0,
            "label":    sc["label"],
            "n_sta":    n_sta,
            "csv_path": avg_csv if csv_paths else None,
            "stdout":   avg_stdout,
            "stderr":   "",
            "elapsed":  total_elapsed,
            "cmd":      f"(averaged over {n_runs} runs)",
            "n_success": n_success,
            "n_runs":   n_runs,
        }

    return n_sta, averaged


# ─────────────────────── Log Writer ──────────────────────────────────

def write_log(n_sta: int, data_rate: str, results: dict, n_runs: int):
    """Write combined log for one nSta to a txt file."""
    log_path = OUT_DIR / log_name(n_sta, data_rate)
    with open(log_path, "w") as f:
        f.write(f"{'='*70}\n")
        f.write(f"  Simulation Log — nSta={n_sta}  dataRate={data_rate}\n")
        f.write(f"  Runs per scenario: {n_runs}\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*70}\n\n")

        for sc_key in ["pedca", "edca"]:
            if sc_key not in results:
                continue
            r = results[sc_key]
            f.write(f"{'─'*70}\n")
            f.write(f"  {r['label']}  (nSta={n_sta})\n")
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

    prob_by_start = {r["start"]: r["prob"] for r in rows}

    full_starts, full_probs = [], []
    cur = min_start
    eps = bin_width * 1e-6
    while cur < max_end - eps:
        full_starts.append(cur)
        p = 0.0
        for k, v in prob_by_start.items():
            if abs(k - cur) <= eps:
                p = v
                break
        full_probs.append(p)
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


# ─────────────────────── Combined Overlay Plot ───────────────────────

def plot_combined(csv_dict: dict, out_path: Path, n_sta: int, data_rate: str,
                  n_runs: int = 1,
                  fig_width: float = 14.0, fig_height: float = 5.5, dpi: int = 200):
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    global_xmin = float("inf")
    global_xmax = float("-inf")

    for sc in SCENARIOS:
        key = sc["key"]
        if key not in csv_dict:
            continue
        mids, probs, xmin, xmax, bw = load_histogram(csv_dict[key])
        global_xmin = min(global_xmin, xmin)
        global_xmax = max(global_xmax, xmax)

        ax.bar(mids, probs, width=bw * 0.45, align="center",
               color=sc["color"], alpha=0.25, edgecolor="none")
        ax.plot(mids, probs, linewidth=1.6, marker="o", markersize=2.0,
                color=sc["color"], label=sc["label"])

    if global_xmin < global_xmax:
        ax.set_xlim(global_xmin, global_xmax)
        ticks = build_ticks(global_xmin, global_xmax, fig_width)
        ax.set_xticks(ticks)

    ax.tick_params(axis="x", labelsize=8, rotation=45)
    ax.set_xlabel("Delay (µs)", fontsize=11)
    ax.set_ylabel("Probability", fontsize=11)
    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""
    ax.set_title(
        f"VO Delay Distribution — P-EDCA vs EDCA  (nSta={n_sta}, {data_rate}{runs_label})",
        fontsize=13, fontweight="bold"
    )
    ax.grid(True, alpha=0.25, linestyle="--")
    ax.legend(loc="best", fontsize=10)
    fig.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)


# ─────────────────── Statistics Comparison File ──────────────────────

def write_comparison_stats(all_results: dict, data_rate: str, n_runs: int):
    """
    Write PEDCA_vs_EDCA_statistics.txt with all nSta values in order.
    Format: for each nSta, print EDCA block then P-EDCA block (averaged).
    """
    stats_path = OUT_DIR / f"PEDCA_vs_EDCA_statistics_{data_rate}.txt"
    with open(stats_path, "w") as f:
        f.write(f"{'='*100}\n")
        f.write(f"  P-EDCA vs EDCA Statistics Comparison\n")
        f.write(f"  dataRate = {data_rate}    simTime = {SIM_TIME}s    "
                f"runs = {n_runs} (averaged)\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*100}\n\n")

        for n_sta in sorted(all_results.keys()):
            results = all_results[n_sta]
            f.write(f"{'='*100}\n")
            f.write(f"  nSta = {n_sta}\n")
            f.write(f"{'='*100}\n\n")

            for sc_key, label in [("edca", "EDCA"), ("pedca", "P-EDCA")]:
                if sc_key in results and results[sc_key]["success"]:
                    r = results[sc_key]
                    f.write(f"{label}\n")
                    f.write(r["stdout"])
                    f.write(f"\n")
                else:
                    f.write(f"{label}\n")
                    f.write(f"  (simulation failed or not run)\n\n")

            f.write(f"{'='*100}\n\n\n")

    return stats_path


# ──────────────────────────── Main ───────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Parallel sweep: run P-EDCA & EDCA, plot combined overlay"
    )
    parser.add_argument("--plot-only", action="store_true",
                        help="Skip simulations, only re-plot existing CSVs")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help=f"Max parallel simulation pairs (default: {MAX_WORKERS})")
    parser.add_argument("--runs", type=int, default=N_RUNS,
                        help=f"Number of runs per scenario to average (default: {N_RUNS})")
    parser.add_argument("--fig-width",  type=float, default=14.0)
    parser.add_argument("--fig-height", type=float, default=5.5)
    parser.add_argument("--dpi",        type=int,   default=200)
    args = parser.parse_args()

    data_rate = DATA_RATE
    sim_time  = SIM_TIME
    bin_us    = BIN_WIDTH
    workers   = args.workers
    n_runs    = args.runs

    print(f"\n╔══════════════════════════════════════════════════════════╗")
    print(f"║  VO Delay PDF Sweep (Parallel, Multi-Run Averaging)    ║")
    print(f"║  nSta = {N_STA_LIST}")
    print(f"║  dataRate = {data_rate}    simTime = {sim_time}s")
    print(f"║  runs = {n_runs}    workers = {workers}")
    print(f"╚══════════════════════════════════════════════════════════╝\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t_total = time.time()

    if args.plot_only:
        # ── Plot-only mode: no simulations ──
        for n_sta in N_STA_LIST:
            csv_paths = {}
            for sc in SCENARIOS:
                p = OUT_DIR / csv_name(sc["key"], n_sta, data_rate)
                if p.exists():
                    csv_paths[sc["key"]] = p
                    print(f"  [plot-only] Found: {p.name}")
                else:
                    print(f"  [plot-only] ✗ Missing: {p.name}")
            if csv_paths:
                out_pdf = OUT_DIR / combined_plot_name(n_sta, data_rate)
                plot_combined(csv_paths, out_pdf, n_sta, data_rate, n_runs,
                              args.fig_width, args.fig_height, args.dpi)
                print(f"  ✔ Plot: {out_pdf.name}")
    else:
        # ── Parallel simulation mode ──
        all_results = {}
        total_sims = len(N_STA_LIST) * len(SCENARIOS) * n_runs

        print(f"  Launching {len(N_STA_LIST)} nSta groups × {len(SCENARIOS)} scenarios "
              f"× {n_runs} runs = {total_sims} simulations")
        print(f"  ({workers} nSta groups in parallel)\n")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(run_nsta_pair, n_sta, data_rate, sim_time,
                                bin_us, n_runs): n_sta
                for n_sta in N_STA_LIST
            }

            for future in as_completed(futures):
                n_sta_done = futures[future]
                try:
                    n_sta_val, results = future.result()
                    all_results[n_sta_val] = results

                    # Print summary
                    for sc_key, r in results.items():
                        status = "✔" if r["success"] else "✗"
                        ok = r.get("n_success", "?")
                        print(f"  {status} nSta={n_sta_val:>2}  {r['label']:<7}  "
                              f"{ok}/{n_runs} runs OK  "
                              f"elapsed={r['elapsed']:.1f}s")

                    # Write log
                    write_log(n_sta_val, data_rate, results, n_runs)

                except Exception as e:
                    print(f"  ✗ nSta={n_sta_done} EXCEPTION: {e}")

        # ── Generate plots (sequential, after all sims done) ──
        print(f"\n{'─'*60}")
        print(f"  Generating plots...")
        print(f"{'─'*60}")

        for n_sta in N_STA_LIST:
            csv_paths = {}
            for sc in SCENARIOS:
                p = OUT_DIR / csv_name(sc["key"], n_sta, data_rate)
                if p.exists() and p.stat().st_size > 10:
                    csv_paths[sc["key"]] = p

            if csv_paths:
                out_pdf = OUT_DIR / combined_plot_name(n_sta, data_rate)
                plot_combined(csv_paths, out_pdf, n_sta, data_rate, n_runs,
                              args.fig_width, args.fig_height, args.dpi)
                print(f"    ✔ {out_pdf.name}")
            else:
                print(f"    ⚠ nSta={n_sta}: no CSVs, skipping plot")

        # ── Generate comparison statistics file ──
        print(f"\n{'─'*60}")
        print(f"  Generating statistics comparison...")
        print(f"{'─'*60}")
        stats_path = write_comparison_stats(all_results, data_rate, n_runs)
        print(f"    ✔ {stats_path.name}  ({stats_path.stat().st_size:,} bytes)")

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
    print(f"\n  Log files:")
    for f in sorted(OUT_DIR.glob(f"sim_log_*_{data_rate}.txt")):
        print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    print(f"\n  Statistics comparison:")
    for f in sorted(OUT_DIR.glob(f"PEDCA_vs_EDCA_statistics_*.txt")):
        print(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
